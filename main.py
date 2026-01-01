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

# UTF-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')

# ==========================================
# 1. 초기화 및 설정
# ==========================================
load_dotenv()
mcp = FastMCP("Convenience Store Smart Bot")
DB_DIR = os.path.join(os.path.dirname(__file__), "db")

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

# ==========================================
# 3. 사용자 공개 도구 (AI 호출용)
# ==========================================


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
    
    intent_res = await asyncio.to_thread(model.generate_content, analysis_prompt)
    intent = json.loads(intent_res.text.replace("```json", "").replace("```", ""))

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
            
        file_path = os.path.join(DB_DIR, f"db_{store}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(DB_DIR, f"db_{store}.json")
            
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
    
    rag_res = await asyncio.to_thread(model.generate_content, rag_prompt)
    return f"[SMART_RECOMMENDATION]\n{rag_res.text}"

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
    
    intent_res = await asyncio.to_thread(model.generate_content, analysis_prompt)
    intent = json.loads(intent_res.text.replace("```json", "").replace("```", ""))
    
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
            
        file_path = os.path.join(DB_DIR, f"db_{store_id}.json")
        enriched_path = os.path.join(DB_DIR, f"db_{store_id}_with_tags.json")
        target_path = enriched_path if os.path.exists(enriched_path) else file_path
        
        if not os.path.exists(target_path): continue
            
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data.get("items") if isinstance(data, dict) else data
                if not isinstance(items, list): continue
                
                for item in items:
                    p_name = item.get("product_name", "").lower()
                    tags = f"{item.get('brand','')} {item.get('category','')} {item.get('taste','')} {item.get('situation','')}".lower()
                    
                    # [개선] 점수 산정 방식 고도화
                    match_score = 0
                    
                    # 상품명에 모든 검색어가 포함되면 높은 점수 (예: 초코 + 우유)
                    if all(term in p_name for term in search_terms):
                        match_score += 100 
                    # 일부 키워드만 포함된 경우
                    elif any(term in p_name for term in search_terms):
                        match_score += 30
                    
                    # 태그(맛, 상황 등) 매칭 점수
                    if any(term in tags for term in search_terms):
                        match_score += 10

                    if match_score >= 30:
                        item["match_score"] = match_score
                        item["store_name"] = store_id.upper()
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

if __name__ == "__main__":
    mcp.run()