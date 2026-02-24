# 🏪 편리단 (Pyeon-Ri-Dan)

> **편의점 행사 정보 MCP 서버** — CU, GS25, 세븐일레븐의 행사 상품을 검색하고, 가성비를 분석하고, 꿀조합을 추천받으세요.

AI 에이전트가 편의점 행사 데이터를 **MCP(Model Context Protocol) 도구**로 활용할 수 있도록 설계된 서버입니다.  
매월 자동으로 데이터를 수집하고, Gemini LLM이 상품마다 카테고리·맛·상황 태그를 부여하여 정밀한 추천이 가능합니다.

---

## ✨ 주요 기능

| MCP Tool | 설명 | 사용 예시 |
|---|---|---|
| `find_best_price` | 특정 상품의 최저가 검색 | "코카콜라 최저가" |
| `find_best_value` | 용량 대비 가성비(100ml당 단가) 분석 | "펩시 콜라 용량 당 최저" |
| `recommend_smart_snacks` | 상황별·취향별 꿀조합 추천 | "만원으로 야식 조합 짜줘" |
| `compare_category_top3` | 매장별 카테고리 TOP3 비교 | "편의점별 라면 비교해줘" |
| `get_available_tags` | 검색에 사용 가능한 태그 목록 조회 | — |

---

## 🏗️ 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                   AI 클라이언트                        │
│            (Claude, Gemini 등 MCP 지원 LLM)           │
└──────────────────────┬──────────────────────────────┘
                       │ MCP (Streamable HTTP)
                       ▼
┌─────────────────────────────────────────────────────┐
│              FastMCP 서버 (main.py)                   │
│  ┌───────────┬──────────────┬─────────────────────┐ │
│  │ 최저가검색 │ 가성비분석    │ 꿀조합추천 / 비교    │ │
│  └───────────┴──────────────┴─────────────────────┘ │
│                       │                               │
│              JSON DB (db/ 디렉토리)                    │
└─────────────────────────────────────────────────────┘
                       ▲
    ┌──────────────────┘ (매월 1일 자동 갱신)
    │
┌───┴─────────────────────────────────────────────────┐
│           데이터 파이프라인                             │
│  crawler.py → manager.py (Gemini LLM 태깅)           │
│  GitHub Actions (monthly-crawl.yml)                   │
└─────────────────────────────────────────────────────┘
```

---

## 🗂️ 프로젝트 구조

```
kakao/
├── main.py              # MCP 서버 & 5개 도구 정의
├── crawler.py           # CU, GS25, 세븐일레븐 크롤러
├── manager.py           # 데이터 관리 & Gemini LLM 태깅
├── run_crawl.py         # GitHub Actions 전용 실행 스크립트
├── db/                  # 크롤링 데이터 (JSON)
│   ├── db_cu.json
│   ├── db_cu_with_tags.json
│   ├── db_gs25.json
│   ├── db_gs25_with_tags.json
│   ├── db_seven_eleven.json
│   ├── db_seven_eleven_with_tags.json
│   └── tag_candidates.json
├── .github/workflows/
│   └── monthly-crawl.yml  # 매월 자동 크롤링 & 배포
├── Dockerfile           # 배포용 Docker 이미지
├── fly.toml             # Fly.io 배포 설정
├── requirements.txt     # Python 의존성
└── .env                 # 환경변수 (GOOGLE_API_KEY)
```

---

## 🚀 시작하기

### 사전 요구사항

- Python 3.11+
- [Google AI API Key](https://aistudio.google.com/) (Gemini, 태깅용)

### 로컬 실행

```bash
# 1. 의존성 설치
pip install -r requirements.txt
playwright install chromium

# 2. 환경변수 설정
echo "GOOGLE_API_KEY=your_api_key_here" > .env

# 3. 서버 실행
python main.py
```

서버가 `http://localhost:8000` 에서 Streamable HTTP 방식으로 실행됩니다.

### MCP 클라이언트 연결

```json
{
  "mcpServers": {
    "pyeon-ri-dan": {
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

---

## 📡 데이터 파이프라인

### 크롤링 대상

| 편의점 | 수집 방식 | 행사 유형 |
|---|---|---|
| **CU** | API 호출 | 1+1, 2+1 |
| **GS25** | API 호출 | 1+1, 2+1 |
| **세븐일레븐** | 웹 크롤링 (Playwright) | 1+1, 2+1 |

### 자동 업데이트

- **GitHub Actions**가 매월 말일에 실행되어 다음 달 1일 데이터를 수집합니다.
- 크롤링 → Gemini LLM 태깅 → DB 커밋 → Fly.io 자동 배포

### 수동 크롤링

```bash
# 전체 매장 크롤링
python run_crawl.py

# 특정 매장만 크롤링
python run_crawl.py cu gs25
```

---

## ☁️ 배포

[Fly.io](https://fly.io)에 Docker 컨테이너로 배포됩니다.

```bash
# Fly.io 배포
flyctl deploy -a kakao --remote-only
```

| 설정 | 값 |
|---|---|
| 리전 | `nrt` (도쿄) |
| 인스턴스 | `shared-cpu-1x`, 2GB RAM |
| 자동 중지 | suspend (비활성 시 절전) |
| HTTPS | 강제 |

---

## 🛠️ 기술 스택

| 영역 | 기술 |
|---|---|
| MCP 프레임워크 | [FastMCP](https://github.com/jlowin/fastmcp) |
| 웹 서버 | FastAPI + Uvicorn |
| 크롤링 | Requests, BeautifulSoup4, Playwright |
| AI 태깅 | Google Gemini (`gemini-3-flash-preview`) |
| 스케줄링 | APScheduler |
| CI/CD | GitHub Actions |
| 배포 | Fly.io + Docker |
| 데이터 저장 | JSON 파일 기반 |

---

## 📄 라이선스

MIT License