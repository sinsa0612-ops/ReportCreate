"""학술 공식 API 어댑터 — arXiv, Semantic Scholar (둘 다 키 불필요).

검색 레이어(Tavily/Exa)와 동일하게 list[Document] 를 반환한다.
Semantic Scholar 는 인용수(citationCount)를 metadata 에 담아 신뢰도 가점에 쓴다.

실행 예시 (단독 점검):
    .venv\\Scripts\\python.exe -m src.collectors.academic
"""
import time

import httpx
from xml.etree import ElementTree as ET

from src.models import Document

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"

S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = ("title,abstract,url,year,publicationDate,"
             "citationCount,authors,openAccessPdf")

TIMEOUT = httpx.Timeout(60.0, connect=10.0)
HEADERS = {"User-Agent": "Crawling-Research-Pipeline/1.0 (academic source collector)"}


def _get(url: str, params: dict, max_retry: int = 4) -> httpx.Response:
    """GET with 429/503/타임아웃 재시도 (arXiv·Semantic Scholar rate limit 대응)."""
    r = None
    for attempt in range(max_retry):
        try:
            r = httpx.get(url, params=params, timeout=TIMEOUT,
                          follow_redirects=True, headers=HEADERS)
        except httpx.TimeoutException:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 503):
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    if r is not None:
        r.raise_for_status()
    raise httpx.TimeoutException(f"{url}: {max_retry}회 재시도 실패")


def arxiv_search(query: str, max_results: int = 5) -> list[Document]:
    """arXiv Atom API. 관련도순 정렬."""
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    r = _get(ARXIV_API, params)
    root = ET.fromstring(r.text)

    docs: list[Document] = []
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        summary = (entry.findtext(f"{ATOM}summary") or "").strip()
        published = entry.findtext(f"{ATOM}published")

        abs_url, pdf_url = "", ""
        for link in entry.findall(f"{ATOM}link"):
            if link.get("rel") == "alternate":
                abs_url = link.get("href", "")
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
        authors = [a.findtext(f"{ATOM}name") for a in entry.findall(f"{ATOM}author")]

        if not (abs_url or pdf_url):
            continue
        docs.append(Document(
            title=title, url=abs_url or pdf_url, content=summary[:1500],
            source="arxiv", source_type="paper", published=published,
            metadata={"pdf_url": pdf_url, "authors": authors[:6]},
        ))
    return docs


# S2 무인증 호출은 rate limit 이 빡빡하다 — 같은 런에서 영어 쿼리 5개가 연속
# 호출되며 429 로 전량 실패하던 문제(21차 문제4)를 호출 간 최소 간격으로 완화.
# (_get 의 429 backoff 재시도와 별개로, 애초에 연사하지 않는 1차 방어.)
_S2_MIN_INTERVAL = 1.5
_s2_last_call = 0.0


def semantic_scholar_search(query: str, limit: int = 5) -> list[Document]:
    """Semantic Scholar Graph API. 인용수 포함."""
    global _s2_last_call
    wait = _S2_MIN_INTERVAL - (time.monotonic() - _s2_last_call)
    if wait > 0:
        time.sleep(wait)
    params = {"query": query, "limit": limit, "fields": S2_FIELDS}
    try:
        r = _get(S2_API, params)
    finally:
        _s2_last_call = time.monotonic()
    data = r.json()

    docs: list[Document] = []
    for p in data.get("data", []):
        pdf = (p.get("openAccessPdf") or {}).get("url", "") or ""
        url = p.get("url") or pdf
        if not url:
            continue
        pub = p.get("publicationDate") or (str(p["year"]) if p.get("year") else None)
        docs.append(Document(
            title=p.get("title") or "", url=url,
            content=(p.get("abstract") or "")[:1500],
            source="semantic_scholar", source_type="paper", published=pub,
            metadata={
                "citationCount": p.get("citationCount"),
                "authors": [a.get("name") for a in (p.get("authors") or [])][:6],
                "openAccessPdf": pdf,
                "year": p.get("year"),
            },
        ))
    return docs


if __name__ == "__main__":
    q = "green hydrogen electrolysis"
    print(f"=== Semantic Scholar: {q} ===")
    try:
        for d in semantic_scholar_search(q, 3):
            c = d.metadata.get("citationCount")
            print(" ", d.short(), f"· cited {c}")
    except Exception as e:
        print("  S2 실패:", type(e).__name__, str(e)[:80])

    print(f"\n=== arXiv: {q} ===")
    try:
        for d in arxiv_search(q, 3):
            print(" ", d.short())
    except Exception as e:
        print("  arXiv 실패:", type(e).__name__, str(e)[:80])
