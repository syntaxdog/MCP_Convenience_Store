import os
import json
import sys
import io
import re
import asyncio
from fastmcp import FastMCP
from dotenv import load_dotenv
from typing import List

# manager.py에서 공통 로직 및 Gemini 설정 임포트
from manager import model, load_all_data, GEMINI_API_KEY

# UTF-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')

# ==========================================
# 1. 초기화 및 설정
# ==========================================
load_dotenv()
mcp = FastMCP("Convenience Store Smart Bot")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    사용자의 무드(Mood)와 상황에 맞는 실시간 편의점 행사를 기반으로 최적의 가성비 상품을 추천합니다.
    """
    # 1. 의도 및 키워드 추출
    analysis_prompt = f"""
    사용자 요청: "{user_request}"
    분석 항목: primary_keywords, specs, mood_tags, preferred_store
    반드시 JSON으로 응답해.
    """
    
    intent_res = await asyncio.to_thread(model.generate_content, analysis_prompt)
    intent = json.loads(intent_res.text.replace("```json", "").replace("```", ""))

    # 2. 데이터 타입 안정화 및 매장 필터링 설정
    def ensure_string_list(data):
        if isinstance(data, list): return [str(i).lower() for i in data if i]
        if isinstance(data, str): return [data.lower()]
        return []
    
    primary = ensure_string_list(intent.get('primary_keywords', []))
    specs = ensure_string_list(intent.get('specs', []))
    moods = ensure_string_list(intent.get('mood_tags', []))
    search_pool = list(set(primary + specs + moods))
    
    target_store = intent.get('preferred_store')
    all_items = [] 
    stores = ["cu", "gs25", "seven_eleven", "emart", "gs_the_fresh"] 

    # 3. 데이터 로드 (With Tags 우선)
    for store in stores:
        if target_store and str(target_store).lower() not in store: continue
            
        file_path = os.path.join(BASE_DIR, f"db_{store}_with_tags.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(BASE_DIR, f"db_{store}.json")
        if not os.path.exists(file_path): continue
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                items_list = data.get("items", [])
                for item in items_list:
                    item["store"] = store.upper()
                    all_items.append(item)
        except Exception as e:
            print(f"Error loading {store}: {e}")

    if not all_items:
        return "죄송합니다. 현재 편의점 행사 데이터를 불러올 수 없습니다."

    # 4. 스마트 스코어링 (키워드 매칭 + 가성비 가산점)
    scored_results = []
    for item in all_items:
        score = 0
        p_name = item.get("product_name", "").lower()
        
        # 태그 통합 검색 (쉼표 문자열 구조 반영)
        tags_text = f"{item.get('category','')} {item.get('brand','')} {item.get('taste','')} {item.get('situation','')}".lower()

        for kw in search_pool:
            if kw in p_name: score += 20  # 상품명 매칭 가중치 상승
            elif kw in tags_text: score += 15
        
        # [추가] 가성비 가산점: 1+1 이거나 실질 단가가 정가보다 낮으면 추가 점수
        if item.get("discount_condition") == "1+1": score += 10
        elif item.get("effective_unit_price", 0) < item.get("original_price", 0): score += 5

        if score >= 10: 
            scored_results.append((score, item))
            
    # 점수 높은 순 정렬 후 상위 5개 추출
    scored_results.sort(key=lambda x: x[0], reverse=True)
    top_matches = [x[1] for x in scored_results[:5]]

    if not top_matches:
        return f"'{user_request}'에 어울리는 추천 상품을 찾지 못했어요."

    # 5. 최종 RAG 추천 (데이터에 기반한 상세 설명 요청)
    rag_prompt = f"""
    사용자 상황: {user_request}
    추천 상품 리스트: {json.dumps(top_matches, ensure_ascii=False)}
    
    [지침]
    1. 각 상품이 왜 사용자의 상황(Mood)에 맞는지 태그(taste, situation)를 언급하며 설명해줘.
    2. '실질 단가(effective_unit_price)'를 언급하며 얼마나 저렴한지 강조해줘.
    3. 1+1이나 2+1 같은 행사 정보도 포함해줘.
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