"""
편의점 행사 정보 MCP 서버
- CU, GS25, 세븐일레븐 행사 상품 추천
"""
import os
import random
import json
import sys
import io
import re
from fastmcp import FastMCP
from dotenv import load_dotenv
from manager import load_tag_candidates

# UTF-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')

# ==========================================
# 초기화 및 설정
# ==========================================
load_dotenv()
mcp = FastMCP("Pyeon_Ri_Dan")
DB_DIR = os.path.join(os.path.dirname(__file__), "db")

STORE_NAMES = {
    "cu": "CU",
    "gs25": "GS25",
    "seven_eleven": "세븐일레븐"
}

# 태그 후보 미리 로드 (성능 향상)
TAG_CANDIDATES = load_tag_candidates()

def decode_unicode(text: str) -> str:
    """유니코드 이스케이프 디코딩"""
    if not text:
        return text
    try:
        def replace_unicode(match):
            return chr(int(match.group(1), 16))
        return re.sub(r'\\u([0-9a-fA-F]{4})', replace_unicode, text)
    except:
        pass
    return text

# ==========================================
# MCP 도구
# ==========================================

@mcp.tool()
async def find_best_price(
    keywords: list[str],
    preferred_store: str = None
) -> str:
    """
    [특정 상품 최저가 찾기]
    사용자가 지정한 특정 상품(예: 코카콜라, 신라면) 의 최저가를 찾아줍니다.
    단순히 가격이 가장 낮은 곳을 찾을 때 사용하세요.

    ✅ 사용 예시:
    - "코카콜라 최저가 알려줘."
    - "신라면 어디가 제일 싸?"

    Args:
        keywords: 검색할 상품명 키워드 (예: ["코카콜라", "콜라"])
        preferred_store: (선택) 특정 편의점만 검색 ("cu", "gs25", "seven_eleven")
    """
    if not keywords:
        return json.dumps({"error": "키워드를 입력해주세요"}, ensure_ascii=False)
    
    main_keyword = keywords[0]
    search_terms = [term.replace(" ", "").lower() for term in keywords]

    all_matched_items = []
    available_stores = list(STORE_NAMES.keys())
    
    # 매장 필터링
    if preferred_store:
        target = preferred_store.lower().replace(" ", "")
        available_stores = [s for s in available_stores if target in s]
    
    for store_id in available_stores:
        file_path = os.path.join(DB_DIR, f"db_{store_id}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(DB_DIR, f"db_{store_id}.json")
        if not os.path.exists(file_path):
            continue
            
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data.get("items") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    continue
                
                for item in items:
                    p_name_clean = item.get("product_name", "").lower().replace(" ", "")
                    
                    match_score = 0
                    for i, term in enumerate(search_terms):
                        if term in p_name_clean:
                            match_score += 100 if i == 0 else 20
                    
                    if match_score >= 50:
                        display_name = STORE_NAMES.get(store_id, store_id.upper())
                        item["match_score"] = match_score
                        item["store_name"] = display_name
                        raw_price = (
                        item.get("effective_unit_price")
                        )
                        try:
                            price = float(raw_price)
                        except (TypeError, ValueError):
                            price = 99999

                        item["sort_price"] = price
                        all_matched_items.append(item)
                        
        except Exception as e:
            print(f"Error reading {store_id}: {e}")

    if not all_matched_items:
        return json.dumps({
            "error": f"'{main_keyword}'에 대한 행사 정보를 찾지 못했습니다.",
            "results": []
        }, ensure_ascii=False)

    # 1단계: 키워드 점수로 관련성 높은 10개 선별
    all_matched_items.sort(key=lambda x: -x["match_score"])
    top_relevant = all_matched_items[:10]

    # 2단계: 그 10개를 가격으로 정렬
    top_relevant.sort(key=lambda x: x["sort_price"])

    return json.dumps({
        "query": {
            "keywords": keywords,
            "store": preferred_store
        },
        "results": [{
            "product_name": item.get("product_name"),
            "store": item.get("store_name"),
            "discount_condition": item.get("discount_condition"),
            "pay_price": item.get("sale_price"),
            "price_per_one": item.get("effective_unit_price")
        } for item in top_relevant[:10]]
    }, ensure_ascii=False, indent=2)

@mcp.tool()
async def find_best_value(
    keywords: list[str],
    preferred_store: str = None
) -> str:
    """
    [용량 대비 가성비 분석]
    100ml당 단가를 계산하여, 용량 대비 가격 효율이 가장 좋은 상품을 찾습니다.
    사용자가 '가성비', '용량 당 최저' 를 찾을 때 적합합니다.

    ✅ 사용 예시:
    - "칠성 사이다 가성비."
    - "펩시 콜라 용량 당 최저 알려줘."

    Args:
        keywords: 검색할 상품명 키워드 (예: ["우유", "서울우유"])
        preferred_store: (선택) 특정 편의점만 검색
    """
    if not keywords:
        return json.dumps({"error": "키워드를 입력해주세요"}, ensure_ascii=False)

    keywords = [decode_unicode(k) for k in keywords]
    main_keyword = keywords[0]
    search_terms = [term.replace(" ", "").lower() for term in keywords]

    all_matched_items = []
    available_stores = list(STORE_NAMES.keys())

    # 매장 필터링
    if preferred_store:
        target = preferred_store.lower().replace(" ", "")
        available_stores = [s for s in available_stores if target in s]

    for store_id in available_stores:
        file_path = os.path.join(DB_DIR, f"db_{store_id}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(DB_DIR, f"db_{store_id}.json")
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data.get("items") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    continue

                for item in items:
                    p_name_clean = item.get("product_name", "").lower().replace(" ", "")

                    match_score = 0
                    for i, term in enumerate(search_terms):
                        if term in p_name_clean:
                            match_score += 100 if i == 0 else 20

                    if match_score >= 50:
                        # 용량당 가격이 있는 상품만 (안전 파싱)
                        raw_ppu = item.get("price_per_unit")

                        # raw_ppu가 "1,234" 같이 문자열일 수도 있으니 정리
                        if isinstance(raw_ppu, str):
                            raw_ppu = raw_ppu.strip().replace(",", "")
                            raw_ppu = re.sub(r"[^0-9.]", "", raw_ppu)

                        try:
                            ppu = float(raw_ppu)
                        except (TypeError, ValueError):
                            continue  # 파싱 불가면 스킵

                        if ppu <= 0:
                            continue

                        display_name = STORE_NAMES.get(store_id, store_id.upper())
                        item["match_score"] = match_score
                        item["store_name"] = display_name
                        item["sort_price"] = ppu
                        all_matched_items.append(item)

        except Exception as e:
            print(f"Error reading {store_id}: {e}")

    if not all_matched_items:
        return json.dumps({
            "error": f"'{main_keyword}'에 대한 용량 정보가 있는 행사 상품을 찾지 못했습니다.",
            "results": []
        }, ensure_ascii=False)

    # 1단계: 키워드 점수로 관련성 높은 10개 선별
    all_matched_items.sort(key=lambda x: -x["match_score"])
    top_relevant = all_matched_items[:10]

    # 2단계: 용량당 가격(가성비)으로 정렬 (낮을수록 좋음)
    top_relevant.sort(key=lambda x: x["sort_price"])

    return json.dumps({
        "query": {
            "keywords": keywords,
            "store": preferred_store
        },
        "results": [{
            "product_name": item.get("product_name"),
            "store": item.get("store_name"),
            "discount_condition": item.get("discount_condition"),
            "pay_price": item.get("sale_price"),
            "price_per_one": item.get("effective_unit_price"),
            "unit_value": item.get("unit_value"),
            "unit_type": item.get("unit_type"),
            "price_per_unit": item.get("price_per_unit"),
            "price_reference": item.get("price_reference")
        } for item in top_relevant[:10]]
    }, ensure_ascii=False, indent=2)

@mcp.tool()
def get_available_tags() -> dict:
    """
    검색에 사용 가능한 태그 목록을 반환합니다. 꼭 이 리스트안에서만 고르시오.
    recommend_smart_snacks, compare_category_top3 호출 전에 이 목록에서 선택하세요.
    """
    return TAG_CANDIDATES

@mcp.tool()
async def recommend_smart_snacks(
    categories: list[str] | None = None,
    situation_tags: list[str] | None = None,
    taste_tags: list[str] | None = None,
    preferred_store: str | None = None
) -> str:
    """
    [상황별/취향별 꿀조합 추천]
    1. "야식 추천", "매운 거 땡겨" 같이 상황이나 맛을 묘사할 때 추천 상품을 제안합니다.
    2.  "만원으로 야식 조합 짜줘" 와 같이 사용자의 예산이 제시되면, 결과 리스트에서 상품을 골라 
        '메인+사이드+음료' 또는 '1+1 상품 2개' 등의 구체적인 조합을 구성해서 답변하세요.
        단순 상품 나열이 아닌, '한 끼 식사 세트'를 구성하는 것이 목표입니다.

    ✅ 사용 예시:
    
    - "시험 기간에 먹기 좋은 카페인 조합" (상황)
    - "단짠단짠 과자랑 음료 추천" (맛)
    - "만원으로 2명이 먹을 야식 조합 짜줘" (예산+상황)

    ⚠️ 중요: 호출 전 get_available_tags()로 유효한 태그를 확인해야 합니다.

    Args:
        categories: 여러 카테고리 동시 검색 가능 (예: ["음료", "과자", "빵"]) - 필수 권장 get_available_tags()의 category에서 선택
        situation_tags: 상황 태그 - get_available_tags()의 situation에서 선택
        taste_tags: 맛 태그 - get_available_tags()의 taste에서 선택
        preferred_store: 선호 매장 - "cu", "gs25", "seven_eleven" 중 하나
    """
    all_items = []
    stores = list(STORE_NAMES.keys())
    categories = categories or []
    situation_tags = situation_tags or []
    taste_tags = taste_tags or []

    categories = [decode_unicode(c) for c in categories]
    situation_tags = [decode_unicode(s) for s in situation_tags]
    taste_tags = [decode_unicode(t) for t in taste_tags]

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

    # 3. 카테고리 필터링 (성능 향상)
    if categories:
        categories_lower = [c.lower() for c in categories]
        all_items = [
            item for item in all_items 
            if item.get("category", "").lower() in categories_lower
        ]

    # 4. 검색 준비
    search_situations = [s.lower().strip() for s in (situation_tags or []) if s]
    search_tastes = [t.lower().strip() for t in (taste_tags or []) if t]

    # 5. 스코어링
    scored_results = []

    for item in all_items:
        score = 0
        item_situation = (item.get("situation") or "").lower()
        item_taste = (item.get("taste") or "").lower()

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

    # 상위 30개 정도의 좋은 후보군을 뽑아서 그 안에서 순서를 섞습니다.
    if len(scored_results) > 1:
        # 전체 결과가 30개보다 적으면 전체를, 많으면 상위 30개만 섞음
        mix_limit = min(len(scored_results), 30)
        top_candidates = scored_results[:mix_limit]
        random.shuffle(top_candidates)
        scored_results[:mix_limit] = top_candidates

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
            # "image_url": item.get("image_url")
        })

        if len(final_results) >= 10:
            break

    return json.dumps({
        "query": {
            "categories": categories,
            "situation_tags": situation_tags,
            "taste_tags": taste_tags,
            "store": preferred_store
        },
        "total_matched": len(scored_results),
        "results": final_results
    }, ensure_ascii=False, indent=2)

@mcp.tool()
async def compare_category_top3(
    keywords: list[str],
    category: str = None,
    preferred_store: list[str] = None
) -> str:
    """
    [매장별 카테고리 승자 비교]
    "편의점별 라면 비교해줘", "어디가 음료 행사가 좋아?"처럼 매장 간의 카테고리 경쟁력을 비교합니다.
    특정 카테고리의 매장별 BEST 3 상품을 뽑아 비교 분석합니다.

    ✅ 사용 예시:
    - "초콜릿 어디가 제일 싸?" (전체 비교)
    - "CU랑 GS25 중에 라면 어디가 더 싸?" (매장 간 비교)
    - "이번 달 과자 행사는 어디가 제일 좋아?" (카테고리 승자 찾기)

    ❌ 제외: 특정 상품 단건 검색은 find_best_price 사용.

    ⚠️ 중요: 호출 전 get_available_tags()를 무조건 실행

    Args:
        keywords: 검색 키워드 (예: ["컵라면"])
        category: 비교할 카테고리 명 (get_available_tags 참조)
        preferred_store: (선택) 특정 매장만 확인
    """
    available_stores = list(STORE_NAMES.keys())
    category = decode_unicode(category)
    keywords = [decode_unicode(k) for k in keywords]
    
    if preferred_store:
        target = preferred_store.lower().replace(" ", "")
        available_stores = [s for s in available_stores if target in s]
    
    search_keywords = [k.replace(" ", "").lower() for k in keywords]
    
    report_data = {store: [] for store in available_stores}
    
    for store_id in available_stores:
        file_path = os.path.join(DB_DIR, f"db_{store_id}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(DB_DIR, f"db_{store_id}.json")
        if not os.path.exists(file_path):
            continue
            
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            for item in data.get("items", []):
                if category:
                    item_category = (item.get("category") or "").lower()
                    if category.lower() != item_category:
                        continue
                
                p_name = item.get("product_name", "").lower().replace(" ", "")
                cat_name = (item.get("category") or "").lower()
                
                match_score = 0
                 # category 필터 통과하면 기본 점수
                if category:
                    match_score += 100
                
                # 카테고리 일치 보너스
                if any(k == cat_name for k in search_keywords):
                    match_score += 200
                
                # 키워드 매칭
                for i, kw in enumerate(search_keywords):
                    if kw in p_name:
                        match_score += 100 if i == 0 else 30
                
                if match_score >= 100:
                    raw_price = (
                    item.get("effective_unit_price") or
                    item.get("price_per_unit")
                )

                try:
                    sort_price = float(re.sub(r"[^0-9.]", "", str(raw_price)))
                except (TypeError, ValueError):
                    continue

                if 0 < sort_price < 999999:
                    item["_score"] = match_score
                    item["_sort_price"] = sort_price
                    report_data[store_id].append(item)
                        
        except Exception as e:
            print(f"Error reading {store_id}: {e}")
    
    # 정렬 및 TOP 3 추출
    final_results = {}
    for store_id, items in report_data.items():
        if items:
            sorted_items = sorted(items, key=lambda x: (-x["_score"], x["_sort_price"]))[:3]
            
            display_name = STORE_NAMES.get(store_id, store_id.upper())
            final_results[display_name] = [{
                "product_name": item.get("product_name"),
                "discount_condition": item.get("discount_condition"),
                "pay_price": item.get("sale_price"),
                "get_count": 2 if "1+1" in item.get("discount_condition", "") else 3 if "2+1" in item.get("discount_condition", "") else 1,
                "price_per_one": item.get("effective_unit_price") or item.get("unit_effective_unit_price"),
                "category": item.get("category")
            } for item in sorted_items]
    
    return json.dumps({
        "query": {
            "keywords": keywords,
            "category": category,
            "store": preferred_store
        },
        "results": final_results
    }, ensure_ascii=False, indent=2)

if __name__ == "__main__":
        mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000
    )