"""보고서 검증·보완 루프 (2단계 모델 분업)

흐름:
  1. [리뷰]   agy "Gemini 3.1 Pro (High)" — tech-writer(Paige) 관점으로 강점·약점·데이터공백을
              자유 마크다운으로 분석 → output/reviews/{slug}-review-v{n}.md
  2. [쿼리]   agy "Gemini 3.5 Flash (Medium)" — 위 리뷰를 읽고 재수집 여부 판단 + 검색 쿼리 플랜을
              JSON 으로 산출 → output/reviews/{slug}-queries-v{n}.json
              (수집 문서 목록은 [투입]/[미투입] 라벨로 전달 — 미투입은 승격 가능)
  2.5 [승격]  공백 쿼리로 탈락 풀(수집됐으나 미투입)을 먼저 키워드 검색해 승격(19차).
              '고등급+최신' 자료로 충족된 공백은 웹 재수집 생략, 아니면 병행.
  3. [재수집] 풀로 못 메운 공백 쿼리만 추가 수집 (pipeline.collect)
  4. [병합]   기존 raw JSON 에 중복 없이 병합 → {slug}_{date}_v{n}.json 저장
  5. [재생성] 단계적 보고서 재생성 (개요→챕터(Opus 4.8)→조합)
  6. max_iterations(기본 1)까지 반복. should_trigger=False 또는 신규 0건이면 조기 종료.

설계 의도:
  - Pro 는 "분석"에만 집중(구조 강제 없는 서술), Flash 는 "액션 플랜"에만 집중(JSON).
  - YAML 스키마 강제를 폐기 → Pro 리뷰 품질↑, 파싱은 Flash JSON 한 번으로 단순화.

실행:
    .venv\\Scripts\\python.exe -m src.validators.report_validator <slug> [--max-iter N]
    .venv\\Scripts\\python.exe -m src.validators.report_validator <slug> --skip-review  # 기존 Pro 리뷰 재사용
"""
import sys
import re
import json
import time
import datetime
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.pipeline import collect
from src.collectors.fetch import fetch_sources
from src.generators.report import find_raw
from src.generators.staged_report import run_all as generate_report
from src.generators.staged_report import run_partial, _selected_docs
from src.locks import slug_lock
from src.processors.select import selection_score
from src.processors.trust import infer_year_from_url
from src.processors.relevance import gap_terms, term_hits
from src.report_templates import template_for_slug

PROJECT_ROOT = Path(__file__).parent.parent.parent
REPORT_DIR   = PROJECT_ROOT / "output" / "reports"
REVIEW_DIR   = PROJECT_ROOT / "output" / "reviews"
RAW_DIR      = PROJECT_ROOT / "output" / "raw"

REVIEW_MODEL = "Gemini 3.1 Pro (High)"       # 심층 분석 — 강점/약점/데이터공백
QUERY_MODEL  = "Gemini 3.5 Flash (Medium)"   # 빠른 액션 플랜 — 재수집 JSON
# 주의: agy 모델명은 버전에 따라 바뀐다(구 "Gemini 3.5 flash" → 티어 명시형으로 드리프트).
# 실패 시 `agy --print --model x` 가 유효 모델 목록을 출력하니 그에 맞춰 갱신할 것.

AGY_TIMEOUT        = 1200  # agy 프로세스 종료 대기(초)
AGY_PRINT_TIMEOUT  = "900s"
FILE_POLL_RETRIES  = 20    # 출력 파일 생성 폴링 횟수
FILE_POLL_INTERVAL = 3     # 폴링 간격(초)

BAR = "=" * 60


# ─── agy 프롬프트 ──────────────────────────────────────────────────────────

_REVIEW_PROMPT = """\
당신은 BMAD 프레임워크의 Paige(Technical Writer) 에이전트입니다.
Paige의 역할: 복잡한 기술 개념을 명확하고 구조화된 문서로 변환하고,
논리적 공백과 데이터 부족을 정확히 짚어냅니다. 모든 단어가 목적을 가져야 합니다.

@{report_rel} 파일을 읽고, 기술 문서 작성자의 관점에서 검토 의견을 작성하세요.

다음 세 가지를 빠짐없이 다루세요:

1. **강점** — 이 보고서가 잘 해낸 점. 구조·근거·완결성·명료성 측면에서 구체적으로.
2. **약점** — 논리적 비약, 근거 없는 주장, 흐름 단절 등 개선이 필요한 지점. 챕터를 명시.
3. **데이터 공백** — '데이터 없음'으로 비어 있거나 근거가 약해, 추가 자료를 수집하면
   메울 수 있는 부분. 각 공백이 어떤 종류의 자료(정량 스펙·시장 데이터·정책 문서 등)를
   필요로 하는지 설명.
{audit_section}
형식은 자유로운 마크다운입니다. 표·목록·소제목을 적절히 사용하되,
약점과 데이터 공백은 챕터 번호와 함께 누구나 후속 조치할 수 있을 만큼 구체적으로 쓰세요.

write_to_file 도구만 사용하세요. 셸 명령은 실행하지 마세요.
검토 의견을 {review_abs} 에 저장하세요."""


_QUERY_PROMPT = """\
당신은 자료 수집 플래너입니다. 아래 입력을 읽으세요.

- 원본 보고서: @{report_rel}
- 검토 의견(강점·약점·데이터 공백): @{review_rel}

이미 수집된 문서 목록 — [투입]=보고서 작성에 전달됨 / [미투입]=수집됐으나 미전달(내부 승격 가능):
{existing_docs}

보고서는 아래 {n_chapters}개 챕터로 구성됩니다(영향 챕터를 지목할 때 이 번호를 쓰세요):
{chapters_ref}

검토 의견을 두 갈래로 처리하세요.
(가) **데이터 공백** — 추가 자료 수집이 필요한 부분 → query_plan
(나) **논리 약점** — 자료가 아니라 글의 논리·구조·설명 보강으로 해결되는 부분 → revision_plan

아래 JSON 스키마 **그대로** 출력하여 {queries_abs} 에 저장하세요.
JSON 외의 다른 텍스트(설명·코드펜스)는 절대 포함하지 마세요.

{{
  "should_trigger": true,
  "verdict": "NEEDS_RECOLLECTION",
  "query_plan": [
    {{
      "rationale": "이 쿼리 묶음이 메우는 공백 설명",
      "priority": "high",
      "queries": ["한국어 쿼리", "English query 1", "English query 2"],
      "affected_chapters": [3, 4]
    }}
  ],
  "revision_plan": [
    {{
      "chapter": 5,
      "issue": "재수집 없이 고칠 논리·구조 약점 설명"
    }}
  ]
}}

규칙:
- verdict 선택지: APPROVED(보완 불필요) | NEEDS_REVISION(문체/구조만, 재수집 불필요) | NEEDS_RECOLLECTION(추가 자료 필요)
- 보완이 전혀 필요 없으면 verdict=APPROVED, should_trigger=false, query_plan=[], revision_plan=[] 로 두세요.
- 재수집이 필요 없으면 should_trigger=false, query_plan=[] (논리 약점만 있으면 revision_plan 은 채우세요).
- priority 선택지: critical | high | medium | low. medium 이하 공백은 query_plan 에 포함하지 마세요.
- 각 query_plan 항목 queries 는 한국어 1개 이상 + 영어 2개 이상 (과도한 중복 쿼리 금지).
- affected_chapters: 이 쿼리로 보강될 챕터 번호(1~7).
- [투입] 자료가 이미 충분히 다루는 주제만 쿼리에서 제외하세요.
  [미투입] 자료로 메울 수 있어 보이는 공백도 query_plan 에 쿼리를 작성하세요 —
  시스템이 그 쿼리로 [미투입] 풀을 먼저 검색해 내부 승격하고, 부족할 때만 웹을 검색합니다.
- revision_plan: 논리 약점이 있는 챕터만. 강점/사소한 스타일 지적은 제외.

write_to_file 도구만 사용하세요. 셸 명령은 실행하지 마세요."""


# 챕터 골격 참조(하위호환 기본값) — 실제로는 slug 의 양식에서 해석해 쓴다.
# Flash 가 영향 챕터를 지목할 때 쓰는 번호·제목 목록.
_CHAPTERS_REF = (
    "1. 기술 개요\n2. 부상 배경 및 시장 맥락\n3. 주요 기술 경로 분석\n"
    "4. 정량 비교 분석\n5. 쟁점 및 리스크\n6. 주요 플레이어 및 정책 환경\n"
    "7. 시사점 및 권고안"
)


# ─── agy 실행 공통 헬퍼 ─────────────────────────────────────────────────────

def _run_agy(model: str, prompt: str, out_path: Path) -> Path:
    """agy 를 model 로 실행하고, out_path 파일이 생성될 때까지 폴링한다."""
    print(f"  [agy:{model}] 요청 → {out_path.name}")
    subprocess.run(
        ["agy", "--model", model, "--print", prompt,
         "--print-timeout", AGY_PRINT_TIMEOUT],
        cwd=str(PROJECT_ROOT),
        timeout=AGY_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )

    # write_to_file 완료 대기 (agy 프로세스 종료 후 딜레이 가능)
    for _ in range(FILE_POLL_RETRIES):
        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"  [agy:{model}] 파일 생성 확인: {out_path.name} ({out_path.stat().st_size}B)")
            return out_path
        time.sleep(FILE_POLL_INTERVAL)

    raise RuntimeError(
        f"agy({model})가 파일을 생성하지 않았습니다: {out_path}\n"
        f"  → agy 로그: ~/.gemini/antigravity-cli/log/ 확인 필요"
    )


# ─── 1단계: Pro 리뷰 요청 ───────────────────────────────────────────────────

def request_review(slug: str, iteration: int) -> Path:
    """Pro 모델에 강점·약점·데이터공백 검토를 요청하고 리뷰 파일 경로를 반환한다."""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORT_DIR / f"{slug}.md"
    if not report_path.exists():
        raise FileNotFoundError(f"보고서를 찾을 수 없습니다: {report_path}")

    review_path = REVIEW_DIR / f"{slug}-review-v{iteration}.md"
    report_rel  = f"output/reports/{slug}.md"  # agy 는 PROJECT_ROOT 기준 실행

    # 수치 인용 감사(LLM 비용 0) — 원문에 없는 수치를 Pro 리뷰가 집중 점검하도록
    # 결과를 프롬프트에 첨부한다. 감사 실패는 리뷰 진행을 막지 않는다.
    audit_section = ""
    try:
        from src.validators.citation_audit import run_audit
        audit_path = REVIEW_DIR / f"{slug}-audit-v{iteration}.md"
        audit_md = run_audit(slug, audit_path)
        if audit_md:
            audit_section = f"\n{audit_md}"
    except Exception as e:
        print(f"  [audit] 수치 인용 감사 실패 (리뷰는 계속): {type(e).__name__}: {e}")

    prompt = _REVIEW_PROMPT.format(
        report_rel=report_rel,
        review_abs=str(review_path).replace("\\", "/"),
        audit_section=audit_section,
    )
    return _run_agy(REVIEW_MODEL, prompt, review_path)


# ─── 2단계: Flash 쿼리 플랜 ─────────────────────────────────────────────────

def _existing_docs_block(slug: str, limit: int = 120) -> str:
    """Flash 에 전달할 수집 문서 목록 — [투입]/[미투입] 라벨 포함(19차).

    투입 = 현재 보고서 작성에 전달된 문서(_selected_docs 기준).
    미투입 = 수집됐으나 선별에서 탈락한 문서 — 내부 승격 후보임을 Flash 가
    인지하도록 선별 점수 높은 순으로 보여준다. 투입 판별이 실패하면(테스트·
    레거시 등) 전체를 [미투입]으로 라벨링한다.
    """
    raw_path = find_raw(slug)
    payload  = json.loads(raw_path.read_text(encoding="utf-8"))
    docs     = payload.get("documents", [])
    try:
        selected, _q, _p = _selected_docs(slug)
        used = {d.get("url") for d in selected}
    except Exception:
        used = set()

    def _line(d: dict, label: str) -> str:
        return (f"- [{label}|{d.get('trust_grade', '?')}] "
                f"{(d.get('title') or '(제목없음)')[:80]}")

    used_docs = [d for d in docs if d.get("url") in used]
    pool_docs = sorted((d for d in docs if d.get("url") not in used),
                       key=selection_score, reverse=True)
    lines = [_line(d, "투입") for d in used_docs[:limit]]
    lines += [_line(d, "미투입") for d in pool_docs[:max(0, limit - len(lines))]]
    print(f"  [plan] 문서 목록 전달 — 투입 {len(used_docs)}건, "
          f"미투입 {len(pool_docs)}건 (표시 {len(lines)}건)")
    return "\n".join(lines) if lines else "(수집 문서 없음)"


def plan_queries(slug: str, review_path: Path, iteration: int) -> Path:
    """Flash 모델에 Pro 리뷰 기반 재수집 쿼리 플랜(JSON)을 요청하고 경로를 반환한다."""
    queries_path = REVIEW_DIR / f"{slug}-queries-v{iteration}.json"
    report_rel   = f"output/reports/{slug}.md"
    review_rel   = f"output/reviews/{review_path.name}"

    # 이미 수집된 문서 목록(투입/미투입 라벨)을 Flash에 전달해
    # 중복 쿼리를 막으면서도 미투입 자료의 승격 가능성은 열어둔다.
    try:
        existing_docs = _existing_docs_block(slug)
    except Exception as e:
        existing_docs = "(목록 로드 실패)"
        print(f"  [plan] 기존 문서 목록 로드 실패 (무시): {e}")

    tpl = template_for_slug(slug)
    prompt = _QUERY_PROMPT.format(
        report_rel=report_rel,
        review_rel=review_rel,
        queries_abs=str(queries_path).replace("\\", "/"),
        existing_docs=existing_docs,
        n_chapters=tpl.n_chapters,
        chapters_ref=tpl.chapters_ref(),
    )
    return _run_agy(QUERY_MODEL, prompt, queries_path)


def _extract_json(text: str) -> dict | None:
    """Flash 출력에서 JSON 객체를 robust 하게 추출한다(코드펜스·잡텍스트 허용)."""
    text = text.strip()
    # 1) 그대로 파싱
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2) ```json ... ``` 코드펜스 제거
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 3) 첫 { 부터 마지막 } 까지
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            pass
    return None


def parse_query_plan(queries_path: Path) -> tuple[bool, list[str]]:
    """Flash JSON 을 파싱해 (should_trigger, 중복 제거된 쿼리 목록)을 반환한다."""
    data = _extract_json(queries_path.read_text(encoding="utf-8"))
    if data is None:
        print(f"  [warn] 쿼리 플랜 JSON 파싱 실패: {queries_path.name}")
        return False, []

    should  = bool(data.get("should_trigger", False))
    verdict = data.get("verdict", "")
    plan    = data.get("query_plan", []) or []

    print(f"  [plan] verdict={verdict}  should_trigger={should}  groups={len(plan)}")

    if not should or verdict == "APPROVED":
        return False, []

    # query_plan 의 모든 쿼리를 순서 유지하며 평탄화 + 중복 제거
    seen: set[str] = set()
    queries: list[str] = []
    for group in plan:
        for q in group.get("queries", []) or []:
            q = str(q).strip()
            if q and q not in seen:
                seen.add(q)
                queries.append(q)

    if not queries:
        print("  [plan] should_trigger=true 이나 추출된 쿼리가 없음")
        return False, []

    print(f"  [plan] 재수집 쿼리 {len(queries)}개")
    return True, queries


# ─── 2단계 확장: 영향 챕터 식별 (부분 재생성용) ──────────────────────────────

@dataclass
class PlanResult:
    """Flash 플랜 파싱 결과 — 부분 재생성(stage2_patch)용 영향 챕터 포함.

    should_recollect:    재수집(추가 수집) 필요 여부
    queries:             재수집 쿼리(평탄화·중복 제거)
    recollect_chapters:  {챕터id: 사유} — 신규 자료로 보강될 챕터(데이터 공백)
    revision_chapters:   {챕터id: 사유} — 재수집 없이 고칠 챕터(논리 약점)
    query_groups:        공백(쿼리 그룹) 단위 원본 — 풀 승격·선별적 웹 재수집용(19차).
                         각 항목: {"queries": [...], "affected_chapters": [...],
                                   "rationale": str}
    """
    should_recollect: bool
    queries: list[str]
    recollect_chapters: dict[int, str]
    revision_chapters: dict[int, str]
    query_groups: list[dict] = field(default_factory=list)


def _as_chapter_id(v, max_id: int = 7) -> int | None:
    """1~max_id 범위의 챕터 번호로 정규화. 범위 밖·비정수는 None.

    max_id 는 양식(ReportTemplate)의 챕터 수 — 기본 7은 하위호환용 기본 양식 값.
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= max_id else None


def parse_plan(queries_path: Path, max_id: int = 7) -> PlanResult:
    """Flash JSON 을 파싱해 영향 챕터까지 포함한 PlanResult 를 반환한다."""
    data = _extract_json(queries_path.read_text(encoding="utf-8"))
    if data is None:
        print(f"  [warn] 쿼리 플랜 JSON 파싱 실패: {queries_path.name}")
        return PlanResult(False, [], {}, {})

    verdict = data.get("verdict", "")
    if verdict == "APPROVED":
        print("  [plan] verdict=APPROVED → 보완 불필요")
        return PlanResult(False, [], {}, {})

    should = bool(data.get("should_trigger", False))
    plan   = data.get("query_plan", []) or []
    rev    = data.get("revision_plan", []) or []

    # (가) 데이터 공백 — 쿼리 평탄화 + 영향 챕터 수집 + 그룹 원본 보존
    seen: set[str] = set()
    queries: list[str] = []
    recollect_chapters: dict[int, str] = {}
    query_groups: list[dict] = []
    for group in plan:
        group_queries = [str(q).strip() for q in (group.get("queries", []) or [])
                         if str(q).strip()]
        for q in group_queries:
            if q not in seen:
                seen.add(q)
                queries.append(q)
        rationale = str(group.get("rationale", "")).strip()
        chapter_ids = [cid for cid in
                       (_as_chapter_id(ch, max_id)
                        for ch in (group.get("affected_chapters", []) or []))
                       if cid is not None]
        for cid in chapter_ids:
            if cid in recollect_chapters and rationale:
                recollect_chapters[cid] += " / " + rationale
            else:
                recollect_chapters[cid] = rationale
        if group_queries:
            query_groups.append({"queries": group_queries,
                                 "affected_chapters": chapter_ids,
                                 "rationale": rationale})

    # (나) 논리 약점 — 재수집 없이 고칠 챕터
    revision_chapters: dict[int, str] = {}
    for r in rev:
        cid = _as_chapter_id(r.get("chapter"), max_id)
        if cid is not None:
            revision_chapters[cid] = str(r.get("issue", "")).strip()

    should_recollect = should and bool(queries)
    print(f"  [plan] verdict={verdict}  recollect={should_recollect}  "
          f"공백챕터={sorted(recollect_chapters)}  약점챕터={sorted(revision_chapters)}")

    # 언어 균형 점검 — collect() 는 쿼리 언어로 소스를 라우팅하므로,
    # 영어 쿼리가 없으면 학술·해외 소스가, 한국어가 없으면 국내 API가 아예 안 불린다.
    if should_recollect:
        if not any(re.search(r"[a-zA-Z]{3,}", q) for q in queries):
            print("  [warn] 재수집 쿼리에 영어가 없음 — 학술·해외 소스"
                  "(Exa·arXiv·S2·EIA·해외특허)는 호출되지 않습니다")
        if not any(re.search(r"[가-힣]", q) for q in queries):
            print("  [warn] 재수집 쿼리에 한국어가 없음 — 국내 정부·도서관 API는 "
                  "호출되지 않습니다")

    return PlanResult(should_recollect, queries, recollect_chapters,
                      revision_chapters, query_groups)


# ─── 2.5단계: 탈락 풀 승격 (웹 재수집 전, 19차) ─────────────────────────────

PROMOTE_PER_GAP  = 4          # 공백(쿼리 그룹)당 승격 상한
PROMOTE_MIN_HITS = 2          # 승격 후보 최소 키워드 일치 수(서로 다른 용어)
FRESH_YEARS      = 1          # '최신' 판정: 발행 연도 ≥ 올해 - 1
STRONG_GRADES    = {"SS", "S", "AA", "A"}


# 공유 헬퍼로 이동(relevance.py, 21차 문제2) — 기존 이름으로 재노출(테스트 호환).
_gap_terms = gap_terms
_term_hits = term_hits


def _doc_year(doc: dict) -> int | None:
    """발행 연도 — published 필드 우선, 없으면 URL 추론.

    published 도 범위(1900~올해+1)를 검증한다 — 쓰레기 값(실측: EIA
    '2121-12')이 '최신' 판정(_is_strong)을 오염시켜 웹 재수집을 잘못
    생략하게 만드는 것을 막는다.
    """
    m = re.match(r"(\d{4})", str(doc.get("published") or ""))
    if m:
        year = int(m.group(1))
        if 1900 <= year <= datetime.date.today().year + 1:
            return year
    return infer_year_from_url(doc.get("url") or "")


def _is_strong(doc: dict) -> bool:
    """웹 재수집을 생략해도 될 만큼 강한 자료인가 — 고등급 **그리고** 최신.

    이 기준에 못 미치는 승격(B/C급·연식 미상)은 빈칸보단 나으니 투입하되,
    웹 재수집도 병행해 더 새/더 권위 있는 자료를 찾을 기회를 잃지 않는다.
    """
    if (doc.get("trust_grade") or "") not in STRONG_GRADES:
        return False
    year = _doc_year(doc)
    return year is not None and year >= datetime.date.today().year - FRESH_YEARS


def _base_sources_dir(slug: str) -> Path:
    """첫 수집의 base `_sources` 폴더 — 검증 루프가 신규 원문을 누적하는 곳.

    검증본(`_v{n}.json`)은 검증 실행일 날짜라 첫 수집일과 다를 수 있다.
    최신 raw 의 base stem 폴더가 있으면 그것을, 없으면 slug 의 기존
    base `_sources` 폴더(글롭, `_v*` 제외) 중 최신을 쓴다.
    """
    stem = re.sub(r"_v\d+$", "", find_raw(slug).stem)
    direct = RAW_DIR / f"{stem}_sources"
    if direct.is_dir():
        return direct
    candidates = [c for c in sorted(RAW_DIR.glob(f"{slug}_*_sources"))
                  if c.is_dir() and not re.search(r"_v\d+_sources$", c.name)]
    return candidates[-1] if candidates else direct


def promote_from_pool(slug: str, plan: PlanResult,
                      iteration: int) -> tuple[int, list[str]]:
    """탈락 풀(수집됐으나 미투입)에서 공백 관련 문서를 승격한다 — 웹 재수집 전 호출.

    공백(쿼리 그룹)마다:
      1. 쿼리에서 판별 용어를 뽑아 풀을 키워드 검색 (LLM·네트워크 비용 0)
      2. (일치 수, 선별 점수) 순 상위 PROMOTE_PER_GAP 건 승격
      3. 승격분에 '고등급+최신' 자료가 있으면 그 공백의 웹 쿼리는 생략,
         없으면(B/C급 임시 충당) 웹 재수집도 병행

    승격 문서는 payload 의 promoted_urls(누적)·last_promoted_urls(이번 회차)에
    기록된다. _selected_docs 는 promoted_urls 를 무경쟁으로 항상 투입하고,
    _added_doc_indices 는 last_promoted_urls 를 대상 챕터에 주입한다.
    원문도 이 시점에 base _sources 폴더로 incremental fetch 한다.

    Returns (승격 건수, 여전히 웹 재수집이 필요한 쿼리 목록).
    """
    raw_path = find_raw(slug)
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    docs = payload.get("documents", [])
    already = set(payload.get("promoted_urls") or [])

    try:
        selected, _q, _p = _selected_docs(slug)
        used = {d.get("url") for d in selected}
    except Exception:
        used = set()
    pool = [d for d in docs
            if d.get("url") and d.get("url") not in used
            and d.get("url") not in already]

    promoted: list[dict] = []
    promoted_urls: set[str] = set()
    remaining: list[str] = []
    seen_q: set[str] = set()

    for group in plan.query_groups or []:
        terms = _gap_terms(group.get("queries") or [])
        picked: list[dict] = []
        if terms:
            cands = [(d, _term_hits(d, terms)) for d in pool
                     if d.get("url") not in promoted_urls]
            cands = [(d, h) for d, h in cands if h >= PROMOTE_MIN_HITS]
            cands.sort(key=lambda t: (-t[1], -selection_score(t[0])))  # 안정 정렬
            picked = [d for d, _h in cands[:PROMOTE_PER_GAP]]
        for d in picked:
            promoted.append(d)
            promoted_urls.add(d["url"])

        covered = any(_is_strong(d) for d in picked)
        if picked:
            label = "충족(웹 생략)" if covered else "임시 충당(웹 병행)"
            print(f"  [promote] 공백 '{(group.get('rationale') or '')[:40]}' — "
                  f"풀에서 {len(picked)}건 승격, {label}")
        if not covered:
            for q in group.get("queries") or []:
                if q not in seen_q:
                    seen_q.add(q)
                    remaining.append(q)

    if not promoted:
        return 0, plan.queries

    payload["promoted_urls"] = list(dict.fromkeys(
        (payload.get("promoted_urls") or []) + [d["url"] for d in promoted]))
    payload["last_promoted_urls"] = [d["url"] for d in promoted]

    date = datetime.date.today().isoformat()
    new_path = RAW_DIR / f"{slug}_{date}_v{iteration}.json"
    new_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"  [promote] 합계 {len(promoted)}건 승격 → {new_path.name} "
          f"(웹 재수집 필요 쿼리 {len(remaining)}개)")

    # 승격 문서는 초기 선별에서 탈락해 원문 fetch 를 건너뛰었다 — 지금 받는다.
    base_sources = _base_sources_dir(slug)
    try:
        fetch_sources(new_path, out_dir=base_sources, incremental=True,
                      only_urls=promoted_urls)
    except Exception as e:
        print(f"  [promote] 원문 저장 일부 실패: {type(e).__name__}: {e}")

    return len(promoted), remaining


# ─── 3~4단계: 재수집 + 병합 저장 ───────────────────────────────────────────

def collect_and_merge(slug: str, queries: list[str],
                      iteration: int) -> tuple[Path, int]:
    """쿼리로 추가 수집하고, 기존 raw JSON 에 중복 없이 병합해 새 파일로 저장한다.

    Returns (결과 JSON 경로, 실제 추가된 신규 문서 수).
    신규 문서가 0건이면 새 파일을 만들지 않고 (원본 경로, 0)을 반환한다.
    """
    print(f"  [collect] 갭 쿼리 {len(queries)}개 → 재수집 시작")
    new_docs = collect(queries, per_query=5)
    print(f"  [collect] {len(new_docs)}건 신규 수집")

    original_path = find_raw(slug)
    payload = json.loads(original_path.read_text(encoding="utf-8"))
    existing_urls = {d["url"] for d in payload["documents"] if d.get("url")}

    added = [d for d in new_docs if d.url and d.url not in existing_urls]
    if not added:
        print("  [collect] 새로운 URL 없음 (추가 0건)")
        return original_path, 0

    payload["documents"].extend([asdict(d) for d in added])
    payload["count"] = len(payload["documents"])
    payload["validated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    payload["validation_iteration"] = iteration
    payload["gap_queries"] = queries
    # 부분 재생성(stage2_patch)이 신규 문서를 대상 챕터에 주입하도록 URL 기록.
    payload["last_added_urls"] = [d.url for d in added]
    # 검증으로 추가된 전체 URL 누적 — _selected_docs 가 기본 선별과 분리해
    # 항상 투입 목록 뒤에 붙이는 근거(반복 2회차 이후에도 인덱스 안정 유지).
    payload["validation_added_urls"] = list(dict.fromkeys(
        (payload.get("validation_added_urls") or []) + [d.url for d in added]))

    # find_raw 는 alphabetical sort → _v{n} suffix 가 날짜 뒤라 최신으로 인식됨
    date = datetime.date.today().isoformat()
    new_path = RAW_DIR / f"{slug}_{date}_v{iteration}.json"
    new_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [merge] {len(added)}건 추가 → {new_path.name} (총 {payload['count']}건)")

    # 신규 문서 원문 저장: 첫 수집의 base _sources 폴더에 누적(기존 URL 재크롤링 안 함).
    # 추가분 '전량'이 아니라 보고서에 실제 투입될 문서만 받는다 — _selected_docs 가
    # ADDED_DOCS_CAP(12) 으로 절단하므로 전량 fetch 는 회차당 수십~백여 건의
    # 시간·대역폭 낭비였다(실측: 추가 159건 중 투입 12건). 첫 수집의
    # '선별 후 fetch' 원칙(평가 보고서 H3)을 검증 경로에도 적용한다(20차).
    # gas_safety 등 정부 API 문서도 manifest 에 pre_fetched 로 등록돼 보고서가 인식한다.
    # 폴더 계산은 _base_sources_dir — 검증본(_v{n})이 첫 수집과 다른 날짜라도
    # 기존 base 폴더를 정확히 찾는다(19차에서 stem 계산 버그 수정).
    base_sources = _base_sources_dir(slug)
    added_urls = {d.url for d in added}
    try:
        selected, _q2, _p2 = _selected_docs(slug)
        fetch_urls = {d.get("url") for d in selected} & added_urls
    except Exception as e:
        print(f"  [fetch] 투입 선별 계산 실패 — 추가분 전체 fetch 로 폴백: {e}")
        fetch_urls = added_urls
    print(f"  [fetch] 신규 {len(added)}건 중 투입 선별 {len(fetch_urls)}건 "
          f"원문 저장 → {base_sources.name}")
    try:
        fetch_sources(new_path, out_dir=base_sources, incremental=True,
                      only_urls=fetch_urls)
    except Exception as e:
        print(f"  [fetch] 원문 저장 일부 실패: {type(e).__name__}: {e}")

    return new_path, len(added)


# ─── 환각 수치 결정적 가드 (21차 문제1) ─────────────────────────────────────

DRAFTS_DIR = PROJECT_ROOT / "output" / "drafts"


def _locate_chapters_by_numbers(slug: str, tokens: list[str]) -> set[int]:
    """드래프트 ch0N.md 들을 직접 검색해, 해당 수치를 담은 챕터 id 집합을 찾는다.

    최종 보고서의 챕터 표기(audit_findings 의 chapter)만 믿으면 핵심 요약("?")
    이나 표 안 인용을 놓친다 — 챕터 파일을 grep 하는 것이 결정적이다.
    """
    hits: set[int] = set()
    draft = DRAFTS_DIR / slug
    if not draft.is_dir() or not tokens:
        return hits
    for p in sorted(draft.glob("ch??_*.md")):
        try:
            cid = int(p.name[2:4])
        except ValueError:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").replace(",", "")
        except OSError:
            continue
        if any(t in text for t in tokens):
            hits.add(cid)
    return hits


def _hallucination_guard(slug: str) -> tuple[dict[int, str], set[str]]:
    """수치 인용 감사 결과를 챕터 패치 노트로 변환하는 결정적 가드 (LLM 비용 0).

    Pro 리뷰가 환각을 지적해도 Flash 의 자유 서술 플랜이 이를 누락하면 재생성에
    반영되지 않던 공백(21차 실측: CAPEX EUR 244.5M 이 Ch4·Ch7에 잔존)을 메운다:
      - 미확인 수치를 인용한 챕터를 드래프트 grep 으로 직접 찾아
        '[환각 의심 수치 제거]' 노트를 강제 추가 → 해당 챕터가 항상 재작성된다.
        Ch7(메모 전용)이 인용해도 target 에 포함된다(자료 없이 재작성하므로 저비용).
      - 미확인 수치 토큰 집합은 Ch4 다이제스트 전처리(excluded_numbers)로 전달돼,
        재작성 전 옛 본문의 환각 수치가 Ch4에 재주입되는 것도 막는다.

    Returns ({챕터id: 노트 텍스트}, 미확인 수치 토큰 집합).
    """
    try:
        from src.validators.citation_audit import audit_findings
        _total, findings = audit_findings(slug)
    except Exception as e:
        print(f"  [guard] 수치 인용 감사 실패 (가드 생략): {type(e).__name__}: {e}")
        return {}, set()
    if not findings:
        return {}, set()

    tokens = {t for f in findings for t in f["missing"]}
    max_id = template_for_slug(slug).max_id
    by_ch: dict[int, list[str]] = {}
    for f in findings:
        chapters = _locate_chapters_by_numbers(slug, f["missing"])
        cid = _as_chapter_id(f.get("chapter"), max_id)
        if cid is not None:
            chapters.add(cid)
        for c in chapters:
            item = f'{", ".join(f["missing"])} — 문맥: "…{f["ctx"][-60:]}"'
            if item not in by_ch.setdefault(c, []):
                by_ch[c].append(item)

    notes = {
        cid: ("[환각 의심 수치 제거] 아래 수치는 수집 원문 어디에서도 확인되지 "
              "않았습니다. 본문·표·결론에서 해당 수치를 제거하거나 '데이터 없음'으로 "
              "교체하십시오(반올림·단위 환산으로 원문 수치와 일치함을 확신할 때만 유지):\n"
              + "\n".join(f"  - {it}" for it in items))
        for cid, items in by_ch.items()
    }
    if notes:
        print(f"  [guard] 환각 의심 수치 {len(tokens)}개 — "
              f"챕터 {sorted(notes)} 에 제거 노트 강제 추가")
    return notes, tokens


# ─── 메인 루프 ────────────────────────────────────────────────────────────

def _full_regen(slug: str) -> Path:
    """폴백 전체 재생성 — 개요부터 7챕터까지 모두 다시 생성한다.

    챕터 파일을 직접 지우지 않는다 — 이 함수가 불리는 시점에는 자료 구성이
    바뀌어 있어(신규/승격 문서 병합) stage1 의 지문(fingerprint) 가드가
    불일치를 감지하고 기존 챕터·메모를 보관 폴더로 옮긴 뒤 재생성한다(20차).
    """
    return generate_report(slug)


def run_validation_loop(slug: str, max_iterations: int = 1,
                        skip_review: bool = False) -> None:
    """보고서 검증·보완 루프를 실행한다. (동시 실행 가드 포함)

    Args:
        slug:           보고서 slug (output/reports/{slug}.md)
        max_iterations: 최대 루프 횟수 (기본 1)
        skip_review:    True면 기존 Pro 리뷰 파일 재사용(Pro 재호출 없음). Flash 는 항상 실행.
    """
    with slug_lock(slug):
        _validation_loop_inner(slug, max_iterations, skip_review)


def _validation_loop_inner(slug: str, max_iterations: int,
                           skip_review: bool) -> None:
    for iteration in range(1, max_iterations + 1):
        print(BAR)
        print(f"[검증 루프 {iteration}/{max_iterations}]  slug={slug}")
        print(BAR)

        # 1. Pro 리뷰 (강점·약점·데이터공백)
        review_path = REVIEW_DIR / f"{slug}-review-v{iteration}.md"
        if skip_review and review_path.exists() and review_path.stat().st_size > 0:
            print(f"  [skip-review] 기존 Pro 리뷰 재사용: {review_path.name}")
        else:
            print(f"[1/2 리뷰] {REVIEW_MODEL} — tech-writer 관점 분석")
            review_path = request_review(slug, iteration)

        # 2. Flash 쿼리 플랜 (재수집 JSON + 영향 챕터)
        print(f"[2/2 플랜] {QUERY_MODEL} — 재수집 쿼리 + 영향 챕터 플랜")
        queries_path = plan_queries(slug, review_path, iteration)
        plan = parse_plan(queries_path, max_id=template_for_slug(slug).max_id)

        # 2.5. 탈락 풀 승격 — 웹 재수집 전에 이미 수집한 자료부터 (19차)
        # 3~4. 풀로 못 메운 공백만 웹 재수집 + 병합
        added = 0
        promoted = 0
        if plan.should_recollect:
            print("[2.5/2 승격] 탈락 풀에서 공백 관련 자료 검색")
            promoted, remaining = promote_from_pool(slug, plan, iteration)
            if remaining:
                _merged, added = collect_and_merge(slug, remaining, iteration)
            elif promoted:
                print("  [promote] 모든 공백이 풀 승격으로 충족 → 웹 재수집 생략")

        # 5. 영향 챕터 노트 구성 — 데이터 공백(신규·승격 자료 반영) + 논리 약점
        chapter_notes: dict[int, list[str]] = {}
        if added + promoted > 0:
            for cid, why in plan.recollect_chapters.items():
                chapter_notes.setdefault(cid, []).append(
                    f"[데이터 공백] {why}" if why else "[데이터 공백]")
        elif plan.should_recollect:
            print("  신규·승격 문서 0건 → 데이터 공백 챕터 패치 생략")
        for cid, issue in plan.revision_chapters.items():
            chapter_notes.setdefault(cid, []).append(
                f"[논리 약점] {issue}" if issue else "[논리 약점]")

        # 5.5. 환각 수치 결정적 가드(21차 문제1) — Flash 누락과 무관하게,
        # 감사가 잡은 미확인 수치를 인용한 챕터(Ch7 포함)를 항상 재작성 대상에 넣는다.
        halluc_notes, halluc_tokens = _hallucination_guard(slug)
        for cid, note in halluc_notes.items():
            chapter_notes.setdefault(cid, []).append(note)

        if not chapter_notes:
            print("  보완 대상 챕터 없음 → 루프 종료")
            break

        # 6. 보고서 재생성
        print(BAR)
        print(f"[보고서 재생성]")
        print(BAR)
        try:
            # 신규 자료가 있는데 영향 챕터를 못 짚었으면 전체 재생성으로 폴백(자료 유실 방지).
            if added + promoted > 0 and not plan.recollect_chapters:
                print("  영향 챕터 미상 + 신규 자료 존재 → 전체 재생성 폴백")
                report_path = _full_regen(slug)
            else:
                notes = {cid: "\n".join(ns) for cid, ns in chapter_notes.items()}
                print(f"  부분 재생성 — 대상 챕터 {sorted(chapter_notes)}")
                try:
                    report_path = run_partial(
                        slug, set(chapter_notes), notes,
                        query_groups=plan.query_groups,
                        excluded_numbers=halluc_tokens)
                except FileNotFoundError:
                    print("  개요/챕터 드래프트 없음 → 전체 재생성 폴백")
                    report_path = _full_regen(slug)
            print(f"  [report] 완료: {report_path}")
        except Exception as e:
            print(f"  [report] 실패: {type(e).__name__}: {e}")
            break

    print(BAR)
    print(f"[검증 완료]  output/reports/{slug}.md")
    print(BAR)


# ─── CLI ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    argv = sys.argv[1:]
    slugs_args  = [a for a in argv if not a.startswith("--")]
    max_iter    = 1
    skip_review = "--skip-review" in argv

    for i, a in enumerate(argv):
        if a == "--max-iter" and i + 1 < len(argv):
            try:
                max_iter = int(argv[i + 1])
            except ValueError:
                pass

    if not slugs_args:
        print('사용법: python -m src.validators.report_validator <slug> [--max-iter N] [--skip-review]')
        sys.exit(1)

    slug = slugs_args[0]
    try:
        run_validation_loop(slug, max_iterations=max_iter, skip_review=skip_review)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[오류] {type(e).__name__}: {e}")
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print("[오류] agy 타임아웃 초과")
        sys.exit(3)
    except KeyboardInterrupt:
        print("\n[중단] 사용자 인터럽트")
        sys.exit(130)
