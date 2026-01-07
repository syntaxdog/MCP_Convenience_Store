import os
import json
import sys
import io
import re
import asyncio
from fastmcp import FastMCP
from dotenv import load_dotenv

# manager.py에서 공통 로직 및 Gemini 설정 임포트
from manager import model, load_all_data, GEMINI_API_KEY
from manager import load_all_data, load_tag_candidates

# UTF-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')

# ==========================================
# 1. 초기화 및 설정
# ==========================================
load_dotenv()
mcp = FastMCP("Convenience Store Smart Bot")
DB_DIR = os.path.join(os.path.dirname(__file__), "db")
store_display_names = {
            "emart": "대형마트 이마트",
            "gs_the_fresh": "기업형 슈퍼마켓(SSM) GS더프레시",
            "cu": "편의점 CU",
            "gs25": "편의점 GS25",
            "seven_eleven" : "편의점 세븐일레븐"
        }

# ==========================================
# 2. 유틸리티 함수 (내부 로직)
# ==========================================

def ensure_string_list(data):
    """검색어 리스트 정규화"""
    if isinstance(data, list):
        return [str(i).lower().strip() for i in data if i]
    if isinstance(data, str):
        return [data.lower().strip()]
    return []

def get_safe_str(field):
    """필드 데이터를 안전하게 문자열로 변환"""
    if isinstance(field, list):
        return " ".join(str(i) for i in field if i)
    return str(field) if field else ""

@mcp.tool()
def get_available_tags() -> str:
    """
    검색에 사용 가능한 태그 목록을 반환합니다.
    recommend_smart_snacks 호출 전에 이 목록에서 선택하세요.
    """
    candidates = load_tag_candidates()
    return json.dumps(candidates, ensure_ascii=False, indent=2)

# ==========================================
# 3. 사용자 공개 도구 (AI 호출용)
# ==========================================

@mcp.tool()
async def recommend_smart_snacks(
    keywords: list[str],
    category: str = None,
    situation_tags: list[str] = None,
    taste_tags: list[str] = None,
    preferred_store: str = None
) -> str:
    """
    [실시간 편의점 행사 기반 스마트 추천]
    
    ⚠️ 호출 전 get_available_tags()로 태그 목록을 확인하고 선택하세요.

    Args:
        keywords: 검색 키워드 + 브랜드/동의어 포함 (예: ["라면", "신라면", "컵라면"])
        category: 상품 카테고리 - get_available_tags()의 category에서 선택 (⭐ 필수 권장 - 정확한 결과를 위해 반드시 선택)
        situation_tags: 상황 태그 - get_available_tags()의 situation에서 선택
        taste_tags: 맛 태그 - get_available_tags()의 taste에서 선택
        preferred_store: 선호 매장 - "cu", "gs25", "emart", "seven_eleven" 중 하나

    Returns:
        매칭된 상품 리스트 JSON
    """
    all_items = []
    stores = ["cu", "gs25", "seven_eleven", "emart"]

    # 1. 매장 필터링
    if preferred_store:
        target = preferred_store.lower().replace(" ", "")
        stores = [s for s in stores if target in s]

    # 2. 데이터 로드
    for store in stores:
        file_path = os.path.join(DB_DIR, f"db_{store}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(DB_DIR, f"db_{store}.json")
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data.get("items", []):
                    item["store"] = store.upper()
                    all_items.append(item)
        except Exception as e:
            print(f"Error loading {store}: {e}")

    if not all_items:
        return json.dumps({"error": "데이터 로드 실패", "results": []}, ensure_ascii=False)

    # 3. 카테고리 필터링 (먼저 적용 - 성능 향상)
    if category:
        all_items = [item for item in all_items if item.get("category", "").lower() == category.lower()]

    # 4. 검색 준비
    search_keywords = [k.lower().strip() for k in keywords if k]
    search_situations = [s.lower().strip() for s in (situation_tags or []) if s]
    search_tastes = [t.lower().strip() for t in (taste_tags or []) if t]

    # 5. 스코어링
    scored_results = []

    for item in all_items:
        score = 0
        p_name = (item.get("product_name") or "").lower()
        item_situation = (item.get("situation") or "").lower()
        item_taste = (item.get("taste") or "").lower()

        # 키워드 매칭 (상품명)
        for kw in search_keywords:
            if kw in p_name:
                score += 15

        # situation 매칭
        for sit in search_situations:
            if sit in item_situation:
                score += 12

        # taste 매칭
        for taste in search_tastes:
            if taste in item_taste:
                score += 12

        if score >= 10:
            sort_price = (
                item.get("price_per_unit") or
                item.get("effective_unit_price") or
                item.get("sale_price") or
                99999
            )
            if isinstance(sort_price, str):
                sort_price = int(re.sub(r"[^0-9]", "", sort_price) or 99999)

            item["_score"] = score
            item["_sort_price"] = sort_price
            scored_results.append(item)

    # 6. 정렬
    scored_results.sort(key=lambda x: (-x["_score"], x["_sort_price"]))

    # 7. 중복 제거 + 매장 다양성
    seen_products = set()
    store_count = {}
    final_results = []
    MAX_PER_STORE = 3

    for item in scored_results:
        name_key = item["product_name"].replace(" ", "").lower()
        store = item.get("store", "")

        if name_key in seen_products:
            continue
        if store_count.get(store, 0) >= MAX_PER_STORE:
            continue

        seen_products.add(name_key)
        store_count[store] = store_count.get(store, 0) + 1

        condition = item.get("discount_condition", "")
        final_results.append({
            "product_name": item.get("product_name"),
            "store": store,
            "discount_condition": condition,
            "pay_price": item.get("sale_price"),
            "get_count": 2 if "1+1" in condition else 3 if "2+1" in condition else 1,
            "price_per_one": item.get("effective_unit_price") or item.get("unit_effective_unit_price"),
            "category": item.get("category"),
            "image_url": item.get("image_url")
        })

        if len(final_results) >= 10:
            break

    return json.dumps({
        "query": {
            "keywords": keywords,
            "category": category,
            "situation_tags": situation_tags,
            "taste_tags": taste_tags,
            "store": preferred_store
        },
        "total_matched": len(scored_results),
        "results": final_results
    }, ensure_ascii=False, indent=2)

@mcp.tool()
async def find_best_price(keywords: list[str]) -> str:
    """
    [검색 및 최저가 비교 전용] 
    특정 상품명(예: '불닭볶음면 봉지', '코카콜라 500ml')을 입력받아 현재 가장 저렴하게 판매 중인 매장 정보를 찾습니다.
    사용자가 구체적인 상품을 언급하며 최저가를 물을 때 사용하세요.

    - keywords: 검색 정확도를 높이기 위해 AI가 생성한 연관 단어 리스트
    """
    product_keyword = keywords[0] if isinstance(keywords, list) else keywords

    # 1. 의도 분석 (매장 필터링 및 핵심 키워드 분리)
    analysis_prompt = f"""
    사용자 검색어: "{product_keyword}"
    분석 항목:
    - target_store: 언급된 매장 (CU, GS25, EMART, SEVEN_ELEVEN 등 / 없으면 null)
    - clean_keyword: 매장명을 제외한 순수 상품 검색어
    - specs: 제로, 무설탕, 대용량 등 특징
    형식: JSON
    """
    
    intent_res = await asyncio.to_thread(model.generate_content, analysis_prompt)
    intent = json.loads(intent_res.text.replace("```json", "").replace("```", ""))
    
    target_store = intent.get('target_store')
    clean_query = intent.get('clean_keyword', product_keyword)
    search_terms = keywords if isinstance(keywords, list) else [clean_query]

    # 2. 통합 DB 로드 및 필터링
    all_matched_items = []
    # 검색할 전체 스토어 목록 (확장된 리스트)
    available_stores = ["cu", "emart", "gs_the_fresh", "gs25", "seven_eleven"] 
    
    for store_id in available_stores:
        # 사용자가 특정 매장을 지정했다면 해당 매장만 검색 (유연한 필터)
        if target_store and target_store.lower() not in store_id.lower():
            continue
            
        file_path = os.path.join(DB_DIR, f"db_{store_id}.json")
        enriched_path = os.path.join(DB_DIR, f"db_{store_id}_with_tags.json")
        target_path = enriched_path if os.path.exists(enriched_path) else file_path
        
        if not os.path.exists(target_path): continue

        display_name = store_display_names.get(store_id, store_id)
            
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data.get("items") if isinstance(data, dict) else data
                if not isinstance(items, list): continue
                
                for item in items:
                    p_name_clean = item.get("product_name", "").lower().replace(" ", "")
                    tags_clean = f"{item.get('brand','')} {item.get('category','')} {item.get('taste','')} {item.get('situation','')}".lower().replace(" ", "")
                    
                    match_score = 0
                    clean_search_terms = [term.replace(" ", "").lower() for term in search_terms]
                    
                    # --- [핵심 수정: 가중치 기반 스코어링] ---
                    for i, term in enumerate(clean_search_terms):
                        if term in p_name_clean:
                            if i == 0:
                                # 1순위 키워드(사용자 직접 입력) 매칭 시 압도적 점수
                                match_score += 100 
                            else:
                                # 유사어 매칭 시 보조 점수 (후보군 유지용)
                                match_score += 20 
                    
                    # B. 태그 매칭 가산점 (기존 유지)
                    if any(term in tags_clean for term in clean_search_terms):
                        match_score += 10

                    # --- [결과 처리: 기존 로직 유지] ---
                    # match_score가 100점 이상이면 1순위 키워드가 포함된 것이므로 확실히 필터 통과
                    if match_score >= 50:
                        display_name = store_display_names.get(store_id, store_id.upper())
                        item["match_score"] = match_score
                        item["store_name"] = display_name
                        item["sort_price"] = item.get("price_per_unit") or item.get("effective_unit_price") or 99999
                        all_matched_items.append(item)  
                        
        except Exception as e:
            print(f"Error reading {store_id}: {e}")

    if not all_matched_items:
        return f"'{product_keyword}'에 대한 행사 정보를 찾지 못했습니다."

    all_matched_items.sort(key=lambda x: (-x["match_score"], x["sort_price"]))

    best = all_matched_items[0]
    ref_label = best.get("price_reference", "개당")
    
    summary = (f"'{product_keyword}'와 가장 유사한 상품 {len(all_matched_items)}개를 찾았습니다. "
               f"{best['store_name']}의 '{best['product_name']}'이 "
               f"{ref_label} {int(best['sort_price']):,}원으로 추천 1순위입니다.")

    return json.dumps({
        "summary": summary,
        "best_deal": best,
        "all_results": all_matched_items[:10] # 상위 10개만 전달
    }, ensure_ascii=False, indent=2)

@mcp.tool()
async def compare_category_top3(keywords: list[str]) -> str:
    """
    상품 카테고리(예: '라면', '음료', '고기')를 입력받아 각 편의점/마트별 가성비 TOP 3 리포트를 생성합니다.
    사용자가 품목군 전체의 가격을 비교하고자 할 때 호출해줘.
    
    - keywords: 검색 정확도를 높이기 위해 AI가 생성한 연관 단어 리스트
    """
    all_data_list = []
    
    # 1. 모든 DB 로드 및 store_id 주입 (파일명 기반 자동 태깅)
    for filename in os.listdir(DB_DIR):
        if filename.endswith(".json"):
            target_store_id = None
            for s_key in store_display_names.keys():
                if s_key in filename.lower():
                    target_store_id = s_key
                    break
            
            if not target_store_id: continue

            try:
                with open(os.path.join(DB_DIR, filename), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data.get("items", []):
                        item["_internal_store_id"] = target_store_id
                    all_data_list.append(data)
            except: continue

    # 2. 결과 저장소 및 검색어 준비
    report_data = {store: [] for store in store_display_names.keys()}
    clean_keywords = [k.replace(" ", "").lower() for k in keywords]
    main_query = clean_keywords[0] # 사용자가 입력한 핵심 단어

    for data in all_data_list:
        for item in data.get("items", []):
            p_name = item.get("product_name", "").lower()
            p_name_no_space = p_name.replace(" ", "")
            cat_name = item.get("category", "").lower()
            s_id = item.get("_internal_store_id")
            
            # --- [핵심: 일반화된 지능형 필터링] ---
            match_score = 0
            
            # 1. 단어 완전 일치 보너스 (노이즈 방지 핵심)
            # '물'이 단독 단어로 있거나, 카테고리명이 검색어와 일치할 때 높은 점수
            if any(k == cat_name or k in p_name.split() for k in clean_keywords):
                match_score += 200 

            # 2. 키워드 포함 점수 (순서에 따른 차등)
            for i, kw in enumerate(clean_keywords):
                if kw in p_name_no_space:
                    # 첫 번째 키워드(메인 의도)일수록 높은 가중치
                    weight = 100 if i == 0 else 30
                    match_score += weight
            
            # 3. 부정 매칭 방어 (일반적 노이즈 단어 패턴 차단)
            # 검색어는 짧은데 상품명은 너무 길고 카테고리가 다르면 감점
            if len(main_query) <= 2 and len(p_name_no_space) > 10:
                if main_query not in cat_name: # 카테고리에 검색어가 없다면 노이즈 확률 높음
                    match_score -= 50

            # --- [결과 처리] ---
            # 점수가 일정 수준(예: 100점) 이상인 것만 '진짜'로 간주
            if match_score >= 100:
                if s_id in report_data:
                    sort_price = item.get("price_per_unit") or item.get("effective_unit_price") or 0
                    if 0 < sort_price < 999999:
                        item["sort_price"] = sort_price
                        item["match_score"] = match_score
                        report_data[s_id].append(item)

    # 4. 정렬 및 후보군 추출
    final_payload = {}
    for s_id, items in report_data.items():
        if items:
            # 1순위: 연관 점수(진짜 상품인가?), 2순위: 가성비
            final_payload[s_id] = sorted(items, key=lambda x: (-x["match_score"], x["sort_price"]))[:10]

    return json.dumps(final_payload, ensure_ascii=False)

if __name__ == "__main__":
    mcp.run()