"""
GitHub Actions용 크롤러 실행 스크립트
- 크롤링 후 바로 종료
"""
import asyncio
import sys

async def main():
    from crawler import run_full_pipeline

    stores = sys.argv[1:] if len(sys.argv) > 1 else ["cu", "gs25", "seven_eleven"]
    print(f"🚀 크롤링 시작: {stores}")

    await run_full_pipeline(stores)

    print("✅ 크롤링 완료!")

if __name__ == "__main__":
    asyncio.run(main())
