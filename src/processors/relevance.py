"""관련성 필터 — 출처 신뢰도와 별개로 '주제에 실제로 맞는가'를 거른다.

원리(2단계):
  1) 쿼리에서 핵심 용어를 뽑아, 각 자료(title+content)가 몇 개 포함하는지 센다.
  2) 용어를 강신호(도메인 핵심어)와 범용어로 나눈다. 범용어만 여러 개 맞춘
     자료(LLM 평가·지진·줄기세포 등)는 강신호가 없으면 제외한다.

  범용어란 system·reliability·evaluation·prediction·시스템·평가 처럼 어느
  분야에나 등장하는 일반 기술어. 강신호란 그것들을 뺀 나머지(fuel·연료전지 등)로,
  쿼리에서 자동 도출되므로 주제가 바뀌어도 적응한다(perovskite·photovoltaic 등).

실행 예시 (분류 점검):
    .venv\\Scripts\\python.exe -m src.processors.relevance output\\raw\\xxx.json
"""
import re

from src.models import Document

STOPWORDS = {
    "and", "or", "the", "for", "with", "from", "via", "of", "to", "in",
    "on", "an", "vs", "using", "based", "toward", "towards", "into",
}

# 한국어 불용어(조사·접속·일반어 최소셋). query_terms는 어절 단위라
# 조사는 대개 분리되어 나오므로 최소만 둔다.
STOPWORDS_KO = {
    "및", "또는", "그리고", "등", "위한", "관한", "대한", "통한", "있는", "통해",
}

# 범용 기술어 — 어느 분야 논문/자료에나 흔히 등장해 단독으로는 주제를 못 가른다.
# 쿼리 용어에서 이것들을 빼면 도메인 핵심어(강신호)만 남는다.
GENERIC_TERMS = {
    # 영어
    "system", "systems", "method", "methods", "methodology",
    "analysis", "assessment", "evaluation", "prediction", "predictive",
    "performance", "reliability", "model", "modeling", "modelling",
    "technology", "technologies", "application", "applications",
    "review", "study", "studies", "research", "development",
    "design", "management", "monitoring", "diagnosis", "diagnostic",
    "durability", "degradation", "lifetime", "efficiency",
    "cell", "cells", "distributed", "distribution", "stationary",
    "network", "networks", "data", "approach", "framework",
    "comparison", "verification", "estimation", "condition", "control",
    # 한국어
    "시스템", "기술", "평가", "분석", "방법", "동향", "시장", "정책",
    "신뢰성", "진단", "수명", "성능", "개발", "연구", "적용", "관리",
    "분산형", "분산발전", "열화", "모델",
}


# 강신호 동의어/약어 확장 — 쿼리에 앵커(왼쪽 키)가 있으면 값을 강신호로 추가한다.
# 약어만 쓴 자료(PEMFC=Proton Exchange Membrane Fuel Cell)가 누락되는 것을 막는다.
# 에너지 도메인 큐레이션(코드베이스의 TRUSTED_DOMAINS·S_DOMAINS 와 동일 성격).
SYNONYMS: dict[str, set[str]] = {
    "연료전지": {"fuel cell", "pemfc", "sofc", "pefc", "mcfc", "pafc", "dmfc", "soec"},
    "fuel":     {"pemfc", "sofc", "pefc", "mcfc", "pafc", "dmfc", "soec"},
    "수소":     {"hydrogen"},
    "hydrogen": {"수소"},
    "태양광":   {"photovoltaic", "perovskite"},
    "photovoltaic": {"태양광", "perovskite"},
    "배터리":   {"battery", "lithium"},
    "battery":  {"배터리", "lithium-ion"},
}


def strong_terms(terms: set[str]) -> set[str]:
    """쿼리 용어 중 범용어를 뺀 도메인 핵심어(강신호) 집합."""
    return {t for t in terms if t not in GENERIC_TERMS}


def strong_signals(terms: set[str]) -> set[str]:
    """강신호 집합 + 동의어/약어 확장. 필터 판정에 쓰는 최종 강신호.

    예: 쿼리에 '연료전지'/'fuel' 이 있으면 'pemfc','sofc' 등 약어가 강신호에 포함된다.
    """
    signals = strong_terms(terms)
    for anchor, syns in SYNONYMS.items():
        if anchor in terms:
            signals |= syns
    return signals


def query_terms(queries: list[str]) -> set[str]:
    """쿼리에서 핵심 용어를 추출한다.

    영어: 3글자+ 단어(불용어 제외). 한국어: 2글자+ 어절(불용어 제외).
    한국어는 조사가 붙어도 본문 substring 매칭으로 잡히므로 어절 그대로 쓴다.
    """
    terms: set[str] = set()
    for q in queries:
        for w in re.findall(r"[a-z]{3,}", q.lower()):
            if w not in STOPWORDS:
                terms.add(w)
        for w in re.findall(r"[가-힣]{2,}", q):
            if w not in STOPWORDS_KO:
                terms.add(w)
    return terms


def gap_terms(queries: list[str]) -> set[str]:
    """쿼리 목록에서 판별력 있는 검색 용어를 추출한다(불용어·범용어 제외).

    검증 루프의 탈락 풀 승격(19차)과 패치 신규자료 그룹 분류(21차 문제2)가
    공유하는 헬퍼 — 원래 report_validator._gap_terms 였으나 staged_report 도
    쓰게 되어 임포트 순환 없는 이곳으로 이동.
    """
    terms: set[str] = set()
    for q in queries:
        for w in re.findall(r"[a-zA-Z]{3,}", q.lower()):
            if w not in STOPWORDS and w not in GENERIC_TERMS:
                terms.add(w)
        for w in re.findall(r"[가-힣]{2,}", q):
            if w not in GENERIC_TERMS:
                terms.add(w)
    return terms


def term_hits(doc: dict, terms: set[str]) -> int:
    """문서 dict(제목+스니펫)가 용어 몇 개를 포함하는지 — 키워드 매칭 점수.

    영어는 \\b 단어 경계('usd'≠'used'), 한국어는 부분 매칭.
    """
    text = ((doc.get("title") or "") + " " + (doc.get("content") or "")).lower()
    hits = 0
    for t in terms:
        if t.isascii():
            if re.search(rf"\b{re.escape(t)}\b", text):
                hits += 1
        elif t in text:
            hits += 1
    return hits


def relevance_hits(doc: Document, terms: set[str]) -> int:
    text = (doc.title + " " + (doc.content or "")).lower()
    return sum(1 for t in terms if t in text)


# 서버사이드(또는 로컬 키워드)로 이미 주제 필터된 소스 — 강신호 요구 면제
GOV_SOURCES = {"kosis", "gas_safety", "eia", "kipris", "kipris_foreign", "nalib"}

# 특허 소스는 면제에서 제외하고 '제목 강신호'를 필수로 요구한다(21차 문제4).
# 특허 검색 API의 word/free 광범위 매칭이 무관 특허(줄기세포·면역항암 등)를
# 반환하는데, 본문이 없어 약한 면제 판정(total≥1)을 쉽게 통과했다.
# 가스안전공사 등 본문 있는 정부 소스는 면제 유지.
PATENT_SOURCES = {"kipris", "kipris_foreign"}


def filter_relevant(docs: list[Document], queries: list[str],
                    min_hits: int = 2) -> tuple[list[Document], list[Document]]:
    """(관련, 무관) 으로 분리. doc.metadata 에 매칭 수를 기록한다.

    판정 규칙:
      - 특허 소스(kipris, kipris_foreign): **제목**에 강신호 ≥1 필수(21차 문제4).
        본문 없는 특허가 면제 판정을 약하게 통과해 무관 특허가 혼입되던 문제 차단.
        강신호가 없는 쿼리(전부 범용어)면 제목 총매칭 ≥1 로 폴백.
      - 그 외 정부 API 소스(kosis, gas_safety 등): 매칭 ≥1 이면 통과 (이미 주제 필터됨).
      - 강신호 용어가 존재하는 일반 쿼리: 강신호 ≥1 AND 총매칭 ≥ min_hits.
        → 범용어(system·reliability 등)만 맞춘 노이즈는 강신호가 없어 제외.
      - 쿼리가 전부 범용어라 강신호가 없을 때: 총매칭 ≥ min_hits 로 폴백(기존 동작).
    """
    terms = query_terms(queries)
    strong = strong_signals(terms)
    kept: list[Document] = []
    dropped: list[Document] = []
    for d in docs:
        total = relevance_hits(d, terms)
        s_hits = relevance_hits(d, strong)
        d.metadata["relevance"] = total
        d.metadata["relevance_strong"] = s_hits

        if d.source in PATENT_SOURCES:
            title = (d.title or "").lower()
            check = strong if strong else terms
            ok = any(t in title for t in check)
        elif d.source in GOV_SOURCES:
            ok = total >= 1
        elif strong:
            ok = s_hits >= 1 and total >= min_hits
        else:                       # 강신호 용어가 없는 쿼리 → 기존 동작 폴백
            ok = total >= min_hits

        (kept if ok else dropped).append(d)
    return kept, dropped


if __name__ == "__main__":
    import sys
    import json

    payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
    queries = payload["queries"]
    docs = [Document(**{k: v for k, v in d.items()
                        if k in Document.__dataclass_fields__})
            for d in payload["documents"]]
    kept, dropped = filter_relevant(docs, queries)

    print(f"쿼리 용어: {sorted(query_terms(queries))}\n")
    print(f"=== 관련 {len(kept)}건 ===")
    for d in sorted(kept, key=lambda x: x.metadata['relevance'], reverse=True):
        print(f"  ({d.metadata['relevance']:>2}) {d.title[:62]}")
    print(f"\n=== 제외 {len(dropped)}건 ===")
    for d in dropped:
        print(f"  ({d.metadata['relevance']:>2}) {d.title[:62]}")
