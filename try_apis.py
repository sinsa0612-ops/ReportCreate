"""한국 정부 API 키 검증 (일회성). KOSIS·KIPRIS 호출 테스트."""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# 1. KOSIS — 통계 통합검색
print("=== KOSIS (통계 통합검색: '연료전지') ===")
try:
    r = httpx.get("https://kosis.kr/openapi/statisticsSearch.do",
                  params={"method": "getList",
                          "apiKey": os.environ.get("KOSIS_API_KEY", ""),
                          "searchNm": "연료전지", "format": "json", "jsonVD": "Y"},
                  timeout=TIMEOUT, follow_redirects=True)
    print("status", r.status_code)
    print(r.text[:500])
except Exception as e:
    print("ERR", type(e).__name__, str(e)[:120])

# 2. KIPRIS Plus — 특허실용신안 단어검색
print("\n=== KIPRIS (특허 단어검색: '수소 연료전지') ===")
try:
    r = httpx.get("http://plus.kipris.or.kr/kipo-api/kipi/patUtiliModInfoSearchSevice/getWordSearch",
                  params={"word": "수소 연료전지",
                          "ServiceKey": os.environ.get("KIPRIS_API_KEY", "")},
                  timeout=TIMEOUT, follow_redirects=True)
    print("status", r.status_code)
    print(r.text[:600])
except Exception as e:
    print("ERR", type(e).__name__, str(e)[:120])
