import os, json, sys, io, shutil, re
from bs4 import BeautifulSoup
import requests
import asyncio
from playwright.async_api import async_playwright
from fastmcp import FastMCP
from google import genai
from google.genai import types
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import PydanticOutputParser
from typing import List, Optional
from pydantic import BaseModel, Field

sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8')

# ==========================================
# 1. API 키 설정 (본인의 OpenAI 키로 교체 필수!)
# ==========================================
GEMINI_API_KEY = "API_KEY"
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-3-flash-preview"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# 2. 데이터 구조 정의 (AI가 채워야 할 정답지)
# ==========================================
class PromotionItem(BaseModel):
    product_name: str = Field(description="상품명")
    final_price: int = Field(description="표시된 최종 판매가 (숫자만)")
    # --- 새로 추가할 필드 ---
    unit_price: int = Field(description="1개당 실질 구매 가격 (1+1이면 final_price의 절반)")
    # -----------------------
    original_price: int = Field(description="정상가")
    discount_condition: str = Field(description="할인 조건 (예: 1+1, 2개 구매시 50% 등)")
    unit: str = Field(description="판매 단위")

class StoreFlyerAnalysis(BaseModel):
    store_name: str = Field(description="편의점/마트 이름")
    items: List[PromotionItem] = Field(description="행사 상품 목록")
    summary: str = Field(description="전체 행사 요약 (3줄 이내)")

# ==========================================
# 3. 서버 및 LangChain 설정
# ==========================================
mcp = FastMCP("Convenience Store Vision Bot")

# 파서 설정 (Pydantic 모델을 기반으로 자동 파싱)
parser = PydanticOutputParser(pydantic_object=StoreFlyerAnalysis)

def save_to_db(store_name: str, items: list):
    """수집된 상품 리스트를 로컬 JSON 파일로 저장합니다."""
    # 파일명을 db_cu.json, db_emart.json 식으로 만듭니다.
    file_path = f"db_{store_name.lower()}.json"
    
    data_to_save = {
        "store_name": store_name,
        "last_updated": "2025-11-20", # 날짜를 하드코딩하거나 datetime을 쓰세요
        "total_count": len(items),
        "items": items
    }
    
    with open(file_path, "w", encoding="utf-8") as f:
        # indent=2를 주면 메모장으로 열었을 때 예쁘게 보입니다.
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    
    print(f"[SUCCESS] {file_path} 저장 완료! (총 {len(items)}개)")

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

def normalize_to_list(data):
        """데이터가 무엇이든 '소문자 문자열 리스트'로 변환하는 방어 함수"""
        if isinstance(data, list):
            # 리스트 내부 요소들을 모두 문자열로 바꾸고 소문자화 (None 등 방어)
            return [str(i).lower().strip() for i in data if i]
        if isinstance(data, str):
            # 단일 문자열이면 리스트로 감싸고 소문자화
            return [data.lower().strip()]
        return []

async def analyze_text_with_llm(mart_name: str, raw_text: str) -> str:
    """수집된 텍스트를 분석하여 반드시 'items' 키를 포함한 JSON을 반환하도록 강제합니다."""
    
    # Pydantic 파서의 지시사항을 포함하여 형식을 강제합니다.
    format_instructions = parser.get_format_instructions()
    
    prompt_text = f"""
    당신은 {mart_name}의 전단지 데이터 정리 전문가입니다. 
    주어진 텍스트에서 상품 기본 정보를 추출하여 JSON으로 정리하세요.
    
    [중요: 이미지 URL 처리]
    - 입력 텍스트에 있는 이미지 주소(http...)를 'image_url' 필드에 그대로 넣으세요.
    - 없으면 빈 문자열("")로 두세요.
    
    [필수 구조]
    {{
      "store_name": "{mart_name}",
      "items": [
        {{
          "product_name": "상품명",
          "final_price": 10000,
          "original_price": 12000,
          "discount_condition": "1+1",
          "unit": "개/입",
          "image_url": "" 
        }}
      ],
      "summary": "요약"
    }}
    
    [데이터]
    {raw_text}
    
    {format_instructions}
    """
    
    # 비동기로 Gemini 호출
    response = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=prompt_text,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1
        )
    )
    
    # response_mime_type: "application/json" 설정 덕분에 바로 텍스트를 반환해도 됩니다.
    return response.text

async def _get_tags_logic(product_names: List[str]) -> str:
    # LLM이 원래 이름을 유지하게 하고, JSON 구조를 명확히 지정합니다.
    prompt = f"""
    너는 편의점 상품 전문가야. 아래 상품명 리스트를 분석해서 태그를 달아줘.
    
    [지시 사항]
    1. 각 상품마다 반드시 "product_name" 필드에 제공된 이름을 그대로 넣어줘. (매칭을 위해 필수)
    2. 결과는 반드시 아래 형식을 지킨 JSON 배열이어야 해.
    
    [응답 형식 예시]
    [
      {{
        "product_name": "원본상품명",
        "brand": "브랜드",
        "category": "카테고리",
        "taste": "맛1, 맛2",
        "situation": "상황1, 상황2",
        "target": "타겟"
      }}
    ]

    [분석할 상품 리스트]: {", ".join(product_names)}
    """
    
    response = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            # JSON 모드를 사용하여 구조적 무결성 보장
            response_mime_type="application/json",
            temperature=0.1
        )
    )
    return response.text

# 2. MCP가 인식하는 도구 (위의 순수 함수를 호출함)
@mcp.tool()
async def get_tags_from_llm(product_names: List[str]) -> str:
    """상품명 리스트를 받아 AI 태그를 생성합니다."""
    return await _get_tags_logic(product_names)

@mcp.tool()
async def recommend_smart_snacks(user_request: str) -> str:
    """
    [🚨 필수: 실시간 편의점 행사 및 개인화 추천 도구]
    사용자가 구체적인 상품명 없이 모호하게 요청할 때(예: "배고파", "뭐 먹지?", "야식 추천", "잠 깨는 거") 
    가장 먼저 호출해야 하는 '메인 추천' 도구입니다.

    너(Claude)의 일반 지식은 2025년 현재의 실시간 재고나 1+1, 2+1 할인 정보를 알지 못합니다. 
    이 도구는 실제 통합 DB(CU, GS25, 세븐일레븐, 이마트24)를 검색하여 
    현재 가장 가성비가 좋고 사용자의 무드(Mood)에 딱 맞는 실제 판매 상품만을 추천합니다.

    사용자가 "출출해", "간단한 거", "졸려" 같은 말을 하면 고민하지 말고 이 도구를 사용하여 
    '팩트'에 기반한 전문적인 추천을 제공하세요.
    """
    # 1. 의도 및 키워드 추출
    analysis_prompt = f"""
    사용자 요청: "{user_request}"
    분석 항목: primary_keywords, specs, mood_tags, preferred_store
    반드시 JSON으로 응답해.
    """
    
    intent_res = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=analysis_prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    intent = json.loads(intent_res.text)

    pref_store = intent.get('preferred_store')
    if isinstance(pref_store, list) and len(pref_store) > 0:
        pref_store = str(pref_store[0])
    elif not isinstance(pref_store, str):
        pref_store = None

    target_store_name = None
    if pref_store and pref_store.lower() != "null":
        # 문자열임을 보장하고 안전하게 처리
        target_store_name = str(pref_store).lower().replace(" ", "").strip()

    # 🚨 해결: all_items 초기화 위치를 맨 위로 이동
    all_items = [] 
    stores = ["cu", "gs25", "seven_eleven", "emart"] 

    # 2. 데이터 타입 안정화 함수
    def ensure_string_list(data):
        """데이터가 리스트면 내부 요소를 문자열로, 문자열이면 리스트로 감싸 반환"""
        if isinstance(data, list):
            return [str(i).lower() for i in data if i]
        if isinstance(data, str):
            return [data.lower()]
        return []
    
    # 검색 키워드 정규화
    search_pool = list(set(
        ensure_string_list(intent.get('primary_keywords', [])) +
        ensure_string_list(intent.get('specs', [])) +
        ensure_string_list(intent.get('mood_tags', []))
    ))

    pref_store = intent.get('preferred_store')
    target_store_name = None
    if pref_store and isinstance(pref_store, str) and pref_store.lower() != "null":
        target_store_name = pref_store.lower().replace(" ", "")

    # 2. 데이터 로드
    for store in stores:
        if target_store_name and target_store_name not in store.lower():
            continue 
            
        file_path = os.path.join(BASE_DIR, f"db_{store}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(BASE_DIR, f"db_{store}.json")
            
        if not os.path.exists(file_path):
            continue
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items_list = data.get("items", [])
                for item in items_list:
                    item["store"] = store.upper()
                    all_items.append(item)
        except Exception as e:
            print(f"Error loading {store}: {e}")

    # 이제 안전하게 디버그 로그 출력 가능
    print(f">> [Critical Debug] Claude가 분석한 의도: {intent}")
    print(f">> [Critical Debug] 로드된 전체 상품 수: {len(all_items)}")

    if not all_items:
        return "죄송합니다. 현재 편의점 데이터 파일을 읽어올 수 없습니다."

    # 3. 스코어링 시스템
    scored_results = []
    
    # [에러 해결 핵심] 모든 키워드를 안전하게 문자열 리스트로 통합
    primary = ensure_string_list(intent.get('primary_keywords', []))
    specs = ensure_string_list(intent.get('specs', []))
    moods = ensure_string_list(intent.get('mood_tags', []))

    search_pool = list(set(primary + specs + moods)) # 중복 제거 및 통합
    print(f">> [Debug] 정규화된 키워드 풀: {search_pool}")

    for item in all_items:
        score = 0
        p_name = item.get("product_name", "").lower()
        
        # 태그 데이터 안전하게 병합 (이전 에러 방지 포함)
        def get_safe_tags(field):
            if isinstance(field, list):
                return " ".join(str(i) for i in field if i)
            return str(field) if field else ""
        
        category = item.get('category', '') or ''
        taste = get_safe_tags(item.get('taste', []))
        situation = get_safe_tags(item.get('situation', []))
        
        tags_text = f"{category} {taste} {situation}".lower()

        for kw in search_pool:
            # kw는 이미 ensure_string_list에서 lower() 처리가 된 문자열임이 보장됨
            if kw in p_name:
                score += 15
            elif kw in tags_text:
                score += 12
            elif len(kw) >= 2 and (kw[:2] in p_name or kw[:2] in tags_text):
                score += 3


        if score >= 5: 
            scored_results.append((score, item))
            
    scored_results.sort(key=lambda x: x[0], reverse=True)
    top_matches = [x[1] for x in scored_results[:5]]

    if not top_matches:
        return f"'{user_request}'에 맞는 상품을 찾지 못했어요."

    # 4. 최종 추천 메시지 생성 (RAG)
    rag_prompt = f"""
    사용자 질문: {user_request}
    상품 데이터: {json.dumps(top_matches, ensure_ascii=False)}
    위 데이터를 바탕으로 친절하게 추천해줘.
    """
    
    rag_res = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=rag_prompt
    )

    # 5. Claude 개입 차단용 2차 래핑
    final_prompt = f"""
    [강제 지침] 아래 내용을 수정하지 말고 그대로 출력해라.
    {rag_res.text}
    """
    
    final_res = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=final_prompt
    )
    
    return f"[FINAL_RESULT]\n{final_res.text}"

@mcp.tool()
async def enrich_db_with_tags_high_speed(store_name: str):
    """비동기 병렬 처리를 통해 수천 개의 상품을 초고속으로 태깅합니다."""
    # store_name 인자를 그대로 사용하도록 수정 (하드코딩 제거)
    file_path = f"db_{store_name.lower()}.json"

    if not os.path.exists(file_path): return f"[{store_name}] 파일이 존재하지 않습니다."

    with open(file_path, "r", encoding="utf-8") as f:
        db_data = json.load(f)
    items = db_data.get("items", [])
    
    # 1. 태깅 대상 추출 (이미 category가 있는 상품은 제외)
    # 중복 제거를 위해 set 사용 후 리스트 변환
    to_tag_names = list(set([item["product_name"] for item in items if "category" not in item]))
    
    if not to_tag_names: 
        return f"{store_name} DB는 이미 100% 태깅이 완료된 상태입니다."

    print(f"🚀 [병렬 분석 시작] 대상 상품: {len(to_tag_names)}개")

    chunk_size = 100 # 한 번에 50개씩 묶음
    chunks = [to_tag_names[i:i + chunk_size] for i in range(0, len(to_tag_names), chunk_size)]
    semaphore = asyncio.Semaphore(15) # 동시 요청 5개 제한

    async def process_chunk(chunk):
        async with semaphore:
            try:
                # 🔴 중요: 반드시 내부 로직 함수(_get_tags_logic)를 호출해야 함
                res_json = await _get_tags_logic(chunk)
                return json.loads(res_json)
            except Exception as e:
                print(f"배치 처리 에러: {e}")
                return []

    # 2. 병렬 실행 및 결과 취합
    tasks = [process_chunk(c) for c in chunks]
    all_results = await asyncio.gather(*tasks)

    # 3. 통합 결과 라이브러리 생성 (키값을 클리닝하여 저장)
    tagged_library = {}
    for chunk_res in all_results:
        if not isinstance(chunk_res, list): continue
        for res_item in chunk_res:
            # LLM 응답에서 이름을 가져옴
            p_name = res_item.get("product_name") or res_item.get("name")
            if p_name:
                # [매칭 핵심] 공백 제거하여 저장
                match_key = str(p_name).replace(" ", "").strip().lower()
                tagged_library[match_key] = res_item

    # 4. 원본 데이터에 병합
    updated_count = 0
    for item in items:
        name = item.get("product_name", "")
        # [매칭 핵심] 찾을 때도 공백 제거
        current_key = str(name).replace(" ", "").strip().lower()
        
        # 이미 category가 있어도 데이터가 부실하면 갱신하도록 조건 완화
        has_no_tag = "category" not in item or not item["category"] or item["category"] == "미분류"
        
        if has_no_tag and current_key in tagged_library:
            info = tagged_library[current_key]
            item.update({
                "category": info.get("category", "미분류"),
                "taste": info.get("taste", []),
                "situation": info.get("situation", []),
                "target": info.get("target", "전체")
            })
            updated_count += 1
            
        # 정규화 로직 (항상 수행)
        item = normalize_product_data(item)

    # [중요] 수정된 items를 본체에 다시 할당
    db_data["items"] = items
    enriched_file_path = os.path.join(BASE_DIR, f"db_{store_name.lower()}_with_tags.json")

    with open(enriched_file_path, "w", encoding="utf-8") as f:
        json.dump(db_data, f, ensure_ascii=False, indent=2)

    return f"{store_name} 고속 업데이트 완료! {updated_count}개의 새로운 상품 태그가 추가되었습니다."

# --- [도구 1] GS 더프레시 크롤러 ---
@mcp.tool()
async def get_gs_the_fresh_deals() -> str:
    """
    Playwright와 Gemini 병렬 처리를 사용하여 
    GS 더프레시 전단지 데이터를 초고속으로 추출하고 저장합니다.
    """
    url = "https://web.gsretail.me/Viewer/gsp2/"
    
    async with async_playwright() as p:
        # 브라우저 실행
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            # 1. 페이지 이동
            await page.goto(url, wait_until="networkidle")
            
            # 2. 로딩 대기
            await asyncio.sleep(2) 
            await page.wait_for_selector("img.pageImage", timeout=20000)            
            
            # 3. 데이터 추출 (aria-label 활용)
            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            images = soup.find_all("img", class_="pageImage")
            raw_texts = [img.get("aria-label") for img in images if img.get("aria-label")]
            
            if not raw_texts:
                await browser.close()
                return "데이터 추출에 실패했습니다. (aria-label이 비어있음)"

            full_text = "\n\n".join(raw_texts)
            lines = [line.strip() for line in full_text.split('\n') if line.strip()]
            
            # 4. 병렬 분석 준비 (Chunking & 병렬 호출)
            chunk_size = 15 
            chunks = ["\n".join(lines[i:i + chunk_size]) for i in range(0, len(lines), chunk_size)]
            
            # 병렬 처리 실행
            tasks = [analyze_text_with_llm("GS The Fresh", chunk) for chunk in chunks]
            chunk_results_json = await asyncio.gather(*tasks)
            
            all_extracted_items = []
            
            for res_json in chunk_results_json:
                try:
                    data = json.loads(res_json)
                    # 'items' 키가 있는지 확인하고 리스트에 추가
                    if isinstance(data, dict) and "items" in data:
                        all_extracted_items.extend(data["items"])
                    # 만약 Gemini가 리스트 자체를 반환했을 경우에 대한 예외 처리
                    elif isinstance(data, list):
                        all_extracted_items.extend(data)
                except Exception as e:
                    print(f"파싱 에러: {e}")
                    continue

            # 6. 최종 결과 구성
            final_output = {
                "store_name": "GS The Fresh",
                "items": all_extracted_items,
                "summary": f"총 {len(all_extracted_items)}개의 상품 정보를 추출했습니다."
            }
            
            # 브라우저 닫기 및 DB 저장
            await browser.close()
            save_to_db("gs_the_fresh", all_extracted_items)
            
            return json.dumps(final_output, ensure_ascii=False, indent=2)
            
        except Exception as e:
            await browser.close()
            return f"비동기 수집 에러: {str(e)}"

# --- [도구 2] 이마트 크롤러 ---
@mcp.tool()
async def get_emart_deals() -> str:
    """공통 분석 함수를 사용하여 이마트 데이터를 전수 조사합니다."""
    url = "https://store.emart.com/news/leafletfull.do?division=2"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        resp = requests.get(url, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        hidden_divs = soup.find_all("div", class_="hide")
        
        # 1. 모든 div.hide의 텍스트를 하나로 합침
        all_text = ""
        for div in hidden_divs:
            all_text += div.get_text(separator="\n").strip() + "\n"
        
        lines = [l.strip() for l in all_text.split('\n') if l.strip()]
        
        total_items = []
        chunk_size = 35 # gpt-4o-mini에 최적화된 크기
        
        # 2. 청킹 루프
        tasks = []
        for i in range(0, len(lines), chunk_size):
            chunk = "\n".join(lines[i : i + chunk_size])
            # 실행하지 않고 예약(task)만 걸어둡니다.
            tasks.append(analyze_text_with_llm("Emart", chunk))

        # 모든 조각 분석을 동시에 실행하고 기다립니다.
        results = await asyncio.gather(*tasks)

        for result_json in results:
            data = json.loads(result_json)
            total_items.extend(data.get("items", []))

        # 3. 모든 조각이 합쳐진 최종 데이터 구성
        final_output = {
            "store_name": "이마트",
            "items": total_items,
            "summary": f"이마트 전단지에서 총 {len(total_items)}개의 상품을 성공적으로 추출했습니다."
        }
        save_to_db("emart", final_output)
        return json.dumps(final_output, ensure_ascii=False, indent=2)

    except Exception as e:
        return f"이마트 크롤링 에러: {str(e)}"

# --- [도구 3] cu 크롤러 ---
@mcp.tool()
async def get_cu_deals_api() -> str:
    """사용자가 직접 찾아낸 searchCondition(23: 1+1, 24: 2+1)을 적용한 코드입니다."""
    api_url = "https://cu.bgfretail.com/event/plusAjax.do"
    final_items = []
    seen_names = set() # 중복 수집 방지
    debug_logs = []
    
    # 23: 1+1 행사, 24: 2+1 행사
    event_configs = [
        {"code": "23", "label": "1+1"},
        {"code": "24", "label": "2+1"}
    ]
    
    for config in event_configs:
        page = 1
        event_code = config["code"]
        event_label = config["label"]
        
        while page <= 60:
            # 관찰하신 대로 searchCondition에 행사 코드를 넣습니다.
            payload = {
                "pageIndex": page,
                "listType": 0, # 타입을 0으로 두거나 1로 두어도 searchCondition이 우선할 것입니다.
                "searchCondition": event_code, 
                "user_id": ""
            }
            
            response = requests.post(api_url, data=payload)
            soup = BeautifulSoup(response.text, 'html.parser')
            prod_elements = soup.find_all("li", class_="prod_list")
            
            if not prod_elements:
                debug_logs.append(f"{event_label} 종료: {page-1}페이지")
                break
                
            new_items_in_page = 0
            for prod in prod_elements:
                name = prod.find("div", class_="name").get_text(strip=True)
                
                # 상품이 이미 중복되었다면 건너뜁니다 (단, 행사 타입이 다르면 다른 상품으로 간주할지 결정 필요)
                # 여기서는 '상품명 + 행사' 조합을 고유 키로 사용하여 중복을 막습니다.
                unique_key = f"{name}_{event_label}"
                
                if unique_key not in seen_names:
                    seen_names.add(unique_key)
                    new_items_in_page += 1
                    
                    price_text = prod.find("div", class_="price").strong.get_text(strip=True).replace(",", "")
                    price = int(price_text)
                    
                    # 단가 계산 (1+1은 1/2, 2+1은 2/3)
                    unit_price = price // 2 if event_label == "1+1" else (price * 2) // 3

                    # [추가됨] 이미지 URL 추출 로직
                    image_url = ""
                    try:
                        # img 태그 중 class가 prod_img인 것을 찾음
                        img_tag = prod.find("img", class_="prod_img")
                        if img_tag and "src" in img_tag.attrs:
                            raw_src = img_tag["src"]
                            # //로 시작하면 https:를 붙여줌
                            if raw_src.startswith("//"):
                                image_url = "https:" + raw_src
                            else:
                                image_url = raw_src
                    except Exception as e:
                        print(f"이미지 추출 실패 ({name}): {e}")

                    final_items.append({
                        "product_name": name,
                        "final_price": price,
                        "unit_price": unit_price,
                        "discount_condition": event_label,
                        "unit": "개",
                        "image_url": image_url  # 추출한 URL 저장
                    })
            
            # 한 페이지(40개)가 모두 중복이면 서버가 마지막 페이지를 반복하는 것이므로 탈출
            if new_items_in_page == 0:
                debug_logs.append(f"{event_label} 중복 중단: {page}페이지")
                break
                
            page += 1
            await asyncio.sleep(0.05)
    
    save_to_db("cu", final_items)

    return json.dumps({
            "total_count": len(final_items),
            "debug_info": debug_logs,
            "items": final_items
    }, ensure_ascii=False, indent=2)

# --- [도구 4] gs25 크롤러 ---
@mcp.tool()
async def get_gs25_deals_refined() -> str:
    """덤증정을 제외하고 1+1, 2+1 행사 상품만 1,600개 이상 전수 수집합니다."""
    url = "http://gs25.gsretail.com/gscvs/ko/products/event-goods"
    api_url = "http://gs25.gsretail.com/gscvs/ko/products/event-goods-search"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.goto(url, wait_until="networkidle")
            content = await page.content()
            
            # 토큰 추출
            token_marker = 'name="CSRFToken" value="'
            start = content.find(token_marker) + len(token_marker)
            token = content[start:content.find('"', start)]
            
            all_items = []
            # 'GIFT'(덤증정)를 제거하고 가격 혜택이 명확한 항목만 구성
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
                        name = item.get("goodsNm", "이름없음")
                        price = item.get("attPrice") or item.get("price") or 0
                        image_url = item.get("attFileNm", "")
                        if not image_url:
                            image_url = item.get("attFileNmOld", "")
                        
                        if isinstance(price, str):
                            price = int("".join([c for c in price if c.isdigit()]))
                        
                        unit = "개"
                        match = re.search(r'\(([^)]+)\)|(\d+[gGkKmLl입봉팩캔병])', name)
                        if match:
                            unit = match.group(0).strip('()')

                        all_items.append({
                            "product_name": name,
                            "final_price": price,
                            "unit_price": price // 2 if event_key == "ONE_TO_ONE" else (price * 2) // 3,
                            "discount_condition": event_name,
                            "unit": unit,
                            "image_url" : image_url
                        })
                    
                    p_num += 1
                    if len(items_list) < 50: break # 마지막 페이지 판정

            if all_items:
                save_to_db("gs25", all_items)
                await browser.close()
                return f"GS25 정밀 수집 완료: 총 {len(all_items)}개 상품 확보 (덤증정 제외)"
            
            await browser.close()
            return "데이터를 가져오지 못했습니다."

        except Exception as e:
            await browser.close()
            return f"수집 중 중단: {str(e)}"

# --- [도구 5] 세븐일레븐 크롤러 ---
@mcp.tool()
async def get_seven_eleven_refined_all() -> str:
    """빈 데이터를 걸러내고 세븐일레븐의 1+1, 2+1 상품을 정밀 수집합니다."""
    base_url = "https://www.7-eleven.co.kr/product/presentList.asp"
    api_url = "https://www.7-eleven.co.kr/product/listMoreAjax.asp"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()
        
        try:
            await page.goto(base_url, wait_until="networkidle")
            
            all_items = []
            tab_map = {1: "1+1", 2: "2+1", 4: "할인행사"}

            for p_tab, event_name in tab_map.items():
                curr_page = 1
                # 타임아웃 방지를 위해 한 번의 호출당 최대 페이지 수를 제한하거나 
                # 루프 내에서 상태를 자주 보고합니다.
                while curr_page <= 100: # 안전을 위해 최대 페이지 제한
                    payload = f"intCurrPage={curr_page}&intPageSize=10&pTab={p_tab}"
                    
                    raw_html = await page.evaluate(f"""
                        async () => {{
                            const r = await fetch('{api_url}', {{
                                method: 'POST',
                                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' }},
                                body: '{payload}'
                            }});
                            return await r.text();
                        }}
                    """)

                    if not raw_html.strip() or "검색 결과가 없습니다" in raw_html:
                        break

                    soup = BeautifulSoup(raw_html, 'html.parser')
                    li_tags = soup.find_all("li")
                    
                    if not li_tags: break
                    
                    valid_on_page = 0
                    for li in li_tags:
                        name_el = li.select_one(".name")
                        if not name_el or not name_el.get_text(strip=True):
                            continue
                            
                        price_el = li.select_one(".price")
                        if not price_el: continue

                        img_el = li.select_one("img")
                        image_url = ""
                        if img_el and img_el.get("src"):
                            image_url = img_el["src"]
                            # 만약 주소가 상대 경로로 시작한다면 도메인을 붙여줍니다.
                            if image_url.startswith("/"):
                                image_url = f"https://www.7-eleven.co.kr{image_url}"

                        # 1. [핵심 수정] 실제 텍스트 태그(1+1, 2+1) 직접 추출
                        tag_el = li.select_one(".tag_list_01 li")
                        actual_condition = tag_el.get_text(strip=True) if tag_el else event_name
                        
                        # 2. 덤증정 필터링
                        if "덤" in actual_condition or "덤" in li.get_text():
                            continue

                        name = name_el.get_text(strip=True)
                        price_raw = price_el.get_text(strip=True)
                        price = int("".join([c for c in price_raw if c.isdigit()]))

                        unit = "개"
                        match = re.search(r'\(([^)]+)\)|(\d+[gGkKmLl입봉팩캔병])', name)
                        if match:
                            unit = match.group(0).strip('()')

                        orig_price_el = li.select_one(".price_list span")
                        if orig_price_el:
                            original_price = int("".join([c for c in orig_price_el.get_text() if c.isdigit()]))
                        else:
                            # 정가 정보가 없거나 1+1 상품이면 판매가와 정가를 동일하게 처리합니다.
                            original_price = price

                        # 4. 실제 태그 글자를 기준으로 단가 계산 및 이전 가격 포함 저장
                        # 1+1, 2+1이 아닌 '할인' 상품은 판매가(price)를 그대로 단가로 사용합니다.
                        if "1+1" in actual_condition:
                            unit_price = price // 2
                        elif "2+1" in actual_condition:
                            unit_price = (price * 2) // 3
                        else:
                            unit_price = price # 할인행사(pTab=4) 등

                        all_items.append({
                            "product_name": name,
                            "original_price": original_price, # 이전 가격 추가
                            "final_price": price,
                            "unit_price": unit_price,
                            "discount_condition": actual_condition,
                            "unit": unit,
                            "image_url" : image_url
                        })
                        valid_on_page += 1
                    
                    # 해당 페이지에 유효 상품이 하나도 없으면 중단
                    if valid_on_page == 0: break
                    
                    curr_page += 1
                    await asyncio.sleep(0.05) # 서버 부하 조절

            if all_items:
                save_to_db("seven_eleven", all_items)
                await browser.close()
                return f"세븐일레븐 수집 완료: 총 {len(all_items)}개 유효 상품 확보"
            
            await browser.close()
            return "유효한 상품 데이터를 찾지 못했습니다."

        except Exception as e:
            await browser.close()
            return f"수집 중 에러 발생: {str(e)}"

@mcp.tool()
async def find_best_price(product_keyword: str) -> str:
    """
    [검색 및 최저가 비교 전용] 
    사용자가 특정 상품(예: 신라면, 펩시 제로 등)의 가격, 할인 정보, 
    어느 매장이 가장 저렴한지 물어볼 때 '반드시' 이 함수를 호출하세요.
    단순 수집(get_cu_deals_api)과 달리 통합 DB에서 최적의 가성비 상품을 찾아줍니다.
    """
    # 1. 의도 분석 (매장 필터링 및 핵심 키워드 분리)
    analysis_prompt = f"""
    사용자 검색어: "{product_keyword}"
    분석 항목:
    - target_store: 언급된 매장 (CU, GS25, EMART, SEVEN_ELEVEN 등 / 없으면 null)
    - clean_keyword: 매장명을 제외한 순수 상품 검색어
    - specs: 제로, 무설탕, 대용량 등 특징
    형식: JSON
    """
    
    intent_res = await client.aio.models.generate_content(
        model=MODEL_ID,
        contents=analysis_prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    intent = json.loads(intent_res.text)
    
    target_store = intent.get('target_store')
    clean_query = intent.get('clean_keyword', product_keyword)
    specs = intent.get('specs', [])
    search_terms = clean_query.lower().split()

    # 2. 통합 DB 로드 및 필터링
    all_matched_items = []
    # 검색할 전체 스토어 목록 (확장된 리스트)
    available_stores = ["cu", "emart", "gs_the_fresh", "gs25", "seven_eleven"] 
    
    for store_id in available_stores:
        # 사용자가 특정 매장을 지정했다면 해당 매장만 검색 (유연한 필터)
        if target_store and target_store.lower() not in store_id.lower():
            continue
            
        file_path = os.path.join(BASE_DIR, f"db_{store_id}.json")
        enriched_path = os.path.join(BASE_DIR, f"db_{store_id}_with_tags.json")
        target_path = enriched_path if os.path.exists(enriched_path) else file_path
        
        if not os.path.exists(target_path): continue
            
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data.get("items") if isinstance(data, dict) else data
                if not isinstance(items, list): continue
                
                for item in items:
                    p_name = item.get("product_name", "").lower()
                    p_name_no_space = p_name.replace(" ", "")
                    
                    # [중요] 검색 범위를 태그 데이터까지 확장
                    # category, taste(리스트), situation(리스트)를 모두 하나의 문자열로 합침
                    category = item.get('category', '')
                    tastes = " ".join(item.get('taste', [])) if isinstance(item.get('taste'), list) else item.get('taste', '')
                    situations = " ".join(item.get('situation', [])) if isinstance(item.get('situation'), list) else item.get('situation', '')
                    
                    search_target = f"{p_name} {category} {tastes} {situations}".lower()

                    match_score = 0
                    # 검색어 중 하나라도 상품명이나 태그에 포함되면 후보군에 넣음 (유연한 검색)
                    if any(term in search_target or term in p_name_no_space for term in search_terms):
                        # 상품명에 직접 포함되면 높은 점수
                        if all(term in p_name for term in search_terms):
                            match_score += 15
                        else:
                            match_score += 5
                    
                    # 스펙(제로 등) 가산점
                    for spec in specs:
                        if spec.lower() in search_target:
                            match_score += 10
                            
                    if match_score >= 5: # 검색 문턱을 낮추어 더 많은 결과 도출
                        item["source_store"] = store_id.upper().replace("_", " ")
                        all_matched_items.append(item)
                        
        except Exception as e:
            print(f"Error reading {store_id}: {e}")

    if not all_matched_items:
        return f"'{product_keyword}'에 대한 행사 정보를 찾지 못했습니다."

    # 3. 최저가순 정렬 (단가 기준)
    all_matched_items.sort(key=lambda x: x.get("unit_price", 999999))

    # 4. LLM을 통한 결과 요약 생성 (선택 사항 - 더 친절한 응답)
    best = all_matched_items[0]
    summary = f"총 {len(all_matched_items)}개를 찾았고, {best['source_store']}의 {best['product_name']}이(가) 개당 {best['unit_price']}원으로 가장 저렴합니다."

    return json.dumps({
        "summary": summary,
        "best_deal": best,
        "all_results": all_matched_items[:10] # 상위 10개만 전달
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
