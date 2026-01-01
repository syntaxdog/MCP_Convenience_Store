import os, json, re, asyncio, sys
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from manager import save_to_db, enrich_db_with_tags_high_speed, analyze_text_with_llm
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 저장 위치 설정 (main.py와 공유할 DB 경로)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def normalize_product_data(item: dict) -> dict:
    """
    상품명, 행사내용, 단위 필드를 순차적으로 탐색하여 
    용량(capacity_ml)과 100단위당 가격(unit_price_per_100)을 계산합니다.
    """
    p_name = item.get("product_name", "")
    price = item.get("final_price", 0)
    condition = item.get("discount_condition", "")
    unit_field = item.get("unit", "")
    
    # 1. 탐색할 텍스트 후보군 (순서 중요: 상품명 -> 행사내용 -> 단위)
    # None이 들어올 경우를 대비해 빈 문자열 처리
    search_targets = [
        str(p_name), 
        str(condition), 
        str(unit_field)
    ]
    
    capacity = 0
    
    # 정규식: 소수점 지원 (1.1kg), 대소문자 무시
    # 예: 1.5L, 200ml, 500g, 1kg
    pattern = r'(\d+(?:\.\d+)?)\s*(ml|l|g|kg)'
    
    for text in search_targets:
        match = re.search(pattern, text.lower())
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            
            # 단위 변환 (L, kg -> 1000배)
            if unit in ['l', 'kg']:
                capacity = int(value * 1000)
            else:
                capacity = int(value)
            
            # 묶음 상품 체크 (x3, *3입 등) - 해당 텍스트 내에서 찾기
            bundle_match = re.search(r'[\*x]\s*(\d+)', text.lower())
            if bundle_match:
                count = int(bundle_match.group(1))
                capacity *= count
            
            # 용량을 찾았으면 루프 중단 (더 이상 뒤질 필요 없음)
            break

    # 2. 실질 가격 및 용량 계산 (행사 반영)
    total_capacity = capacity
    pay_price = price
    
    # 행사 내용(condition)은 어디서 용량을 찾았든 항상 참조해야 함
    cond_lower = str(condition).lower()
    
    if "1+1" in cond_lower:
        total_capacity = capacity * 2
    elif "2+1" in cond_lower:
        total_capacity = capacity * 3
        pay_price = price * 2

    # 3. 데이터 주입
    if total_capacity > 0:
        # 0으로 나누기 방지
        item["unit_price_per_100"] = int((pay_price / total_capacity) * 100)
        item["capacity_ml"] = capacity
    else:
        # 용량 파악 불가 시
        item["unit_price_per_100"] = 0
        item["capacity_ml"] = 0
        
    return item

# --- [도구 1] GS 더프레시 크롤러 ---
async def get_gs_the_fresh_deals():
    """GS 더프레시 전단지 데이터를 추출하고 저장합니다."""
    url = "https://web.gsretail.me/Viewer/gsp2/"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle")
            await asyncio.sleep(2) 
            await page.wait_for_selector("img.pageImage", timeout=20000)            
            
            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            images = soup.find_all("img", class_="pageImage")
            raw_texts = [img.get("aria-label") for img in images if img.get("aria-label")]
            
            if not raw_texts:
                await browser.close()
                return "GS 더프레시: 데이터 추출 실패 (aria-label 비어있음)"

            full_text = "\n\n".join(raw_texts)
            lines = [line.strip() for line in full_text.split('\n') if line.strip()]
            
            chunk_size = 15 
            chunks = ["\n".join(lines[i:i + chunk_size]) for i in range(0, len(lines), chunk_size)]
            tasks = [analyze_text_with_llm("GS The Fresh", chunk) for chunk in chunks]
            chunk_results_json = await asyncio.gather(*tasks)
            
            all_extracted_items = []
            for res_json in chunk_results_json:
                try:
                    data = json.loads(res_json)
                    if isinstance(data, dict) and "items" in data:
                        all_extracted_items.extend(data["items"])
                    elif isinstance(data, list):
                        all_extracted_items.extend(data)
                except: continue
            
            final_items_dict = {}  # {상품명: 상품데이터} 구조로 저장하여 중복 방지

            for item in all_extracted_items:
                name = item.get("product_name", "").strip()
                if not name: continue
                
                # 공백 제거한 소문자 이름을 키로 사용
                unique_key = name.replace(" ", "").lower()
                
                # [수정] 가격 데이터 타입 안전하게 변환
                def safe_int(val):
                    if isinstance(val, int): return val
                    try:
                        # 숫자 외의 문자(원, ,, 공백 등) 제거 후 정수 변환
                        import re
                        return int(re.sub(r'[^0-9]', '', str(val)))
                    except:
                        return 999999 # 변환 실패 시 큰 값 부여

                current_price = safe_int(item.get("effective_unit_price", 999999))
                # 비교를 위해 원본 데이터의 타입도 정수로 업데이트해두면 좋습니다.
                item["effective_unit_price"] = current_price

                if unique_key in final_items_dict:
                    # 이제 두 값 모두 확실한 int이므로 에러가 나지 않습니다.
                    existing_price = final_items_dict[unique_key].get("effective_unit_price", 999999)
                    if current_price < existing_price:
                        final_items_dict[unique_key] = item
                else:
                    final_items_dict[unique_key] = item

            # 딕셔너리를 다시 리스트로 변환
            all_extracted_items = list(final_items_dict.values())
            # --- [중복 제거 로직 종료] ---

            await browser.close()
            save_to_db("gs_the_fresh", all_extracted_items)
            return f"GS 더프레시 완료: {len(all_extracted_items)}개 (중복 제거 완료)"

        except Exception as e:
            await browser.close()
            return f"GS 더프레시 에러: {e}"

# --- [도구 2] 이마트 크롤러 ---
async def get_emart_deals():
    """이마트 전단지 데이터를 수집합니다."""
    url = "https://store.emart.com/news/leafletfull.do?division=2"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        hidden_divs = soup.find_all("div", class_="hide")
        all_text = "".join([div.get_text(separator="\n").strip() + "\n" for div in hidden_divs])
        lines = [l.strip() for l in all_text.split('\n') if l.strip()]
        
        total_items = []
        chunk_size = 35 
        tasks = [analyze_text_with_llm("Emart", "\n".join(lines[i : i + chunk_size])) for i in range(0, len(lines), chunk_size)]
        results = await asyncio.gather(*tasks)

        for result_json in results:
            data = json.loads(result_json)
            total_items.extend(data.get("items", []))

        save_to_db("emart", total_items)
        return f"이마트 완료: {len(total_items)}개"
    except Exception as e:
        return f"이마트 에러: {e}"

# --- [도구 3] CU 크롤러 ---
async def get_cu_deals():
    """CU API를 통해 1+1, 2+1 상품을 수집합니다."""
    api_url = "https://cu.bgfretail.com/event/plusAjax.do"
    final_items = []
    seen_names = set()
    event_configs = [{"code": "23", "label": "1+1"}, {"code": "24", "label": "2+1"}]
    
    for config in event_configs:
        page = 1
        while page <= 60:
            payload = {"pageIndex": page, "listType": 0, "searchCondition": config["code"]}
            response = requests.post(api_url, data=payload)
            soup = BeautifulSoup(response.text, 'html.parser')
            prod_elements = soup.find_all("li", class_="prod_list")
            
            if not prod_elements: break
            
            new_count = 0
            for prod in prod_elements:
                name = prod.find("div", class_="name").get_text(strip=True)
                unique_key = f"{name}_{config['label']}"
                if unique_key not in seen_names:
                    seen_names.add(unique_key)
                    new_count += 1
                    price = int(prod.find("div", class_="price").strong.get_text(strip=True).replace(",", ""))
                    img_tag = prod.find("img", class_="prod_img")
                    image_url = ("https:" + img_tag["src"]) if img_tag and img_tag["src"].startswith("//") else (img_tag["src"] if img_tag else "")
                    
                    if config['label'] == "1+1":
                        # 1개 가격으로 2개를 가져옴
                        sale_price = price  
                        effective_unit_price = price // 2
                    elif config['label'] == "2+1":
                        # 2개 가격으로 3개를 가져옴
                        sale_price = price * 2
                        effective_unit_price = (price * 2) // 3
                    else:
                        # 일반 할인 등
                        sale_price = price
                        effective_unit_price = price

                    # 2. 데이터 추가
                    final_items.append({
                        "product_name": name,
                        "original_price": price,            # 상품 1개의 원래 가격
                        "sale_price": sale_price,            # 행사 참여를 위한 실제 결제 총액
                        "effective_unit_price": effective_unit_price,  # 혜택 적용 후 1개당 실질 단가
                        "discount_condition": config['label'],
                        "image_url": image_url
                    })
            if new_count == 0: break
            page += 1
            await asyncio.sleep(0.05)
            
    save_to_db("cu", final_items)
    return f"CU 완료: {len(final_items)}개"

# --- [도구 4] GS25 크롤러 ---
async def get_gs25_deals():
    """GS25 1+1, 2+1 상품 전수 수집"""
    url = "http://gs25.gsretail.com/gscvs/ko/products/event-goods"
    api_url = "http://gs25.gsretail.com/gscvs/ko/products/event-goods-search"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle")
            content = await page.content()
            token = content[content.find('name="CSRFToken" value="')+24 : ].split('"')[0]
            all_items = []
            events = {"ONE_TO_ONE": "1+1", "TWO_TO_ONE": "2+1"}

            for event_key, event_name in events.items():
                p_num = 1
                while True:
                    raw_res = await page.evaluate(f"""
                        async () => {{
                            const r = await fetch('{api_url}', {{
                                method: 'POST',
                                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' }},
                                body: 'pageNum={p_num}&pageSize=50&parameterList={event_key}&CSRFToken={token}'
                            }});
                            return await r.text();
                        }}
                    """)
                    data = json.loads(raw_res)
                    if isinstance(data, str): data = json.loads(data)
                    items_list = data.get("results", [])
                    if not items_list: break

                    for item in items_list:
                        price = item.get("attPrice") or item.get("price") or 0
                        if isinstance(price, str): price = int("".join(filter(str.isdigit, price)))
                        all_items.append({
                            "product_name": item.get("goodsNm", ""),
                            "original_price": price,
                            "sale_price" : price if event_key == "ONE_TO_ONE" else (price*2),
                            "unit_effective_unit_price": price // 2 if event_key == "ONE_TO_ONE" else (price * 2) // 3,
                            "discount_condition": event_name,
                            "image_url": item.get("attFileNm") or item.get("attFileNmOld", "")
                        })
                    p_num += 1
                    if len(items_list) < 50: break

            save_to_db("gs25", all_items)
            await browser.close()
            return f"GS25 완료: {len(all_items)}개"
        except Exception as e:
            await browser.close()
            return f"GS25 에러: {e}"

# --- [도구 5] 세븐일레븐 크롤러 ---
async def get_seven_eleven_deals():
    """세븐일레븐 정밀 수집"""
    api_url = "https://www.7-eleven.co.kr/product/listMoreAjax.asp"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto("https://www.7-eleven.co.kr/product/presentList.asp")
            all_items = []
            for p_tab in [1, 2, 4]:
                curr_page = 1
                while curr_page <= 100:
                    raw_html = await page.evaluate(f"""
                        async () => {{
                            const r = await fetch('{api_url}', {{
                                method: 'POST',
                                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' }},
                                body: 'intCurrPage={curr_page}&intPageSize=10&pTab={p_tab}'
                            }});
                            return await r.text();
                        }}
                    """)
                    if "검색 결과가 없습니다" in raw_html: break
                    soup = BeautifulSoup(raw_html, 'html.parser')
                    li_tags = soup.find_all("li")
                    if not li_tags: break

                    for li in li_tags:
                        name_el = li.select_one(".name")
                        if not name_el: continue
                        price = int("".join(filter(str.isdigit, li.select_one(".price").get_text())))
                        tag = li.select_one(".tag_list_01 li").get_text(strip=True) if li.select_one(".tag_list_01 li") else "행사"
                        if "덤" in tag: continue
                        
                        all_items.append({
                            "product_name": name_el.get_text(strip=True),
                            "original_price": price,
                            "sale_price" : price if "1+1" in tag else price*2 if "2+1" in tag else price,
                            "unit_effective_unit_price": price // 2 if "1+1" in tag else (price * 2) // 3 if "2+1" in tag else price,
                            "discount_condition": tag,
                            "image_url": "https://www.7-eleven.co.kr" + li.select_one("img")["src"] if li.select_one("img") else ""
                        })
                    curr_page += 1
            save_to_db("seven_eleven", all_items)
            await browser.close()
            return f"세븐일레븐 완료: {len(all_items)}개"
        except Exception as e:
            await browser.close()
            return f"세븐일레븐 에러: {e}"

# --- 스케쥴링 ---
async def run_full_pipeline(stores):
    """
    특정 매장들에 대해 수집 및 Enrich 작업을 순차적으로 수행합니다.
    """
    print(f"\n--- 🔄 작업 시작: {', '.join(stores)} ---")
    
    # 1단계: 수집 (크롤링)
    tasks = []
    if "gs_the_fresh" in stores: tasks.append(get_gs_the_fresh_deals())
    if "emart" in stores: tasks.append(get_emart_deals())
    if "cu" in stores: tasks.append(get_cu_deals())
    if "gs25" in stores: tasks.append(get_gs25_deals())
    if "seven_eleven" in stores: tasks.append(get_seven_eleven_deals())
    
    if tasks:
        print(f"🚀 [1단계] {len(tasks)}개 매장 데이터 수집 시작...")
        results = await asyncio.gather(*tasks)
        for res in results:
            print(f"  > {res}")

    # 2단계: AI Enrich (태깅)
    print("\n✨ [2단계] AI Enrich(태깅) 작업 시작...")
    for store in stores:
        try:
            enrich_result = await enrich_db_with_tags_high_speed(store)
            print(f"  > ✅ {store}: {enrich_result}")
        except Exception as e:
            print(f"  > ❌ {store} 태깅 중 오류: {e}")
            
    print(f"--- ✅ 작업 완료: {', '.join(stores)} ---\n")

# --- 메인 실행부 (스케쥴러) ---
async def main():
    scheduler = AsyncIOScheduler()

    # [스케줄 1] 편의점 (CU, GS25, 7-11) - 매월 1일 새벽 1시
    # '0 1 1 * *'
    scheduler.add_job(
        run_full_pipeline,
        CronTrigger(day="1", hour="1", minute="0"),
        args=[["cu", "gs25", "seven_eleven"]],
        name="Monthly_Convenience_Stores"
    )

    # [스케줄 2] GS 더프레시 - 매주 수요일 새벽 1시
    # '0 1 * * 2' (0:월, 1:화, 2:수...)
    scheduler.add_job(
        run_full_pipeline,
        CronTrigger(day_of_week="wed", hour="1", minute="0"),
        args=[["gs_the_fresh"]],
        name="Weekly_GS_The_Fresh"
    )

    # [스케줄 3] 이마트 - 매주 목요일 새벽 1시
    scheduler.add_job(
        run_full_pipeline,
        CronTrigger(day_of_week="thu", hour="1", minute="0"),
        args=[["emart"]],
        name="Weekly_Emart"
    )

    scheduler.start()
    print("⏰ 스케줄러가 시작되었습니다.")
    print("  - 매월 1일 01:00: 편의점 3사")
    print("  - 매주 수요일 01:00: GS 더프레시")
    print("  - 매주 목요일 01:00: 이마트")

    # 수동 실행 모드 처리
    if len(sys.argv) > 1 and sys.argv[1] == "--manual":
        target_store = sys.argv[2:] if len(sys.argv) > 2 else ["gs_the_fresh", "cu", "gs25", "seven_eleven", "emart"]
        print(f"\n⚡ 수동 실행 모드 감지: {target_store} 작업을 즉시 실행합니다.")
        await run_full_pipeline(target_store)

    # 서버가 종료되지 않도록 무한 대기
    try:
        while True:
            await asyncio.sleep(1000)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    # 비동기 실행
    asyncio.run(main())