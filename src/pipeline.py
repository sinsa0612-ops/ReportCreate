"""수집 오케스트레이터 — 주제(쿼리)를 받아 전 소스에서 모아 저장한다.

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.pipeline "solid-state battery commercialization"
    .venv\\Scripts\\python.exe -m src.pipeline "query1" "query2" "query3"

결과는 output/raw/{slug}_{date}.json 으로 저장된다.
이후 보고서 작성은 Claude Code 세션이 이 JSON을 읽어 직접 수행한다.
"""
import sys
import re
import json
import datetime
from pathlib import Path
from dataclasses import asdict

from dotenv import load_dotenv

from src.models import Document
from src.collectors.search import tavily_search, exa_search
from src.collectors.academic import arxiv_search, semantic_scholar_search
from src.collectors.gov_kr import (
    kosis_search, gas_safety_search,
    kipris_search, kipris_foreign_search,
    nalib_search,
)
from src.collectors.eia import eia_search
from src.processors.trust import annotate
from src.processors.relevance import filter_relevant

load_dotenv()

# 목록형(메타데이터 위주) 소스의 런 단위 상한 — 쿼리마다 limit 건씩 누적되어
# 같은 데이터셋에서 수십 건(실측 가스안전 22~35건)이 들어오는 것을 막는다.
# 상한은 '먼저 수집된 순'(앞 쿼리 우선)으로 적용된다.
RUN_CAPS: dict[str, int] = {
    "gas_safety": 8, "kipris": 8, "kipris_foreign": 8,
    "kosis": 5, "eia": 5, "nalib": 8,
}


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:50] or "untitled"


def _safe(fn, *args, **kwargs) -> list[Document]:
    """수집기 하나가 실패(rate limit·타임아웃 등)해도 전체 수집은 계속한다."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"  [warn] {fn.__name__} 실패: {type(e).__name__}: {str(e)[:70]}")
        return []


def _is_korean(query: str) -> bool:
    return bool(re.search(r"[가-힣]", query))


def _dedup_by_title(docs: list[Document]) -> tuple[list[Document], int]:
    """제목 기반 준중복 제거 — 같은 논문이 Exa·arXiv·S2 에서 다른 URL 로
    들어오는 케이스(실측 ~3%)를 거른다.

    정규화 제목이 20자 이상일 때만 비교한다 — 'WATER ELECTROLYZER' 같은
    짧은 동명 특허들이 서로 다른 문서인데 지워지는 오삭제를 막는다.
    중복이면 본문(스니펫)이 더 긴 쪽을 남긴다.
    """
    by_title: dict[str, int] = {}
    out: list[Document] = []
    removed = 0
    for d in docs:
        key = re.sub(r"[^\w]", "", (d.title or "").lower())
        if len(key) >= 20 and key in by_title:
            removed += 1
            j = by_title[key]
            if len(d.content or "") > len(out[j].content or ""):
                out[j] = d
            continue
        if len(key) >= 20:
            by_title[key] = len(out)
        out.append(d)
    return out, removed


def _report_source_counts(docs: list[Document], queries: list[str]) -> None:
    """소스별 수집 건수를 출력하고, 소스군이 통째로 빈 경우 경고한다.

    수집기 실패는 _safe 가 삼키므로, API 키 만료·쿼터 소진으로 핵심 소스가
    0건이어도 파이프라인이 조용히 끝까지 진행된다 — 빈약한 보고서가 나온
    뒤에야 알게 되는 사고를 여기서 조기에 드러낸다.
    """
    by_src: dict[str, int] = {}
    for d in docs:
        by_src[d.source] = by_src.get(d.source, 0) + 1
    summary = ", ".join(f"{s}={n}" for s, n in
                        sorted(by_src.items(), key=lambda kv: -kv[1]))
    print(f"  [collect] 소스별 수집: {summary or '(0건)'}")

    if not by_src.get("tavily"):
        print("  [경고] Tavily 0건 — API 키·쿼터를 확인하세요 (웹 검색 전체 누락)")
    if any(not _is_korean(q) for q in queries) and not any(
            by_src.get(s) for s in ("exa", "arxiv", "semantic_scholar")):
        print("  [경고] 학술 소스(Exa·arXiv·S2) 0건 — API 키·쿼터를 확인하세요")
    if any(_is_korean(q) for q in queries) and not any(
            by_src.get(s) for s in ("kosis", "gas_safety", "kipris", "nalib")):
        print("  [경고] 국내 API 소스 0건 — API 키·쿼터를 확인하세요")


def collect(queries: list[str], per_query: int = 5) -> list[Document]:
    """전 소스에서 수집하고 URL 중복 제거 → 소스 상한 → 관련성 필터 → 등급 부여.

    쿼리 언어로 소스를 라우팅한다 — 한국어 쿼리는 영어 특화 소스(Exa·arXiv·S2·
    EIA·해외특허)에서 수확이 거의 0이고 그 반대도 마찬가지라, 언어에 맞는
    소스만 호출해 API 쿼터와 시간을 아낀다. Tavily 는 양쪽 모두 처리.
    """
    docs: list[Document] = []
    for q in queries:
        docs += _safe(tavily_search, q, max_results=per_query)
        if _is_korean(q):
            # 한국어 쿼리 → 국내 정부·도서관 API
            docs += _safe(kosis_search, q, limit=per_query)
            docs += _safe(gas_safety_search, q, limit=per_query)
            docs += _safe(kipris_search, q, limit=per_query)
            docs += _safe(nalib_search, q, limit=per_query)
        else:
            # 영어 쿼리 → 학술·해외 통계·해외특허
            docs += _safe(exa_search, q, num_results=per_query,
                          category="research paper")
            docs += _safe(arxiv_search, q, max_results=per_query)
            docs += _safe(semantic_scholar_search, q, limit=per_query)
            docs += _safe(eia_search, q, limit=per_query)
            docs += _safe(kipris_foreign_search, q, limit=per_query)

    _report_source_counts(docs, queries)

    seen: set[str] = set()
    uniq: list[Document] = []
    for d in docs:
        if not d.url or d.url in seen:
            continue
        seen.add(d.url)
        uniq.append(d)

    uniq, t_dup = _dedup_by_title(uniq)
    if t_dup:
        print(f"  [dedup] 제목 중복 {t_dup}건 제거")

    # 목록형 소스 런 단위 상한
    counts: dict[str, int] = {}
    capped: list[Document] = []
    over = 0
    for d in uniq:
        cap = RUN_CAPS.get(d.source)
        if cap is not None:
            if counts.get(d.source, 0) >= cap:
                over += 1
                continue
            counts[d.source] = counts.get(d.source, 0) + 1
        capped.append(d)
    if over:
        print(f"  [cap] 목록형 소스 상한 초과 {over}건 제외")

    # 관련성 필터: 주제 무관 자료(arXiv 노이즈 등) 제외
    kept, dropped = filter_relevant(capped, queries)
    if dropped:
        print(f"  [relevance] 주제 무관 {len(dropped)}건 제외")

    return annotate(kept)  # 신뢰도 등급 부여 + 점수 내림차순 정렬


def save_raw(slug: str, queries: list[str], docs: list[Document],
             template: str | None = None) -> Path:
    out_dir = Path("output/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    path = out_dir / f"{slug}_{date}.json"
    payload = {
        "queries": queries,
        "collected_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(docs),
        # 보고서 양식(templates/<name>.json). 없으면 기본 양식(에너지 7챕터).
        # 개요·챕터작성·검증 전 단계가 이 값을 읽어 같은 양식을 공유한다.
        "template": template,
        "documents": [asdict(d) for d in docs],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


if __name__ == "__main__":
    queries = sys.argv[1:] or ["next-generation energy storage technology"]
    print(f"[collect] queries = {queries}")

    docs = collect(queries)
    slug = slugify(queries[0])
    path = save_raw(slug, queries, docs)

    print(f"[collect] {len(docs)} unique docs -> {path}\n")
    for d in docs:
        print("  ", d.short())
