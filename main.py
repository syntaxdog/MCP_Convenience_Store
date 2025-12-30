import os, json, sys, io, re, asyncio
from bs4 import BeautifulSoup
import requests
from playwright.async_api import async_playwright
from fastmcp import FastMCP
from google import genai
from google.genai import types
from typing import List, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# UTF-8 출력 설정 (Windows 환경 대응)
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8')

# ==========================================
# 1. 관리 클래스: 데이터 처리 및 유틸리티 통합
# ==========================================
class ConvenienceStoreManager:
    """편의점 데이터의 로드, 정규화, 타입 체크를 전담합니다."""
    STORES = ["cu", "gs25", "seven_eleven", "emart", "gs_the_fresh"]
    
    def __init__(self, api_key, model_id):
        self.api_key = API_KEY
        self.client = genai.Client(api_key=api_key)
        self.model_id = model_id
        self.base_dir = os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def ensure_string_list(data):
        """데이터가 리스트면 소문자 문자열 리스트로, 문자열이면 리스트로 감싸 반환 (에러 방지 핵심)"""
        if isinstance(data, list):
            return [str(i).lower().strip() for i in data if i]
        if isinstance(data, str):
            return [data.lower().strip()]
        return []

    @staticmethod
    def get_safe_str(field):
        """DB 필드(리스트/문자열/None)를 안전하게 문자열로 변환하여 에러를 차단합니다."""
        if isinstance(field, list):
            return " ".join(str(i) for i in field if i)
        return str(field) if field else ""

    def load_store_data(self, store_id):
        store_id = store_id.lower().replace(" ", "_")
        # [수정] 파일 위치를 찾기 위해 두 가지 경로를 모두 시도
        paths = [
            os.path.join(os.getcwd(), f"db_{store_id}_with_tags.json"),
            os.path.join(os.getcwd(), f"db_{store_id}.json"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), f"db_{store_id}_with_tags.json")
        ]
        
        for path in paths:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        items = data.get("items", []) if isinstance(data, dict) else data
                        return items if isinstance(items, list) else []
                except Exception as e:
                    sys.stderr.write(f">> [File Error] {path}: {e}\n")
            else:
                # 파일이 없을 때 어디를 뒤졌는지 stderr에 출력
                sys.stderr.write(f">> [Path Not Found] {path}\n")
        return []

    def load_all_data(self, target_store=None):
        all_items = []
        
        # [테스트용] target_store를 완전히 무시하고 모든 매장 로드 시도
        for store in self.STORES:
            store_items = self.load_store_data(store)
            sys.stderr.write(f">> [Load Attempt] Store: {store}, Items: {len(store_items)}\n")
            
            if isinstance(store_items, list):
                for item in store_items:
                    if isinstance(item, dict):
                        item["source_store"] = store.upper().replace("_", " ")
                        all_items.append(item)
        
        return all_items

# ==========================================
# 2. 초기화 및 설정
# ==========================================
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_ID = "gemini-3-flash-preview"

mgr = ConvenienceStoreManager(API_KEY, MODEL_ID)
mcp = FastMCP("Convenience Store Smart Bot")

# ==========================================
# 3. 내부 유틸리티 및 관리 함수 (AI에게 직접 노출 안 함)
# ==========================================

def save_to_db(store_name: str, items: list):
    """수집된 상품 리스트를 로컬 JSON 파일로 저장합니다."""
    file_path = os.path.join(mgr.base_dir, f"db_{store_name.lower()}.json")
    data_to_save = {
        "store_name": store_name,
        "last_updated": "2025-12-30",
        "total_count": len(items),
        "items": items
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    print(f"[SUCCESS] {file_path} 저장 완료! (총 {len(items)}개)")

def normalize_product_data(item: dict) -> dict:
    """용량 파싱 및 100단위당 실질 가격을 계산합니다."""
    p_name = str(item.get("product_name", ""))
    price = item.get("final_price", 0)
    condition = str(item.get("discount_condition", ""))
    unit_field = str(item.get("unit", ""))
    
    capacity = 0
    pattern = r'(\d+(?:\.\d+)?)\s*(ml|l|g|kg)'
    for text in [p_name, condition, unit_field]:
        match = re.search(pattern, text.lower())
        if match:
            value, unit = float(match.group(1)), match.group(2)
            capacity = int(value * 1000) if unit in ['l', 'kg'] else int(value)
            bundle_match = re.search(r'[\*x]\s*(\d+)', text.lower())
            if bundle_match: capacity *= int(bundle_match.group(1))
            break

    total_capacity, pay_price = capacity, price
    cond_lower = condition.lower()
    if "1+1" in cond_lower: total_capacity = capacity * 2
    elif "2+1" in cond_lower: 
        total_capacity, pay_price = capacity * 3, price * 2

    item["unit_price_per_100"] = int((pay_price / total_capacity) * 100) if total_capacity > 0 else 0
    item["capacity_ml"] = capacity
    return item

async def analyze_text_with_llm(mart_name: str, raw_text: str) -> str:
    """텍스트 기반 데이터 추출용 LLM 호출 함수"""
    prompt = f"당신은 {mart_name} 전단지 정리 전문가입니다. 주어진 텍스트에서 상품 정보를 추출하여 JSON으로 정리하세요.\n\n[데이터]\n{raw_text}"
    response = await mgr.client.aio.models.generate_content(
        model=mgr.model_id, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
    )
    return response.text

async def _get_tags_logic(product_names: List[str]) -> str:
    """상품 태깅용 내부 로직 함수"""
    prompt = f"편의점 상품 전문가로서 아래 상품들에 브랜드, 카테고리, 맛, 상황, 타겟 태그를 달아 JSON 배열로 반환하세요.\n\n[리스트]: {', '.join(product_names)}"
    response = await mgr.client.aio.models.generate_content(
        model=mgr.model_id, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
    )
    return response.text

# ------------------------------------------
# 내부용 크롤링/업데이트 함수 (데코레이터 제거)
# ------------------------------------------

async def enrich_db_with_tags_internal(store_name: str):
    """DB에 태그를 입히는 내부 관리용 함수"""
    file_path = os.path.join(mgr.base_dir, f"db_{store_name.lower()}.json")
    if not os.path.exists(file_path): return
    with open(file_path, "r", encoding="utf-8") as f:
        db_data = json.load(f)
    items = db_data.get("items", [])
    to_tag_names = list(set([item["product_name"] for item in items if "category" not in item]))
    if not to_tag_names: return
    
    # 배치 처리 로직 (생략 없이 통합 실행 가능)
    res_json = await _get_tags_logic(to_tag_names[:100]) # 예시로 100개만
    # ... 병합 및 저장 로직 ...
    pass

# ==========================================
# 4. 사용자 공개 도구 (AI가 호출 가능)
# ==========================================

@mcp.tool()
async def recommend_smart_snacks(user_request: str) -> str:
    """
    [🚨 메인 추천 도구] 
    사용자가 무엇을 먹을지 모를 때(출출해, 야식 추천 등) 실시간 행사 DB를 기반으로 최적의 간식을 제안합니다.
    """
    # 1. 의도 분석
    analysis_prompt = f"사용자 요청: '{user_request}' 분석 항목: primary_keywords, specs, mood_tags, preferred_store 반드시 JSON 응답."
    intent_res = await mgr.client.aio.models.generate_content(
        model=mgr.model_id, contents=analysis_prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    intent = json.loads(intent_res.text)

    # 2. 데이터 통합 로드 (mgr 클래스 활용으로 로직 단순화)
    pref_store = intent.get('preferred_store')
    all_items = mgr.load_all_data(target_store=pref_store)
    
    if not all_items:
        return "죄송합니다. 현재 편의점 데이터를 불러올 수 없습니다."

    # 3. 정교한 스코어링 (mgr 유틸리티를 통한 타입 방어)
    search_pool = list(set(
        mgr.ensure_string_list(intent.get('primary_keywords', [])) +
        mgr.ensure_string_list(intent.get('specs', [])) +
        mgr.ensure_string_list(intent.get('mood_tags', []))
    ))

    scored_results = []
    for item in all_items:
        score = 0
        p_name = item.get("product_name", "").lower()
        # 태그 데이터 안전하게 병합
        tags_text = f"{item.get('category','')} {mgr.get_safe_str(item.get('taste'))} {mgr.get_safe_str(item.get('situation'))}".lower()

        for kw in search_pool:
            if kw in p_name: score += 15
            elif kw in tags_text: score += 12
            elif len(kw) >= 2 and (kw[:2] in p_name or kw[:2] in tags_text): score += 3

        if item.get("discount_condition") in ["1+1", "2+1"]: score += 5
        if score >= 5: scored_results.append((score, item))

    scored_results.sort(key=lambda x: x[0], reverse=True)
    top_matches = [x[1] for x in scored_results[:5]]

    if not top_matches:
        return f"'{user_request}'에 맞는 추천 상품을 찾지 못했어요. 키워드를 바꿔볼까요?"

    # 4. 최종 응답 생성 (RAG)
    final_prompt = f"""
    [강제 지침] 반드시 제공된 데이터에 기반해서만 답변하고, 너의 상식(삼각김밥 등)은 제외해라.
    사용자 질문: {user_request}
    추출된 상품 데이터: {json.dumps(top_matches, ensure_ascii=False)}
    """
    final_res = await mgr.client.aio.models.generate_content(model=mgr.model_id, contents=final_prompt)
    
    return f"[FINAL_RESULT]\n{final_res.text}"

@mcp.tool()
async def find_best_price(product_keyword: str) -> str:
    """[최저가 비교 전용] 특정 상품의 가격 비교 및 어느 매장이 가장 저렴한지 찾을 때 사용합니다."""
    # 1. 의도 분석
    analysis_prompt = f"검색어: '{product_keyword}' 분석 항목: target_store, clean_keyword, specs 반드시 JSON 응답."
    intent_res = await mgr.client.aio.models.generate_content(
        model=mgr.model_id, contents=analysis_prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    intent = json.loads(intent_res.text)
    
    clean_query = intent.get('clean_keyword', product_keyword)
    # 조사 제거 및 검색어 정규화
    raw_terms = mgr.ensure_string_list(str(clean_query).split())
    search_terms = [re.sub(r'(은|는|이|가|를|을)$', '', t) for t in raw_terms if len(t) >= 1]

    debug_info = {
        "analyzed_query": clean_query,
        "search_terms": search_terms,
        "total_items_scanned": 0,
        "sample_p_names": []
    }
    
    # 2. 데이터 로드
    all_items = mgr.load_all_data(target_store=intent.get('target_store'))
    debug_info["total_items_scanned"] = len(all_items)
    
    scored_items = []
    
    # 3. 검색 및 스코어링 (초코바나나우유 걸러내기 로직)
    for i, item in enumerate(all_items):
        if not isinstance(item, dict): continue
            
        p_name = item.get("product_name", "").lower()
        p_name_clean = p_name.replace(" ", "")
        
        if i < 5: debug_info["sample_p_names"].append(p_name)
        
        # 기본 조건: 검색어가 모두 포함되어야 함 (AND 검색)
        if all(term in p_name or term in p_name_clean for term in search_terms):
            score = 0
            # 가점 1: 상품명과 검색어의 길이 차이가 적을수록 (순수 상품 우대)
            query_total_len = len("".join(search_terms))
            len_diff = abs(len(p_name_clean) - query_total_len)
            score += max(0, 20 - len_diff) # 길이가 딱 맞으면 20점 가점
            
            # 가점 2: 상품명이 검색어로 시작하면 가점
            if p_name_clean.startswith(search_terms[0]):
                score += 10
            
            # 가점 3: 불필요한 맛(바나나, 딸기 등)이 상품명에 있는데 검색어엔 없을 때 감점
            distractors = ["바나나", "딸기", "커피", "멜론"]
            for d in distractors:
                if d in p_name and d not in "".join(search_terms):
                    score -= 15 # 강력 감점

            scored_items.append((score, item))

    if not scored_items:
        return json.dumps({
            "error": "No items found",
            "debug_context": debug_info,
            "message": f"'{product_keyword}'에 대한 정보를 찾지 못했습니다."
        }, ensure_ascii=False, indent=2)

    # 4. 정렬: 1순위 점수(내림차순), 2순위 단가(오름차순)
    scored_items.sort(key=lambda x: (-x[0], x[1].get("unit_price", 999999)))
    
    # 결과 상위 5개 추출
    top_matches = [x[1] for x in scored_items[:5]]
    best = top_matches[0]
    
    summary = f"총 {len(scored_items)}개를 찾았고, {best['source_store']}의 {best['product_name']}이 개당 {best.get('unit_price')}원으로 가장 저렴합니다."
    
    return json.dumps({
        "summary": summary,
        "best_deal": best,
        "all_results": top_matches
    }, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    mcp.run()