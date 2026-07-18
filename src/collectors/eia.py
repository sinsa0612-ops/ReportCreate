"""EIA (미국 에너지정보청) API v2 어댑터.

EIA v2 API: https://www.eia.gov/opendata/
인증: api_key 쿼리 파라미터

단독 점검:
    .venv\\Scripts\\python.exe -m src.collectors.eia
"""
import os
import httpx
from datetime import date

from src.models import Document

EIA_BASE = "https://api.eia.gov/v2"
EIA_WEB  = "https://www.eia.gov"
TIMEOUT  = httpx.Timeout(30.0, connect=10.0)
HEADERS  = {"User-Agent": "Crawling-Research-Pipeline/1.0 (eia collector)"}

# 한국어/영어 키워드 → EIA v2 최상위 경로 매핑
# 튜플: (매칭 키워드 목록, route_id)
_KEYWORD_ROUTES: list[tuple[list[str], str]] = [
    (["전기", "전력", "electricity", "power", "grid", "발전"], "electricity"),
    # "hydro" 는 "hydrogen" 의 부분 문자열로 오매칭되므로 hydropower 류만 사용
    (["재생", "renewable", "solar", "태양", "wind", "풍력", "biomass", "수력", "hydropower", "hydroelectric", "지열", "geothermal"], "renewable-energy"),
    # 주의: 수소/연료전지 → petroleum 매핑은 의도적으로 제거됨 — EIA 에 수소 전용
    # 경로가 없어 석유 개요 페이지가 S급 노이즈로 투입되던 문제(평가 보고서 H2).
    (["석유", "petroleum", "oil", "crude", "원유", "정유", "lpg", "lng"], "petroleum"),
    (["천연가스", "natural gas", "gas", "가스"], "natural-gas"),
    (["석탄", "coal"], "coal"),
    (["원자력", "nuclear", "핵", "원전", "smr"], "nuclear-outages"),
    (["co2", "탄소", "carbon", "배출", "emission", "온실", "greenhouse"], "co2-emissions"),
    (["에너지", "energy", "총괄", "total"], "total-energy"),
    (["국제", "international", "세계", "global", "world"], "international"),
    (["전망", "outlook", "forecast", "projection", "단기"], "steo"),
    (["주", "state", "지역", "regional", "seds"], "seds"),
    (["수입", "import", "crude oil import"], "crude-oil-imports"),
]

# EIA v2 경로 → 웹 페이지 URL (fetch.py 가 HTML 다운로드 가능)
_ROUTE_URLS: dict[str, str] = {
    "electricity":       f"{EIA_WEB}/electricity/",
    "renewable-energy":  f"{EIA_WEB}/energyexplained/renewable-sources/",
    "petroleum":         f"{EIA_WEB}/petroleum/",
    "natural-gas":       f"{EIA_WEB}/naturalgas/",
    "coal":              f"{EIA_WEB}/coal/",
    "nuclear-outages":   f"{EIA_WEB}/nuclear/",
    "co2-emissions":     f"{EIA_WEB}/environment/emissions/carbon/",
    "total-energy":      f"{EIA_WEB}/totalenergy/",
    "international":     f"{EIA_WEB}/international/",
    "steo":              f"{EIA_WEB}/outlooks/steo/",
    "seds":              f"{EIA_WEB}/state/",
    "crude-oil-imports": f"{EIA_WEB}/petroleum/imports/crude/",
}


def match_routes(query: str) -> list[str]:
    """쿼리 키워드를 EIA v2 경로 목록으로 변환. 중복 없이 순서 보존.

    매칭되는 경로가 없으면 빈 리스트 — 이전의 total-energy 기본값은 모든 쿼리에
    내용 없는 스텁 문서를 끼워 넣는 노이즈였다(평가 보고서 H2).
    """
    q_lower = query.lower()
    seen: set[str] = set()
    routes: list[str] = []
    for keywords, route_id in _KEYWORD_ROUTES:
        if any(kw in q_lower for kw in keywords):
            if route_id not in seen:
                seen.add(route_id)
                routes.append(route_id)
    return routes


def _route_meta(api_key: str, route_id: str) -> dict:
    """EIA v2 route 메타데이터(설명·날짜범위·하위경로) 반환. 실패 시 빈 dict."""
    try:
        r = httpx.get(
            f"{EIA_BASE}/{route_id}/",
            params={"api_key": api_key},
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        r.raise_for_status()
        return r.json().get("response", {})
    except Exception:
        return {}


def _build_content(route_id: str, meta: dict) -> str:
    """Document.content 문자열 구성 — 설명 + 날짜 범위 + 주요 하위 데이터셋."""
    desc      = meta.get("description") or route_id
    start     = meta.get("startPeriod", "")
    end       = meta.get("endPeriod") or meta.get("latestPeriod", "")
    sub_routes = meta.get("routes", [])[:5]

    lines = [f"[EIA] {desc}"]
    if start and end:
        lines.append(f"데이터 범위: {start} ~ {end}")
    if sub_routes:
        lines.append("주요 데이터셋:")
        for sr in sub_routes:
            sr_desc = sr.get("description") or sr.get("id", "")
            lines.append(f"  - {sr_desc}")
    if not meta:
        lines.append("출처: 미국 에너지정보청(EIA) 공식 통계.")
    return "\n".join(lines)


def eia_search(query: str, limit: int = 5) -> list[Document]:
    """EIA API v2 — 쿼리 키워드로 에너지 통계 데이터셋 Document 반환.

    source='eia', source_type='stats'. URL은 EIA 웹 보고서 페이지(fetch.py 가능).
    """
    api_key = os.environ.get("EIA_API_KEY", "")
    if not api_key:
        raise ValueError("EIA_API_KEY not set")

    routes = match_routes(query)
    docs: list[Document] = []

    for route_id in routes[:limit]:
        meta    = _route_meta(api_key, route_id)
        content = _build_content(route_id, meta)
        desc    = meta.get("description") or route_id
        end     = meta.get("endPeriod") or meta.get("latestPeriod", "")
        web_url = _ROUTE_URLS.get(route_id, f"{EIA_WEB}/")

        docs.append(Document(
            title=f"[EIA] {desc}",
            url=web_url,
            content=content,
            source="eia",
            source_type="stats",
            published=end[:10] if end else date.today().isoformat(),
            metadata={"route": route_id, "start": meta.get("startPeriod", ""), "end": end},
        ))

    return docs


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    queries = ["renewable energy solar wind", "수소 연료전지", "electricity generation"]
    for q in queries:
        print(f"\n=== EIA: '{q}' ===")
        try:
            results = eia_search(q, limit=3)
            if results:
                for d in results:
                    print(f"  {d.short()}")
                    for line in d.content.split("\n")[:4]:
                        print(f"    {line}")
            else:
                print("  결과 없음")
        except Exception as e:
            print(f"  실패: {type(e).__name__}: {e}")
