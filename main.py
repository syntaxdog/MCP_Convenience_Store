"""
편의점 행사 정보 MCP 서버
- CU, GS25, 세븐일레븐 행사 상품 추천
"""
import os
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
mcp = FastMCP("Convenience Store Smart Bot")
DB_DIR = os.path.join(os.path.dirname(__file__), "db")

STORE_NAMES = {
    "cu": "편의점 CU",
    "gs25": "편의점 GS25",
    "seven_eleven": "편의점 세븐일레븐"
}

# 태그 후보 미리 로드 (성능 향상)
TAG_CANDIDATES = load_tag_candidates()

def decode_unicode(text: str) -> str:
    """유니코드 이스케이프 디코딩"""
    if not text:
        return text
    try:
        # \\uXXXX 패턴이 있으면 디코딩
        if '\\u' in text:
            return text.encode().decode('unicode_escape')
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
    [특정 상품 최저가 검색]
    사용자가 "OO 제일 싼 곳", "OO 어디가 싸?" 처럼 
    **특정 상품 하나**의 최저가 매장을 찾을 때 사용합니다.
    
    예시 쿼리: "코카콜라 제일 싼 곳", "신라면 어디가 저렴해?"

    Args:
        keywords: 검색 키워드 + 동의어/브랜드 포함 (예: ["코카콜라", "콜라", "제로콜라"])
        preferred_store: 특정 매장만 검색 - "cu", "gs25", "seven_eleven" 중 하나
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
                        item["sort_price"] = (
                            item.get("price_per_unit") or 
                            item.get("effective_unit_price") or 
                            99999
                        )
                        all_matched_items.append(item)
                        
        except Exception as e:
            print(f"Error reading {store_id}: {e}")

    if not all_matched_items:
        return json.dumps({
            "error": f"'{main_keyword}'에 대한 행사 정보를 찾지 못했습니다.",
            "results": []
        }, ensure_ascii=False)

    # 정렬: 매칭 점수 높은 순 → 가격 낮은 순
    all_matched_items.sort(key=lambda x: (-x["match_score"], x["sort_price"]))

    best = all_matched_items[0]
    condition = best.get("discount_condition", "")
    
    return json.dumps({
        "query": {
            "keywords": keywords,
            "store": preferred_store
        },
        "total_found": len(all_matched_items),
        "best_deal": {
            "product_name": best.get("product_name"),
            "store": best.get("store_name"),
            "discount_condition": condition,
            "pay_price": best.get("sale_price"),
            "get_count": 2 if "1+1" in condition else 3 if "2+1" in condition else 1,
            "price_per_one": best.get("effective_unit_price") or best.get("unit_effective_unit_price"),
            "image_url": best.get("image_url")
        },
        "all_results": [{
            "product_name": item.get("product_name"),
            "store": item.get("store_name"),
            "discount_condition": item.get("discount_condition"),
            "pay_price": item.get("sale_price"),
            "price_per_one": item.get("effective_unit_price") or item.get("unit_effective_unit_price")
        } for item in all_matched_items[:10]]
    }, ensure_ascii=False, indent=2)

@mcp.tool()
async def find_best_value(
    keywords: list[str],
    preferred_store: str = None
) -> str:
    """
    [용량 대비 가성비 검색]
    "OO 가성비", "OO 용량 대비 싼 거" 처럼 
    100ml당/100g당 가격 기준으로 가성비 최고 상품을 찾습니다.
    
    예시: "콜라 가성비", "과자 용량 대비", "음료 ml당 싼 거"
    
    ❌ 이 도구 사용 X: "콜라 제일 싼 곳" (절대 최저가 → find_best_price)
    ✅ 이 도구 사용 O: "콜라 가성비 좋은 거" (용량 대비 가격)

    Args:
        keywords: 검색 키워드 + 동의어/브랜드 (예: ["코카콜라", "콜라"])
        preferred_store: 특정 매장만 검색 - "cu", "gs25", "seven_eleven" 중 하나
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
                        # 용량당 가격이 있는 상품만
                        price_per_unit = item.get("price_per_unit")
                        if not price_per_unit or price_per_unit <= 0:
                            continue
                            
                        display_name = STORE_NAMES.get(store_id, store_id.upper())
                        item["match_score"] = match_score
                        item["store_name"] = display_name
                        item["sort_price"] = price_per_unit  # 용량당 가격으로 정렬!
                        all_matched_items.append(item)
                        
        except Exception as e:
            print(f"Error reading {store_id}: {e}")

    if not all_matched_items:
        return json.dumps({
            "error": f"'{main_keyword}'에 대한 용량 정보가 있는 행사 상품을 찾지 못했습니다.",
            "results": []
        }, ensure_ascii=False)

    # 정렬: 매칭 점수 높은 순 → 용량당 가격 낮은 순
    all_matched_items.sort(key=lambda x: (-x["match_score"], x["sort_price"]))

    best = all_matched_items[0]
    condition = best.get("discount_condition", "")
    
    return json.dumps({
        "query": {
            "keywords": keywords,
            "store": preferred_store
        },
        "total_found": len(all_matched_items),
        "best_value": {
            "product_name": best.get("product_name"),
            "store": best.get("store_name"),
            "discount_condition": condition,
            "pay_price": best.get("sale_price"),
            "get_count": 2 if "1+1" in condition else 3 if "2+1" in condition else 1,
            "price_per_one": best.get("effective_unit_price") or best.get("unit_effective_unit_price"),
            "unit_value": best.get("unit_value"),
            "unit_type": best.get("unit_type"),
            "price_per_unit": best.get("price_per_unit"),
            "price_reference": best.get("price_reference"),
            "image_url": best.get("image_url")
        },
        "all_results": [{
            "product_name": item.get("product_name"),
            "store": item.get("store_name"),
            "discount_condition": item.get("discount_condition"),
            "pay_price": item.get("sale_price"),
            "price_per_one": item.get("effective_unit_price") or item.get("unit_effective_unit_price"),
            "unit_value": item.get("unit_value"),
            "unit_type": item.get("unit_type"),
            "price_per_unit": item.get("price_per_unit"),
            "price_reference": item.get("price_reference")
        } for item in all_matched_items[:10]]
    }, ensure_ascii=False, indent=2)

@mcp.tool()
def get_available_tags() -> dict:
    """
    검색에 사용 가능한 태그 목록을 반환합니다.
    recommend_smart_snacks, compare_category_top3 호출 전에 이 목록에서 선택하세요.
    """
    return TAG_CANDIDATES

@mcp.tool()
async def recommend_smart_snacks(
    keywords: list[str],
    categories: list[str] = None,
    situation_tags: list[str] = None,
    taste_tags: list[str] = None,
    preferred_store: str = None
) -> str:
    """
    [실시간 편의점 행사 기반 스마트 추천]
    
    ⚠️⚠️⚠️ 중요: 이 도구 호출 전 반드시 get_available_tags()를 먼저 호출하여 
    category 값을 확인하세요.

    Args:
        keywords: 검색 키워드 + 브랜드/동의어 포함 (예: ["라면", "신라면", "컵라면"])
        categories: 여러 카테고리 동시 검색 가능 (예: ["음료", "과자", "빵"]) - 필수 권장
        situation_tags: 상황 태그 - get_available_tags()의 situation에서 선택
        taste_tags: 맛 태그 - get_available_tags()의 taste에서 선택
        preferred_store: 선호 매장 - "cu", "gs25", "seven_eleven" 중 하나

    Returns:
        매칭된 상품 리스트 JSON
    """
    all_items = []
    stores = list(STORE_NAMES.keys())
    keywords = [decode_unicode(k) for k in keywords] if keywords else []
    categories = [decode_unicode(c) for c in categories] if categories else None
    situation_tags = [decode_unicode(s) for s in situation_tags] if situation_tags else None
    taste_tags = [decode_unicode(t) for t in taste_tags] if taste_tags else None

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
    preferred_store: str = None
) -> str:
    """
    [매장별 최저가 TOP3 비교]
    사용자가 "OO 비교해줘", "편의점별 OO 뭐가 좋아?" 처럼
    **카테고리 전체**를 매장별로 **비교**할 때 사용합니다.
    
    예시 쿼리: "라면 비교해줘", "편의점별 음료 가성비", "과자 어디가 좋아?"

    ⚠️⚠️⚠️ 중요: 이 도구 호출 전 반드시 get_available_tags()를 먼저 호출하여 
    category 값을 확인하세요.
    
    ❌ 이 도구 사용 X: "코카콜라 제일 싼 곳" (특정 상품 → find_best_price)
    ✅ 이 도구 사용 O: "음료 비교해줘" (카테고리 비교)

    Args:
        keywords: 검색 키워드 + 동의어 (예: ["라면", "컵라면"])
        category: 상품 카테고리 - get_available_tags()에서 선택
        preferred_store: 특정 매장만 비교
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
                
                # 카테고리 일치 보너스
                if any(k == cat_name for k in search_keywords):
                    match_score += 200
                
                # 키워드 매칭
                for i, kw in enumerate(search_keywords):
                    if kw in p_name:
                        match_score += 100 if i == 0 else 30
                
                if match_score >= 100:
                    sort_price = (
                        item.get("price_per_unit") or 
                        item.get("effective_unit_price") or 
                        99999
                    )
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
    mcp.run()