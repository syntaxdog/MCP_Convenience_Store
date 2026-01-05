import os, re
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

# 1. 환경 변수 및 Gemini 설정
load_dotenv()
DB_DIR = os.path.join(os.path.dirname(__file__), "db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("API 키가 설정되지 않았습니다. .env 파일을 확인해주세요.")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3-flash-preview")

# 2. 데이터 저장 로직 (DB 역할)
def save_to_db(store_name, items):
    """
    수집된 상품 데이터를 store_name.json 파일로 저장합니다.
    """
    file_path = os.path.join(DB_DIR, f"db_{store_name}.json")
    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "store_name": store_name,
        "items": items
    }
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {file_path} 저장 완료! (총 {len(items)}개 상품)")

# 3. LLM 분석 로직 (비정형 데이터 정제)
async def analyze_text_with_llm(store_name, text_chunk):
    """
    전단지의 텍스트 조각을 받아 Gemini를 통해 JSON 구조로 변환합니다.
    """
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
        # 비동기 환경에서 Gemini 호출 (단순화를 위해 to_thread 사용 가능)
        response = await asyncio.to_thread(model.generate_content, prompt)
        
        # JSON 문자열만 추출 (마크다운 제거)
        res_text = response.text
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        
        return res_text
    except Exception as e:
        print(f"❌ Gemini 분석 에러: {e}")
        return json.dumps({"items": []})

# 4. 데이터 로드 로직 (검색용)
def load_all_data():
    """저장된 모든 JSON DB 파일을 읽어옵니다."""
    all_data = []
    if not os.path.exists(DB_DIR):
        print(f"⚠️ 경고: {DB_DIR} 폴더를 찾을 수 없습니다.")
        return all_data

    # 3. db 폴더 내 파일 탐색
    for file in os.listdir(DB_DIR):
        if file.startswith("db_") and file.endswith(".json"):
            # [중요] 파일 읽을 때 경로를 합쳐줘야 합니다.
            file_path = os.path.join(DB_DIR, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    all_data.append(json.load(f))
            except Exception as e:
                print(f"❌ {file} 읽기 실패: {e}")
                
    return all_data

# 1. 내부 태깅 로직 (Gemini 호출부)
async def _get_tags_logic(product_names: list):
    """
    브랜드, 카테고리, 맛, 상황, 타겟을 모두 포함하는 
    정밀 태깅용 프롬프트입니다.
    """
    prompt = f"""
    당신은 대한민국 편의점 및 마트 상품 전문가입니다. 
    제공된 상품 리스트를 분석하여 마케팅 및 검색에 최적화된 JSON 배열을 생성하세요.

    [절대 규칙 - 매칭 필수]
    1. **product_name**: 입력된 상품명을 **절대 한 글자도, 오타까지도 수정하지 말고 그대로** 다시 적으세요. 
       - 예: 입력이 "덴마트 우유"면 출력도 반드시 "덴마트 우유"여야 합니다. "덴마크"로 고치지 마세요.

    [필수 포함 필드 및 규칙 - 자료형 엄수]
    1. **effective_unit_price**: 혜택 적용 후 상품 개당 실질 단가 (하나 실구매가) (옳은지 검증 후, 옳지 않다면 변경)
    2. **unit_value**: 총 용량 합계를 계산해서 적으세요. (예: "200g*2팩" -> 400, "20g*10입" -> 200, "110g*2" -> 220, "2L" -> 2000)
    3. **unit_type**: 단위 (ml, g, kg, L, 개, 매, 입 등)
    4. **brand**: 브랜드명 (모르면 "일반")
    5. **category**: 세부 분류 (예: 음료, 라면, 스낵)
    6. **taste**: 쉼표로 구분된 문자열. (예: "달콤한, 상큼한")
    7. **situation**: 쉼표로 구분된 문자열. (예: "운동후, 갈증해소")
    8. **target**: 주요 타겟 (예: "학생, 운동인")

    [주의사항]
    - JSON 응답 시 taste와 situation 필드에 대괄호 [ ]를 사용하는 것은 엄격히 금지됩니다. 
    - 예: "taste": ["단맛"] (X) -> "taste": "단맛" (O)

    [응답 형식]
    - 반드시 JSON 배열 형식(`[...]`)으로만 답변하세요.
    - `product_name` 키를 포함하여 원본 데이터와 매칭될 수 있게 하세요.

    [분석 대상 리스트]
    {', '.join(product_names)}

    [JSON 응답 예시]
    [
      {{
        "product_name": "포카리스웨트 500ml",
        "unit_value : 500,
        "unit_type : "ml",
        "effective_unit_price": "4000원",
        "price_per_unit" : "800원 (100ml당)",
        "brand": "CJ",
        "category": "간편식",
        "taste": "짭짤한, 고소한",
        "situation": "아침식사, 간단한끼",
        "target": "학생, 직장인"
      }}
    ]
    """
    
    try:
        # 파일 내부에 정의된 model 객체를 직접 사용
        response = await asyncio.to_thread(model.generate_content, prompt)
        res_text = response.text
        
        # 마크다운 코드 블록 제거 로직
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        elif "```" in res_text:
            res_text = res_text.split("```")[1].split("```")[0].strip()
            
        return res_text
    except Exception as e:
        print(f"❌ Gemini 분석 실패: {e}")
        return "[]"
    
async def enrich_db_with_tags_high_speed(store_name: str):
    """비동기 병렬 처리를 통해 수천 개의 상품을 초고속으로 태깅하고 _with_tags.json으로 저장합니다."""
    file_path = os.path.join(DB_DIR, f"db_{store_name.lower()}.json")

    if not os.path.exists(file_path):
        return f"[{store_name}] 원본 파일이 존재하지 않습니다."

    with open(file_path, "r", encoding="utf-8") as f:
        db_data = json.load(f)
    
    items = db_data.get("items", [])
    
    # 1. 태깅 대상 추출 (중복 제거 및 미분류 상품 대상)
    to_tag_names = list(set([
        item["product_name"] for item in items 
        if "category" not in item or not item["category"] or item["category"] == "미분류"
    ]))
    
    if not to_tag_names: 
        return f"{store_name} DB는 이미 태깅이 완료된 상태입니다."

    print(f"🚀 [{store_name}] 병렬 분석 시작... 대상 상품: {len(to_tag_names)}개")

    chunk_size = 150  # Gemini 처리 적정량
    chunks = [to_tag_names[i:i + chunk_size] for i in range(0, len(to_tag_names), chunk_size)]
    semaphore = asyncio.Semaphore(30) # 동시 요청 10개 제한 (할당량 방어)

    async def process_chunk(chunk):
        async with semaphore:
            res_json = await _get_tags_logic(chunk)
            try:
                return json.loads(res_json)
            except:
                return []

    # 2. 병렬 실행 및 결과 취합
    tasks = [process_chunk(c) for c in chunks]
    all_results = await asyncio.gather(*tasks)

    # 3. 매칭 라이브러리 생성 (공백 제거 매칭용)
    tagged_library = {}
    for chunk_res in all_results:
        if not isinstance(chunk_res, list): continue
        for res_item in chunk_res:
            p_name = res_item.get("product_name") or res_item.get("name")
            if p_name:
                match_key = str(p_name).replace(" ", "").strip().lower()
                tagged_library[match_key] = res_item

    # 4. 데이터 병합 및 정규화
    updated_count = 0
    for item in items:
        name = item.get("product_name", "")
        current_key = str(name).replace(" ", "").strip().lower()
        
        if current_key in tagged_library:
            info = tagged_library[current_key]
            
            # 1. LLM이 추출한 용량 정보 가져오기
            u_val = info.get("unit_value", 1)
            u_type = info.get("unit_type", "개")
            
            # (안전장치) LLM이 문자를 섞어 보냈을 경우 숫자만 추출
            if isinstance(u_val, str):
                import re
                nums = re.findall(r'\d+', u_val)
                u_val = int(nums[0]) if nums else 1
            else:
                u_val = int(u_val) # 강제 형변환

            raw_eff_price = item.get("unit_effective_unit_price") or item.get("effective_unit_price") or 0
            try:
                if isinstance(raw_eff_price, str):
                    # "4,500원" 같은 문자열 대응
                    import re
                    eff_price = int(re.sub(r'[^0-9]', '', raw_eff_price))
                else:
                    # float(3250.0) 등을 int로 안전하게 변환
                    eff_price = int(float(raw_eff_price))
            except:
                eff_price = 0

            # 3. [핵심] 파이썬이 직접 계산 (이제 둘 다 int이므로 에러 없음)
            price_per_unit = 0
            price_ref = "개당"

            if u_val > 0:
                if str(u_type).lower() in ["ml", "g", "mg", "l", "kg"]:
                    # 단위 정규화 (L, kg -> ml, g)
                    if str(u_type).lower() in ["l", "kg", "리터"]:
                        u_val = u_val * 1000
                        u_type = "ml" if "l" in str(u_type).lower() else "g"

                    # 액체/고체: 100단위당 가격
                    price_per_unit = int((eff_price / u_val) * 100)
                    price_ref = f"100{u_type}당"
                else:
                    price_per_unit = int(eff_price / u_val)
                    price_ref = f"{u_type}당" if u_type else "개당"
            else:
                price_per_unit = eff_price

            # 4. 최종 데이터 업데이트
            def ensure_string(val):
                if isinstance(val, list): return ", ".join(str(v) for v in val).strip()
                return str(val) if val else "일반"

            item.update({
                "unit_value": u_val,            # 나중에 검증용으로 남겨둠
                "unit_type": u_type,            # 나중에 검증용으로 남겨둠
                "price_per_unit": price_per_unit, # 정렬용 핵심 데이터
                "price_reference": price_ref,     # UI 표시용 데이터
                "brand": ensure_string(info.get("brand")),
                "category": ensure_string(info.get("category")),
                "taste": ensure_string(info.get("taste")),
                "situation": ensure_string(info.get("situation")),
                "target": ensure_string(info.get("target"))
            })
            updated_count += 1
    
    # 최종 결과 저장
    db_data["items"] = items
    db_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    enriched_file_path = os.path.join(DB_DIR, f"db_{store_name.lower()}_with_tags.json")
    with open(enriched_file_path, "w", encoding="utf-8") as f:
        json.dump(db_data, f, ensure_ascii=False, indent=2)

    return f"✅ {store_name} 고속 업데이트 완료! {updated_count}개 상품 태그 추가."