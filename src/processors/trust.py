"""신뢰도 스코어링 — 각 자료에 출처 등급과 점수(0~100)를 부여한다.

등급 기준:
  SS(95) S 출처 + 발행일 1년 이내 (최신 공인 1차 자료)
  S (90) 국제기구·정부기관 1차 자료 (IEA, IRENA, DOE, NREL, EIA, IAEA, World Bank, OECD, 국내 에너지기관)
         + 미등록 정부 도메인 폴백(.gov / .go.kr)
  AA(80) A 출처 + 발행일 1년 이내 (최신 피어리뷰)
  A (75) 피어리뷰 학술 출판 (해외 주요 출판사 + 국내 학술 플랫폼·학회지)
  B (52) 기업·산업·언론 참고 자료 — 산업 리서치·전문지 큐레이션은 +8 가점
  C (40) 개인·소셜(블로그·LinkedIn 등)·백과(위키)·출처 불명 — 팩트 확인 보조용

여기에 최신성(발행일) 보너스를 더한다. 점수 내림차순으로 정렬해 반환한다.

실행 예시 (등급 분포 점검):
    .venv\\Scripts\\python.exe -m src.processors.trust output\\raw\\xxx.json
"""
import sys
import json
import re
from datetime import date
from urllib.parse import urlparse

from src.models import Document

# 국제기구·정부기관 (1차 공인 자료)
S_DOMAINS = {
    "iea.org", "irena.org", "energy.gov", "nrel.gov", "eia.gov",
    "iaea.org", "worldbank.org", "oecd.org", "un.org", "unsdsn.org",
    "kier.re.kr", "keei.re.kr", "energy.or.kr", "kosis.kr",
    # 한국 에너지·R&D 공공기관 (KETEP 사업계획서 1차 출처)
    "ketep.re.kr", "kistep.re.kr", "kipris.or.kr", "ntis.go.kr",
    "data.go.kr", "odcloud.kr",                   # 공공데이터포털 본체·Open Data Cloud
    "motie.go.kr", "me.go.kr", "knrec.or.kr", "kisti.re.kr",
    "nl.go.kr", "nanet.go.kr",                    # 국회도서관·디지털도서관
    "prism.go.kr",                                # 국가R&D정보관리시스템
    # 미국 DOE 산하기관·국립연구소 — energy.gov 만으론 커버 안 됨
    # (실측: netl.doe.gov, science.osti.gov 가 B 등급으로 저평가)
    "doe.gov", "osti.gov", "ornl.gov", "pnnl.gov", "anl.gov",
    "inl.gov", "sandia.gov", "llnl.gov", "lbl.gov",
    # 해외 정부·국제기구 일반
    "europa.eu", "gov.uk",
}

# 피어리뷰 학술 출판
A_DOMAINS = {
    "nature.com", "science.org",
    "sciencedirect.com", "springer.com", "link.springer.com",
    "wiley.com", "onlinelibrary.wiley.com", "acs.org", "pubs.acs.org",
    "rsc.org", "pubs.rsc.org", "ieee.org", "ieeexplore.ieee.org",
    "mdpi.com", "ncbi.nlm.nih.gov", "arxiv.org", "semanticscholar.org",
    # 해외 주요 출판사 보강
    "tandfonline.com", "iopscience.iop.org", "frontiersin.org",
    "academic.oup.com", "cambridge.org", "pnas.org", "cell.com",
    # 국내 학술 플랫폼·학회지 — 실측에서 동료심사 학술지가 B(52)로
    # 블로그와 동점이던 공백 보정 (한국수소및신에너지학회·대한환경공학회지 등)
    "koreascience.kr", "kci.go.kr", "journal.hydrogen.or.kr",
    "jksee.or.kr", "dbpia.co.kr",
}

# 개인·소셜 출처 — 검증 책임자가 없는 글. 팩트 확인 보조용(C)으로만 쓴다.
# B(52) 버킷에서 학술지·전문지와 동점이 되는 것을 막는다(실측: LinkedIn
# 게시물·네이버 블로그가 산업 리서치와 같은 점수).
C_DOMAINS = {
    "linkedin.com", "facebook.com", "x.com", "twitter.com",
    "instagram.com", "reddit.com", "youtube.com", "medium.com",
    "blog.naver.com", "post.naver.com", "cafe.naver.com",
    "tistory.com", "brunch.co.kr", "namu.wiki", "slideshare.net",
}

# 산업 리서치·전문지 — 등급은 B 유지하되 점수 +8 (B+ 성격).
# 시장규모·LCOE 등 정량 데이터는 학술지가 아니라 이 출처군에서 나온다(19차:
# 'LCOE 자료 33건 전부 B라 탈락' 문제의 근본 보정).
INDUSTRY_DOMAINS = {
    "bnef.com", "idtechex.com", "woodmac.com", "mckinsey.com",
    "bcg.com", "spglobal.com", "dnv.com", "rystadenergy.com",
    "gasworld.com", "hydrogeninsight.com", "rechargenews.com",
    "h2news.kr", "pv-magazine.com", "energy-storage.news",
}
INDUSTRY_BONUS = 8

GRADE_BASE = {"SS": 95, "S": 90, "AA": 80, "A": 75, "B": 52, "C": 40}

# URL 경로에서 연도를 추출하는 패턴 (arXiv ID, 날짜 경로 세그먼트, 파일명 등)
_ARXIV_PAT = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{2})(\d{2})[.\-/]")
_URL_YEAR_PAT = re.compile(r"(?<!\d)(20[2-9]\d)(?!\d)")


def infer_year_from_url(url: str) -> int | None:
    """URL에서 발행 연도를 추론한다. 실패 시 None.

    arXiv ID (yymm 형식) → 연도 변환이 최우선.
    그 다음 URL 경로·파라미터·파일명에서 연도 후보를 찾되, '올해+1'을 넘는
    값은 발행연도일 수 없으므로 버린다 — MDPI 저널번호(ISSN 2071-1050 등)와
    로드맵 목표연도(net-zero-2050)가 발행연도로 오인되어 AA/SS 로 잘못
    승급되던 버그 수정(실측 14건). 유효 후보가 여럿이면 가장 최근 값.
    """
    max_valid = date.today().year + 1

    arxiv_m = _ARXIV_PAT.search(url)
    if arxiv_m:
        # arXiv YY 접두사: 00~99 → 2000~2099
        year = 2000 + int(arxiv_m.group(1))
        if year <= max_valid:
            return year

    years = [int(y) for y in _URL_YEAR_PAT.findall(url) if int(y) <= max_valid]
    if years:
        return max(years)
    return None


def domain_of(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _resolve_year(published: str | None, url: str = "") -> int | None:
    """발행 연도를 확정한다. published 우선, 없으면 URL 추론.

    published 도 범위(1900~올해+1)를 검증한다 — API가 쓰레기 값을 주는
    경우가 실재한다(실측: EIA published='2121-12' → SS|100 으로 오승급).
    범위 밖이면 URL 추론으로 폴백.
    """
    if published:
        try:
            year = int(str(published)[:4])
            if 1900 <= year <= date.today().year + 1:
                return year
        except (ValueError, TypeError):
            pass
    return infer_year_from_url(url) if url else None


def recency_bonus(published: str | None, url: str = "") -> int:
    """발행일이 최근일수록 가산 (최대 +6). 날짜 미상은 URL 추론으로 보완."""
    year = _resolve_year(published, url)
    if year is None:
        return 0
    age = date.today().year - year
    if age <= 1:
        return 6
    if age <= 3:
        return 4
    if age <= 5:
        return 2
    return 0


def citation_bonus(metadata: dict | None) -> int:
    """인용수가 많을수록 가점 (최대 +5). Semantic Scholar 만 제공."""
    c = (metadata or {}).get("citationCount")
    if not c:
        return 0
    if c >= 100:
        return 5
    if c >= 30:
        return 3
    if c >= 10:
        return 1
    return 0


def grade_document(doc: Document) -> tuple[str, float]:
    netloc = domain_of(doc.url)

    def _in(domains: set[str]) -> bool:
        return any(netloc == d or netloc.endswith("." + d) for d in domains)

    if _in(S_DOMAINS):
        base_grade = "S"
    elif _in(A_DOMAINS):
        base_grade = "A"
    elif _in(C_DOMAINS) or netloc.endswith("wikipedia.org"):
        # 개인·소셜·백과 — source_type 추정('paper'·'web')보다 우선한다.
        # 위키류는 보고서 프롬프트의 등급 정의([C] 참고용 위키백과 등)와 일치시킴.
        base_grade = "C"
    elif netloc.endswith(".gov") or netloc.endswith(".go.kr"):
        base_grade = "S"  # 미등록 정부 도메인 폴백 (실측: netl.doe.gov 가 B였음)
    elif doc.source_type == "paper":
        base_grade = "A"  # Exa 가 논문으로 분류했으나 도메인 미등록
    elif doc.source_type == "web":
        base_grade = "B"
    else:
        base_grade = "C"

    # 1년 이내 최신 자료는 S→SS, A→AA로 승급 (published 없으면 URL 연도 추론)
    year = _resolve_year(doc.published, doc.url)
    is_recent = year is not None and (date.today().year - year) <= 1
    if is_recent and base_grade == "S":
        grade = "SS"
    elif is_recent and base_grade == "A":
        grade = "AA"
    else:
        grade = base_grade

    score = (GRADE_BASE[grade]
             + recency_bonus(doc.published, doc.url)
             + citation_bonus(doc.metadata))
    if grade == "B" and _in(INDUSTRY_DOMAINS):
        score += INDUSTRY_BONUS  # 산업 리서치·전문지 — B 버킷 내 우선순위 상향
    return grade, min(100.0, score)


def annotate(docs: list[Document]) -> list[Document]:
    """각 문서에 등급/점수를 부여하고 신뢰도 내림차순으로 정렬한다."""
    for d in docs:
        d.trust_grade, d.trust_score = grade_document(d)
    docs.sort(key=lambda d: (d.trust_score or 0,
                             d.published or ""), reverse=True)
    return docs


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python -m src.processors.trust <raw_json_path>")
        sys.exit(1)

    payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
    docs = [Document(**{k: v for k, v in d.items()
                        if k in Document.__dataclass_fields__})
            for d in payload["documents"]]
    docs = annotate(docs)

    dist: dict[str, int] = {}
    for d in docs:
        dist[d.trust_grade] = dist.get(d.trust_grade, 0) + 1
    print(f"[trust] 등급 분포: {dict(sorted(dist.items()))}\n")
    for d in docs:
        print(" ", d.short())
