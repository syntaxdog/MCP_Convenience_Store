"""
편의점 행사 데이터 관리 모듈
- 태그 후보 생성/로드
- LLM 기반 상품 태깅
- DB 저장/로드
"""
import os

import re
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

# ==========================================
# 환경 설정
# ==========================================
load_dotenv()
DB_DIR = os.path.join(os.path.dirname(__file__), "db")
TAG_CANDIDATES_PATH = os.path.join(DB_DIR, "tag_candidates.json")
GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY")
#GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("API 키가 설정되지 않았습니다. .env 파일을 확인해주세요.")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3-flash-preview")

# ==========================================
# 태그 후보 관리
# ==========================================

async def generate_tag_candidates():
    """태그 후보 생성 - 크롤링 전 1회만 실행"""
    prompt = """
    편의점 상품 태깅용 태그 후보를 만들어줘.
    
    [조건]
    - category: 상품 분류 50개 (명확하게 구분, 겹치지 않게)
    - taste: 맛/식감 표현 50개 (명확하게 구분, 겹치지 않게)
    - situation: 상황/용도 50개 (명확하게 구분, 겹치지 않게)
    - 모두 짧고 명확한 단어로 (2글자 이상, 10글자 이하)
    
    [category 예시]
    음료, 과자, 라면, 유제품, 아이스크림, 도시락, 빵, 샌드위치, 김밥, 생활용품, 위생용품, 주류 등
    
    [taste 예시]
    달콤한, 짭짤한, 매운, 시원한, 고소한, 새콤한, 담백한, 바삭한 등
    
    [situation 예시]
    운동후, 야식, 간식, 아침식사, 다이어트, 술안주, 피로회복 등
    
    JSON 형식으로만 응답:
    {
      "category": ["음료", "과자", ...],
      "taste": ["달콤한", "짭짤한", ...],
      "situation": ["운동후", "야식", ...]
    }
    """
    
    res = await asyncio.to_thread(model.generate_content, prompt)
    text = res.text

    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    
    candidates = json.loads(text)
    
    with open(TAG_CANDIDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 태그 후보 저장 완료!")
    print(f"   - category: {len(candidates['category'])}개")
    print(f"   - taste: {len(candidates['taste'])}개")
    print(f"   - situation: {len(candidates['situation'])}개")
    
    return candidates

def load_tag_candidates() -> dict:
    """저장된 태그 후보 로드"""
    if os.path.exists(TAG_CANDIDATES_PATH):
        with open(TAG_CANDIDATES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    print(f"⚠️ tag_candidates.json 파일이 없습니다: {TAG_CANDIDATES_PATH}")
    return {"category": [], "taste": [], "situation": []}

# ==========================================
# 데이터 저장/로드
# ==========================================
def save_to_db(store_name, items):
    """상품 데이터를 JSON 파일로 저장"""
    os.makedirs(DB_DIR, exist_ok=True)
    
    file_path = os.path.join(DB_DIR, f"db_{store_name}.json")
    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "store_name": store_name,
        "items": items
    }
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {file_path} 저장 완료! (총 {len(items)}개 상품)")

def load_all_data():
    """저장된 모든 JSON DB 파일을 읽어옴"""
    all_data = []
    if not os.path.exists(DB_DIR):
        print(f"⚠️ 경고: {DB_DIR} 폴더를 찾을 수 없습니다.")
        return all_data

    for file in os.listdir(DB_DIR):
        if file.startswith("db_") and file.endswith(".json"):
            file_path = os.path.join(DB_DIR, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    all_data.append(json.load(f))
            except Exception as e:
                print(f"❌ {file} 읽기 실패: {e}")
                
    return all_data

# ==========================================
# LLM 기반 태깅
# ==========================================
async def analyze_text_with_llm(store_name: str, text_chunk: str) -> str:
    """전단지 텍스트를 Gemini로 JSON 구조로 변환 (마트용)"""
    prompt = f"""
    당신은 편의점/마트 행사 정보 분석 전문가입니다.
    제공된 [{store_name}]의 텍스트에서 상품명, 원래가격, 최종가격(할인가), 행사조건(1+1, 2+1 등)을 추출하세요.
    
    반드시 아래의 JSON 형식을 지켜주세요:
    {{
      "items": [
        {{
          "product_name": 상품명 (규격/용량 포함),
          "original_price": 상품 1개당 정가,
          "sale_price": 결제 시 총 지불 금액 (할인 적용가),
          "effective_unit_price": 혜택 적용 후 상품 1개당 실질 단가,
          "discount_condition": 행사 종류 (1+1, 2+1, 할인 등)
        }}
      ]
    }}
    
    텍스트:
    {text_chunk}
    """
    
    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        res_text = response.text
        
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        
        return res_text
    except Exception as e:
        print(f"❌ Gemini 분석 에러: {e}")
        return json.dumps({"items": []})

async def _get_tags_logic(product_names: list):
    """태그 후보 목록에서만 선택하는 정밀 태깅"""
    candidates = load_tag_candidates()
    
    prompt = f"""
    당신은 대한민국 편의점 및 마트 상품 전문가입니다. 
    제공된 상품 리스트를 분석하여 JSON 배열을 생성하세요.

    [절대 규칙]
    1. product_name: 입력된 상품명을 절대 수정하지 말고 그대로 적으세요.
    2. category, taste, situation은 반드시 아래 허용 목록에서만 선택하세요.
    3. unit_value: 상품명에 용량이 명시된 경우만 추출. 없으면 null
    4. unit_type: 상품명에 단위가 명시된 경우만 추출. 없으면 null

⚠️ 상품명에 용량 정보가 없으면 절대 추측하지 말 것!

    [category 허용 목록] - 1개만 선택
    {candidates['category']}
    
    [taste 허용 목록] - 복수 선택 가능, 쉼표로 구분
    {candidates['taste']}
    
    [situation 허용 목록] - 복수 선택 가능, 쉼표로 구분
    {candidates['situation']}

    [필수 필드]
    - product_name: 상품명 (원본 그대로)
    - effective_unit_price: 혜택 적용 후 개당 실질 단가
    - unit_value: 총 용량 (예: "200g*2팩" -> 400)
    - unit_type: 단위 (ml, g, 개, 매 등)
    - brand: 브랜드명 (모르면 "일반")
    - category: 위 목록에서 1개 선택
    - taste: 위 목록에서 선택, 쉼표 구분 문자열
    - situation: 위 목록에서 선택, 쉼표 구분 문자열
    - target: 주요 타겟 (예: "학생, 직장인")

    [선택 필드 - 상품명에 명시된 경우만]
    - unit_value: 총 용량 (예: "200g*2팩" -> 400) (없으면 null 값 대입)
    - unit_type: 단위 (ml, g, 개, 매 등) (없으면 null 값 대입)

    [주의사항]
    - 목록에 없는 태그 절대 사용 금지
    - taste, situation은 문자열로 (배열 아님)

    [분석 대상]
    {', '.join(product_names)}

    [응답 예시]
    [
      {{
        "product_name": "포카리스웨트",
        "unit_value": null,
        "unit_type": null,
        "effective_unit_price": 2000,
        "brand": "동아오츠카",
        "category": "음료",
        "taste": "시원한, 상큼한",
        "situation": "운동후, 갈증해소",
        "target": "운동인"
      }}
    ]
    """
    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        res_text = response.text
        
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        elif "```" in res_text:
            res_text = res_text.split("```")[1].split("```")[0].strip()
            
        return res_text
    except Exception as e:
        print(f"❌ Gemini 분석 실패: {e}")
        return "[]"
    
async def enrich_db_with_tags_high_speed(store_name: str) -> str:
    """비동기 병렬 처리를 통해 수천 개의 상품을 초고속으로 태깅하고 _with_tags.json으로 저장합니다."""
    file_path = os.path.join(DB_DIR, f"db_{store_name.lower()}.json")

    if not os.path.exists(file_path):
        return f"[{store_name}] 원본 파일이 존재하지 않습니다."

    with open(file_path, "r", encoding="utf-8") as f:
        db_data = json.load(f)
    
    items = db_data.get("items", [])
    
    # 태깅 대상 추출 (중복 제거 및 미분류 상품 대상)
    to_tag_names = list(set([
        item.get("product_name","") for item in items
        if item.get("product_name") and ("category" not in item or not item["category"] or item["category"] == "미분류")
    ]))
    
    if not to_tag_names: 
        return f"{store_name} DB는 이미 태깅이 완료된 상태입니다."

    print(f"🚀 [{store_name}] 병렬 분석 시작... 대상 상품: {len(to_tag_names)}개")

    chunk_size = 100  # Gemini 처리 적정량
    chunks = [to_tag_names[i:i + chunk_size] for i in range(0, len(to_tag_names), chunk_size)]
    semaphore = asyncio.Semaphore(30) # 동시 요청 10개 제한 (할당량 방어)

    async def process_chunk(chunk):
        async with semaphore:
            res_json = await _get_tags_logic(chunk)
            try:
                return json.loads(res_json)
            except:
                return []

    # 병렬 실행 및 결과 취합
    tasks = [process_chunk(c) for c in chunks]
    all_results = await asyncio.gather(*tasks)

    # 매칭 라이브러리 생성
    tagged_library = {}
    for chunk_res in all_results:
        if not isinstance(chunk_res, list):
            continue
        for res_item in chunk_res:
            p_name = res_item.get("product_name") or res_item.get("name")
            if p_name:
                match_key = str(p_name).replace(" ", "").strip().lower()
                tagged_library[match_key] = res_item

    # 데이터 병합 및 정규화
    updated_count = 0
    for item in items:
        name = item.get("product_name", "")
        current_key = str(name).replace(" ", "").strip().lower()
        
        if current_key in tagged_library:
            info = tagged_library[current_key]
            
            # LLM이 추출한 용량 정보
            u_val = info.get("unit_value") or 1
            u_type = info.get("unit_type") or "개"

            # 문자 섞여 있으면 숫자만 추출
            if isinstance(u_val, str):
                nums = re.findall(r'\d+', u_val)
                u_val = int(nums[0]) if nums else 1
            else:
                try:
                    u_val = int(float(u_val))
                except (TypeError, ValueError):
                    u_val = 1

            raw_eff_price = item.get("unit_effective_unit_price") or item.get("effective_unit_price") or 0
            try:
                if isinstance(raw_eff_price, str):
                    eff_price = int(re.sub(r'[^0-9]', '', raw_eff_price))
                else:
                    eff_price = int(float(raw_eff_price))
            except:
                eff_price = 0

            # 단위당 가격 계산
            price_per_unit = 0
            price_ref = "개당"

            if u_val > 0:
                if str(u_type).lower() in ["ml", "g", "mg", "l", "kg"]:
                    if str(u_type).lower() in ["l", "kg", "리터"]:
                        u_val = u_val * 1000
                        u_type = "ml" if "l" in str(u_type).lower() else "g"

                    price_per_unit = int((eff_price / u_val) * 100)
                    price_ref = f"100{u_type}당"
                else:
                    price_per_unit = int(eff_price / u_val)
                    price_ref = f"{u_type}당" if u_type else "개당"
            else:
                price_per_unit = eff_price

            def ensure_string(val):
                if isinstance(val, list):
                    return ", ".join(str(v) for v in val).strip()
                return str(val) if val else "일반"

            item.update({
                "unit_value": u_val,
                "unit_type": u_type,
                "price_per_unit": price_per_unit,
                "price_reference": price_ref,
                "brand": ensure_string(info.get("brand")),
                "category": ensure_string(info.get("category")),
                "taste": ensure_string(info.get("taste")),
                "situation": ensure_string(info.get("situation")),
                "target": ensure_string(info.get("target"))
            })
            updated_count += 1
    
    # 저장
    db_data["items"] = items
    db_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    enriched_file_path = os.path.join(DB_DIR, f"db_{store_name.lower()}_with_tags.json")
    with open(enriched_file_path, "w", encoding="utf-8") as f:
        json.dump(db_data, f, ensure_ascii=False, indent=2)

    return f"✅ {store_name} 업데이트 완료! {updated_count}개 상품 태그 추가."

# ==========================================
# 실행
# ==========================================
if __name__ == "__main__":
    asyncio.run(generate_tag_candidates())