"""검색 레이어 수집기 — Tavily(웹/보고서), Exa(학술/의미검색).

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.collectors.search
"""
import os

from src.models import Document


def tavily_search(query: str, max_results: int = 5,
                  include_domains: list[str] | None = None) -> list[Document]:
    """Tavily 검색 — URL + 핵심 요약을 함께 반환한다."""
    from tavily import TavilyClient

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    resp = client.search(
        query=query,
        search_depth="advanced",
        max_results=max_results,
        include_domains=include_domains or [],
    )
    docs: list[Document] = []
    for r in resp.get("results", []):
        docs.append(Document(
            title=r.get("title", ""),
            url=r.get("url", ""),
            content=r.get("content", ""),
            source="tavily",
            source_type="web",
            published=r.get("published_date"),
            score=r.get("score"),
        ))
    return docs


def exa_search(query: str, num_results: int = 5,
               category: str | None = None,
               include_domains: list[str] | None = None) -> list[Document]:
    """Exa 의미 기반 검색 — 학술/기술 문서에 강하다.

    category 예: "research paper", "news", "pdf"
    """
    from exa_py import Exa

    exa = Exa(api_key=os.environ["EXA_API_KEY"])
    kwargs: dict = {
        "type": "auto",
        "num_results": num_results,
        # 스니펫 용도로만 쓰므로 서버에서 1000자로 잘라 받는다 — 전문은 어차피
        # fetch 단계에서 원본 URL 로 다시 받으므로 전체 텍스트 수신은 중복 낭비.
        "contents": {"text": {"max_characters": 1000}},
    }
    if category:
        kwargs["category"] = category
    if include_domains:
        kwargs["include_domains"] = include_domains

    resp = exa.search(query, **kwargs)
    docs: list[Document] = []
    for r in resp.results:
        docs.append(Document(
            title=r.title or "",
            url=r.url,
            content=(r.text or "")[:1000],
            source="exa",
            source_type="paper" if category == "research paper" else "web",
            published=getattr(r, "published_date", None),
            score=getattr(r, "score", None),
        ))
    return docs


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    q = "next-generation energy storage technology breakthroughs"
    print(f"=== Tavily: {q} ===")
    for d in tavily_search(q, max_results=3):
        print(" ", d.short())

    print(f"\n=== Exa (research paper): {q} ===")
    for d in exa_search(q, num_results=3, category="research paper"):
        print(" ", d.short())
