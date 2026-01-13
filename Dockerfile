# 1. 파이썬 베이스 이미지 선택
FROM python:3.11-slim

# 2. 필수 리눅스 패키지 설치 (Playwright 브라우저 실행용 의존성)
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# 3. 작업 디렉토리 설정
WORKDIR /app

# 4. 종속성 파일 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Playwright 전용 브라우저(Chromium) 설치
RUN playwright install chromium
RUN playwright install-deps chromium

# 6. 소스 코드 및 DB 폴더 복사
COPY . .

# 7. 포트 설정 (FastAPI 기본 포트)
EXPOSE 8000

# 8. 서버 실행 명령 (SSE 스트리밍을 위해 uvicorn 사용)
# main.py 안에 있는 FastAPI 앱(app)을 실행합니다.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]