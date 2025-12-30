import os, json, re, asyncio, sys
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# 저장 위치 설정 (main.py와 공유할 DB 경로)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def save_to_db(store_name: str, items: list):
    """모든 매장이 공통으로 사용하는 저장 함수"""
    file_path = os.path.join(BASE_DIR, f"db_{store_name.lower()}.json")
    data_to_save = {
        "store_name": store_name,
        "last_updated": "2025-12-30",
        "total_count": len(items),
        "items": items
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    print(f"✅ [SUCCESS] {store_name} 저장 완료! (총 {len(items)}개)")

# ==========================================
# 1. CU 크롤러 (API 방식)
# ==========================================
async def get_cu_deals():
    api_url = "https://cu.bgfretail.com/event/plusAjax.do"
    final_items = []
    seen_names = set()
    event_configs = [{"code": "23", "label": "1+1"}, {"code": "24", "label": "2+1"}]
    
    for config in event_configs:
        page, event_label = 1, config["label"]
        while page <= 60:
            payload = {"pageIndex": page, "searchCondition": config["code"], "listType": 0}
            resp = requests.post(api_url, data=payload)
            soup = BeautifulSoup(resp.text, 'html.parser')
            prod_elements = soup.find_all("li", class_="prod_list")
            if not prod_elements: break
                
            for prod in prod_elements:
                name = prod.find("div", class_="name").get_text(strip=True)
                if f"{name}_{event_label}" in seen_names: continue
                seen_names.add(f"{name}_{event_label}")
                
                base_price = int(prod.find("div", class_="price").strong.get_text(strip=True).replace(",", ""))
                
                # 가격 구조 계산
                if event_label == "1+1":
                    eff_one, total_buy = base_price // 2, base_price
                else:
                    eff_one, total_buy = (base_price * 2) // 3, base_price * 2

                img_tag = prod.find("img", class_="prod_img")
                img_url = "https:" + img_tag["src"] if img_tag and "src" in img_tag.attrs else ""

                final_items.append({
                    "product_name": name, "base_price": base_price,
                    "effective_one_price": eff_one, "total_purchase_price": total_buy,
                    "event_type": event_label, "unit": "개", "image_url": img_url
                })
            page += 1
            await asyncio.sleep(0.05)
    save_to_db("cu", final_items)

# ==========================================
# 2. GS25 크롤러 (Playwright 방식)
# ==========================================
async def get_gs25_deals():
    url = "http://gs25.gsretail.com/gscvs/ko/products/event-goods"
    api_url = "http://gs25.gsretail.com/gscvs/ko/products/event-goods-search"
    all_items = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        content = await page.content()
        token = content.split('name="CSRFToken" value="')[1].split('"')[0]
        
        events = {"ONE_TO_ONE": "1+1", "TWO_TO_ONE": "2+1"}
        for event_key, event_label in events.items():
            p_num = 1
            while True:
                raw_res = await page.evaluate(f"""
                    async () => {{
                        const r = await fetch('{api_url}', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' }},
                            body: 'pageNum={p_num}&pageSize=50&parameterList={event_key}&CSRFToken={token}'
                        }});
                        return await r.json();
                    }}
                """)
                data = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
                items_list = data.get("results", [])
                if not items_list: break

                for item in items_list:
                    base_price = int(item.get("attPrice") or 0)
                    if event_label == "1+1":
                        eff_one, total_buy = base_price // 2, base_price
                    else:
                        eff_one, total_buy = (base_price * 2) // 3, base_price * 2
                    
                    all_items.append({
                        "product_name": item.get("goodsNm"), "base_price": base_price,
                        "effective_one_price": eff_one, "total_purchase_price": total_buy,
                        "event_type": event_label, "unit": "개", "image_url": item.get("attFileNm", "")
                    })
                p_num += 1
                if len(items_list) < 50: break
        await browser.close()
    save_to_db("gs25", all_items)

async def get_seven_eleven_deals():
    api_url = "https://www.7-eleven.co.kr/product/listMoreAjax.asp"
    all_items = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        # 세븐일레븐은 pTab 1이 1+1, 2가 2+1입니다.
        for p_tab in [1, 2]:
            event_label = "1+1" if p_tab == 1 else "2+1"
            curr_page = 1
            while curr_page <= 50:
                payload = f"intCurrPage={curr_page}&pTab={p_tab}"
                raw_html = await page.evaluate(f"""
                    async () => {{
                        const r = await fetch('{api_url}', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                            body: '{payload}'
                        }});
                        return await r.text();
                    }}
                """)
                if "검색 결과가 없습니다" in raw_html or not raw_html.strip(): break
                
                soup = BeautifulSoup(raw_html, 'html.parser')
                li_elements = soup.find_all("li")
                if not li_elements: break

                for li in li_elements:
                    name_el = li.select_one(".name")
                    price_el = li.select_one(".price")
                    if not name_el or not price_el: continue
                    
                    # 숫자만 추출하여 정가 설정
                    base_price = int("".join(re.findall(r'\d+', price_el.get_text())))
                    
                    # 새로운 가격 구조 계산
                    if event_label == "1+1":
                        eff_one, total_buy = base_price // 2, base_price
                    else: # 2+1
                        eff_one, total_buy = (base_price * 2) // 3, base_price * 2

                    img_el = li.select_one("img")
                    img_url = "https://www.7-eleven.co.kr" + img_el["src"] if img_el else ""

                    all_items.append({
                        "product_name": name_el.get_text(strip=True),
                        "base_price": base_price,
                        "effective_one_price": eff_one,
                        "total_purchase_price": total_buy,
                        "event_type": event_label,
                        "unit": "개",
                        "image_url": img_url
                    })
                curr_page += 1
        await browser.close()
    save_to_db("seven_eleven", all_items)



# ==========================================
# 3. 전체 실행 제어
# ==========================================
async def main():
    print("🚀 [START] 데이터 수집 엔진 가동...")
    
    tasks = [
        ("CU", get_cu_deals()),
        ("GS25", get_gs25_deals()),
        ("세븐일레븐", get_seven_eleven_deals()),
        ("이마트24", get_emart24_deals())
    ]
    
    for name, task in tasks:
        try:
            print(f"📡 {name} 데이터 가져오는 중...")
            await task
        except Exception as e:
            print(f"❌ {name} 수집 중 오류 발생: {e}")

    print("✨ [FINISHED] 모든 편의점 데이터가 업데이트 되었습니다.")

if __name__ == "__main__":
    asyncio.run(main())