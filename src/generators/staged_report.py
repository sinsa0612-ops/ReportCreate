"""단계적 보고서 생성 — 개요 → 챕터별 작성 → 조합 3단계.

기존 report.py(1-shot)와 달리, 챕터를 하나씩 집중 생성해 품질을 높인다.

설계 (Paige + Amelia 협업):
  1단계 stage1_outline  — 수집 자료를 7개 챕터에 배분(relevant_doc_indices)하고
                          챕터별 핵심 포인트를 잡는다. → outline.json + outline.md
                          (사용자 검토 체크포인트)
  2단계 stage2_chapters — 챕터마다 배분된 자료만 프롬프트에 넣어 Opus 4.8로 작성.
                          각 챕터 완성 후 '이어쓰기 메모'를 추출해 다음 챕터에 주입
                          (논리·용어·수치·문체 일관성 유지). → ch01~07.md
                          종합 챕터(18차·20차): Ch4는 이전 챕터의 '수치 다이제스트'
                          (수치 문장·표만 추출, 본문 통째 주입 대체)를 추가 입력으로
                          받아 1~3장 수치와 일관된 비교표를 만들고, Ch7은 자료 없이
                          이전 챕터 메모만으로 시사점을 도출한다.
                          출력 검증: '## {id}.' 제목 누락 시 1회 재시도(A1).
  3단계 stage3_assemble — 완성된 챕터들을 읽고 핵심 요약을 마지막에 작성,
                          참고 자료 목록과 함께 조합. → output/reports/{slug}.md

모델 선택:
  - 개요/조합: Sonnet (구조 작업, 비용 절감)
  - 챕터 작성: Selective Opus — Ch1·5·6만 Opus 4.8, Ch2·3·4·7은 Sonnet 4.6.
    추론·종합 판단이 무거운 챕터만 Opus를 쓰고 나머지는 Sonnet으로 비용 절감(약 40%).

실행 예시 (프로젝트 루트에서):
    python -m src.generators.staged_report "<slug>" --stage outline
    python -m src.generators.staged_report "<slug>" --stage chapters
    python -m src.generators.staged_report "<slug>" --stage assemble
    python -m src.generators.staged_report "<slug>" --auto   # 전체 자동
"""
import re
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from dataclasses import asdict

from src.generators.report import (
    find_raw, _find_sources_dir, _build_fulltext_index,
    _doc_block, _find_claude, MAX_DOCS, extract_quant_sentences,
    _QUANT_PATTERN,
)
from src.collectors.library import load_library, list_library
from src.processors.select import select_for_report, count_groups
from src.processors.relevance import gap_terms, term_hits
from src.report_templates import DEFAULT_TEMPLATE, template_for_slug, ReportTemplate

DRAFTS_DIR = Path("output/drafts")
REPORT_DIR = Path("output/reports")

OPUS_MODEL = "claude-opus-4-8"
STRUCT_MODEL = "claude-sonnet-4-6"
CLAUDE_TIMEOUT = 1800

MEMO_DELIM = "<<<MEMO>>>"

# 종합(synthesis) 챕터 — 신규 자료가 아니라 이전 챕터 산출물을 입력으로 쓴다(18차).
#   prev_body_chapters(예: Ch4): 배분 자료 + '이전 챕터 수치 다이제스트'를 함께
#                   받아, 앞 장들이 인용한 수치와 일관된 비교표를 만든다.
#   memo_only_chapters(예: Ch7): 배분 자료 없이 이전 챕터 메모(누적)만으로 결론.
#
# 어떤 챕터가 종합/메모전용/Opus인지는 이제 **양식(ReportTemplate)** 이 정한다.
# 아래 상수는 하위호환용 별칭 — 기본(에너지) 양식의 값이며, 내부 로직은 slug 별로
# 해석한 양식(_resolve_template)을 사용한다. 주제별로 챕터 구성을 갈아끼우려면
# templates/<name>.json 을 두고 수집 시 --template <name> 으로 지정하면 된다.
CHAPTER_SKELETON = [dict(c) for c in DEFAULT_TEMPLATE.chapters]
CHAPTER_MODELS: dict[int, str] = DEFAULT_TEMPLATE.models(OPUS_MODEL, STRUCT_MODEL)
PREV_BODY_CHAPTERS = set(DEFAULT_TEMPLATE.prev_body_chapters)
MEMO_ONLY_CHAPTERS = set(DEFAULT_TEMPLATE.memo_only_chapters)
PREV_DIGEST_CAP = 8000   # 종합 챕터에 주입하는 이전 챕터 수치 다이제스트 합계 상한(자)


def _resolve_template(slug: str) -> ReportTemplate:
    """slug 의 raw payload 에 저장된 양식을 해석한다(없으면 기본 양식)."""
    return template_for_slug(slug)

# Paige 글쓰기 원칙 — 모든 챕터 프롬프트에 내장한다.
PAIGE_STYLE = """\
[작성 스타일 — 반드시 준수]
- Julia Evans처럼 독자의 과제 중심으로 쉽게, Edward Tufte의 데이터 정밀성으로 정확하게 쓴다.
- 표가 문장보다 더 많은 정보를 전달하는 경우 반드시 표를 쓴다.
- 모든 정량 수치(숫자·단위·비율) 뒤에는 [등급 | 출처명]을 붙인다.
  예: "효율 74.4% [A | ScienceDirect 2025]"
- 기술적 한계·리스크를 긍정 측면과 동등한 비중으로 다룬다.
- 격식체(합니다체)로 일관되게 작성한다.
- 순수 Markdown만 출력한다(코드펜스로 전체를 감싸지 않는다)."""


# ── claude 호출 ────────────────────────────────────────────────────

def _call_claude(prompt: str, model: str) -> str:
    """claude -p --model 을 호출해 stdout(문자열)을 반환한다."""
    claude = _find_claude()
    if not claude:
        raise RuntimeError(
            "claude CLI 를 PATH 에서 찾을 수 없습니다. Claude Code 설치를 확인하세요.")
    result = subprocess.run(
        [claude, "-p", "--model", model],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLAUDE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p 실패 (exit {result.returncode}): {result.stderr.strip()[:200]}")
    out = (result.stdout or "").strip()
    if not out:
        raise RuntimeError("claude -p 가 빈 출력을 반환했습니다.")
    return out


def _strip_fences(text: str) -> str:
    """```json ... ``` 코드펜스를 제거하고 내부만 반환."""
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


# ── 자료 준비 ──────────────────────────────────────────────────────

ADDED_DOCS_CAP = 12   # 검증 재수집 신규 문서의 보고서 투입 상한


def _selected_docs(slug: str):
    """raw payload 에서 보고서 투입 문서를 선별해 (selected, queries, raw_path) 반환.

    - 기본 수집분은 select_for_report 로 MAX_DOCS 건 선별(점수 + 목록형 쿼터
      + 각도 균형).
    - 검증 루프가 추가한 문서(validation_added_urls)는 기본 선별과 분리해
      **뒤에 append** 한다. 기본 선별이 재수집 후에도 동일하게 유지돼
      outline.json 의 위치 기반 자료 인덱스가 흔들리지 않고, 재수집 신규
      문서는 MAX_DOCS 절단과 무관하게 반드시 투입된다(평가 보고서 C1 수정).
    - 18차: 원문 확보 문서에 선별 가점(+6), 추가 배치는 기본 선별의 그룹 쿼터를
      이어받는다(특허가 배치마다 5건씩 들어오던 쿼터 리셋 수정).
    - 19차: 검증 루프가 탈락 풀에서 승격한 문서(promoted_urls)는 **무경쟁으로
      항상 투입**한다(맨 뒤 append, 쿼터·점수 재경쟁 없음). 경쟁은 승격 시점에
      한 번만 — 재생성 때 순위에 밀려 조용히 빠지는 일을 막는다.
      추가·승격 배치는 공백을 겨냥해 뽑힌 자료라 각도 균형(angle_min)을 끈다.
    """
    raw_path = find_raw(slug)
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    queries = payload.get("queries", [])
    docs = payload.get("documents", [])
    promoted = set(payload.get("promoted_urls") or [])
    val_added = set(payload.get("validation_added_urls")
                    or payload.get("last_added_urls") or []) - promoted
    base = [d for d in docs
            if d.get("url") not in val_added and d.get("url") not in promoted]
    added = [d for d in docs if d.get("url") in val_added]
    promo_docs = [d for d in docs if d.get("url") in promoted]

    sources_dir = _find_sources_dir(raw_path)
    ft_urls = set(_build_fulltext_index(sources_dir)) if sources_dir else set()

    selected = select_for_report(base, MAX_DOCS, fulltext_urls=ft_urls)
    if added:
        selected = selected + select_for_report(
            added, ADDED_DOCS_CAP, fulltext_urls=ft_urls,
            initial_counts=count_groups(selected), angle_min=0)
    if promo_docs:
        selected = selected + promo_docs   # 무경쟁 — payload 순서 그대로
    return selected, queries, raw_path


def _prepare_docs(slug: str):
    """slug 의 수집 자료 + 라이브러리를 통합 인덱스로 준비한다.

    반환: (all_docs, blocks, queries)
      all_docs[j]  : 통합 문서 dict (라이브러리 먼저, 이어서 선별된 수집 문서)
      blocks[j]    : 그 문서의 프롬프트용 풀 블록 (1-based 표시는 호출부에서)
      queries      : 검색 쿼리 목록
    """
    selected, queries, raw_path = _selected_docs(slug)
    sources_dir = _find_sources_dir(raw_path)
    fulltext_index = _build_fulltext_index(sources_dir) if sources_dir else {}

    lib_docs = [asdict(d) for d in load_library(queries=queries)]

    all_docs: list[dict] = []
    blocks: list[str] = []
    for d in lib_docs:
        all_docs.append(d)
        blocks.append(_doc_block(d, {}))             # 라이브러리: 원문 매핑 없음
    for d in selected:
        all_docs.append(d)
        blocks.append(_doc_block(d, fulltext_index))  # 수집: URL 키 원문 매핑

    return all_docs, blocks, queries


def _doc_listing(all_docs: list[dict], blocks: list[str] | None = None) -> str:
    """개요 생성용 경량 목록 — 인덱스·등급·출처·제목·짧은 스니펫.

    blocks(원문 추출 블록)가 주어지면 각 문서의 정량 수치 문장 1~2개를
    '수치:' 미리보기로 덧붙인다 — 개요 작성자가 LCOE·CAGR 등 수치 보유
    자료를 정확한 챕터(2·4장 등)에 배분하도록 돕는다(18차 A5).
    """
    lines = []
    for i, d in enumerate(all_docs, 1):
        grade = d.get("trust_grade") or "-"
        src = d.get("source", "?")
        title = d.get("title", "(제목 없음)")
        snippet = (d.get("content") or "").strip().replace("\n", " ")[:140]
        line = f"[{i}] ({grade}|{src}) {title}\n     {snippet}"
        if blocks and i <= len(blocks):
            quant = extract_quant_sentences(blocks[i - 1])
            if quant:
                line += f"\n     수치: {quant}"
        lines.append(line)
    return "\n".join(lines)


def _drafts_dir(slug: str) -> Path:
    d = DRAFTS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 1단계: 개요 ────────────────────────────────────────────────────

def _doc_fingerprint(all_docs: list[dict], n: int | None = None) -> str:
    """자료 목록 URL 지문 — 개요의 '위치 기반 인덱스'가 유효한지 검사하는 근거.

    검증 루프는 자료를 목록 '뒤에만' 추가하므로, 개요 생성 시점의 앞 n건이
    그대로면(지문 일치) 인덱스는 여전히 유효하다. 라이브러리 파일 추가·삭제
    같은 '앞쪽' 변경은 지문 불일치로 드러난다 — 엉뚱한 자료가 엉뚱한 챕터에
    조용히 배분되는 사고를 막는다.
    """
    urls = [d.get("url") or "" for d in all_docs]
    if n is not None:
        urls = urls[:n]
    return hashlib.sha1("\n".join(urls).encode("utf-8")).hexdigest()[:12]


def _check_outline_alignment(outline: dict, all_docs: list[dict]) -> None:
    """개요의 자료 인덱스가 현재 자료 목록과 정합한지 검사. 불일치면 중단."""
    oc, ofp = outline.get("doc_count"), outline.get("doc_fingerprint")
    if not oc or not ofp:
        return  # 레거시 개요(지문 없음) — 검사 불가, 기존 동작 유지
    if len(all_docs) < oc or _doc_fingerprint(all_docs, oc) != ofp:
        raise RuntimeError(
            "자료 목록이 개요 생성 시점과 다릅니다 (라이브러리 파일 변경 또는 "
            "수집 데이터 변경). stage1(outline)을 다시 실행해 개요를 재생성하세요.")


def _archive_stale_drafts(draft: Path) -> None:
    """자료 구성이 바뀌어 무효가 된 챕터 드래프트·메모를 백업 폴더로 옮긴다.

    삭제하지 않고 보관한다 — 잘못된 판단이어도 사용자가 복구할 수 있도록.
    """
    stale = sorted(draft.glob("ch??_*.md")) + sorted(draft.glob("ch??_memo.txt"))
    if not stale:
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = draft / f"_stale_{stamp}"
    bak.mkdir(exist_ok=True)
    for p in stale:
        p.rename(bak / p.name)
    print(f"  [stage1] 자료 구성 변경 — 기존 챕터·메모 {len(stale)}개 파일을 "
          f"{bak.name}/ 으로 보관 (새 개요 기준으로 재작성됩니다)")


OUTLINE_PROMPT = """\
당신은 R&D 기술 분석 보고서의 편집 책임자입니다.
아래 [수집 자료 목록]을 읽고, {n_chapters}개 챕터 각각에 어떤 자료가 관련되는지 배분하고
챕터별 핵심 포인트를 잡으십시오.

[출력 형식 — 순수 JSON만 출력. 코드펜스·설명 금지]
{{
  "chapters": [
    {{
      "id": 1,
      "key_points": ["이 챕터에서 다룰 핵심 주장 2~4개"],
      "relevant_doc_indices": [관련 자료 번호들]
    }}
    // ... 마지막 챕터까지 ([챕터 골격]에 있는 모든 id)
  ]
}}

[배분 규칙]
- 자료 하나는 가장 관련성 높은 챕터 1~2개에만 배분하십시오. 여러 챕터에 두루 걸치는
  자료라도, 그 자료가 가장 핵심적으로 쓰일 챕터를 우선하여 선택하십시오.
- 같은 자료를 3개 이상의 챕터에 중복 배분하지 마십시오.
{extra_rules}

[챕터 골격]
{skeleton}

[수집 자료 목록 {count}건]
{listing}
"""


def stage1_outline(slug: str) -> Path:
    """개요(outline.json + outline.md)를 생성하고 outline.json 경로를 반환.

    - 자료 구성(지문·건수)이 기존 개요와 같으면 LLM 호출 없이 재사용한다 —
      중단 후 재실행(resume) 시 토큰 절약 + 이미 작성된 챕터 보존.
    - 구성이 달라졌으면 기존 챕터 드래프트를 보관 폴더로 옮기고 새로 만든다 —
      '옛 개요로 만든 챕터 + 새 개요'가 섞여 조립되는 사고 방지.
    - JSON 파싱 실패는 1회 재시도, 자료 인덱스는 범위 검증 후 저장한다.
    """
    tpl = _resolve_template(slug)
    all_docs, blocks, queries = _prepare_docs(slug)
    draft = _drafts_dir(slug)
    json_path = draft / "outline.json"

    if json_path.exists():
        try:
            old = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = {}
        oc = old.get("doc_count")
        if (oc and old.get("doc_fingerprint")
                and len(all_docs) == oc
                and _doc_fingerprint(all_docs) == old["doc_fingerprint"]):
            print("  [stage1] 자료 구성 동일 — 기존 개요 재사용 (LLM 호출 생략)")
            return json_path
        _archive_stale_drafts(draft)

    print(f"  [stage1] 양식: {tpl.label} ({tpl.n_chapters}챕터)")
    prompt = OUTLINE_PROMPT.format(
        n_chapters=tpl.n_chapters,
        extra_rules=tpl.outline_extra_rules,
        skeleton=tpl.skeleton_text(),
        count=len(all_docs),
        listing=_doc_listing(all_docs, blocks),
    )
    parsed = None
    for attempt in (1, 2):
        raw = _call_claude(prompt, STRUCT_MODEL)
        try:
            parsed = json.loads(_strip_fences(raw))
            break
        except json.JSONDecodeError as e:
            print(f"  [경고] 개요 JSON 파싱 실패 (시도 {attempt}/2): {e}")
    if parsed is None:
        raise RuntimeError(
            "개요 JSON 파싱에 2회 실패했습니다 — stage1(outline)을 다시 실행하세요.")

    # 골격 + claude 배분 병합 (자료 인덱스는 1..N 범위 검증, 중복 제거)
    assign = {c.get("id"): c for c in parsed.get("chapters", [])}
    missing_ids = [sk["id"] for sk in tpl.chapters if sk["id"] not in assign]
    if missing_ids:
        print(f"  [경고] 개요 응답에 챕터 {missing_ids} 배분이 없습니다 — "
              f"해당 챕터는 stage2 에서 신뢰도 상위 자료로 폴백됩니다.")
    chapters = []
    for sk in tpl.chapters:
        a = assign.get(sk["id"], {})
        idxs: list[int] = []
        for v in a.get("relevant_doc_indices", []) or []:
            try:
                j = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= j <= len(all_docs) and j not in idxs:
                idxs.append(j)
        chapters.append({
            **sk,
            "key_points": a.get("key_points", []),
            "relevant_doc_indices": idxs,
        })

    outline = {"slug": slug, "queries": queries, "chapters": chapters,
               "doc_count": len(all_docs),
               "doc_fingerprint": _doc_fingerprint(all_docs)}
    json_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    # 사람이 검토하는 md 버전
    md_lines = [f"# 보고서 개요: {slug}\n",
                f"검색 주제: {' / '.join(queries)}\n"]
    for c in chapters:
        md_lines.append(f"## {c['id']}. {c['title']}")
        md_lines.append(f"- 핵심 질문: {c['key_question']}")
        md_lines.append(f"- 핵심 포인트:")
        for kp in c["key_points"]:
            md_lines.append(f"  - {kp}")
        md_lines.append(f"- 관련 자료: {c['relevant_doc_indices']}\n")
    (draft / "outline.md").write_text("\n".join(md_lines), encoding="utf-8")

    print(f"  [stage1] 개요 생성 완료 → {json_path}")
    print(f"           검토용: {draft / 'outline.md'}")
    return json_path


# ── 2단계: 챕터별 작성 ─────────────────────────────────────────────

CHAPTER_PROMPT = """\
당신은 R&D 기술 분석 전문가입니다. 아래 자료를 바탕으로 보고서의 한 챕터를 작성하십시오.

{style}

[이 챕터]
## {id}. {title}
- 독자의 핵심 질문: {key_question}
- 반드시 포함: {must_include}
- 잡아둔 핵심 포인트:
{key_points}

{memo_section}
{patch_note}
[작성 지침]
- '## {id}. {title}' 제목으로 시작하는 이 챕터 본문만 작성한다(다른 챕터 금지).
- 아래 배분된 자료에서 근거를 끌어온다. 자료에 없는 수치는 지어내지 않는다.
- 수치를 찾지 못하면 '데이터 없음'으로 표기한다.
- '데이터 없음'으로 표기한 지표를 결론의 근거로 사용하지 않는다. 그 지표의 영향은
  "정량화 시 검증 필요" 수준의 가능성 언급으로만 한정한다.
- 제목·서지정보만 있는 특허·통계 자료(본문 없음)는 개별 기술의 성능·TRL 판단
  근거로 쓰지 않는다. 출원·통계 '동향 집계'(건수·연도·출원 주체 분포)로만 언급한다.
- [인용 교체 규칙] 같은 주제에 여러 자료가 있을 때: 등급(SS>S>AA>A>B>C)과 발행연도
  두 차원이 **모두** 신규 자료가 우월할 때만 기존 인용을 교체한다. 한 차원이라도 기존이
  우위면 기존 인용을 유지하고 신규 정보를 별도 항목으로 추가(보충)한다.

{prev_bodies}[배분된 자료]
{documents}

[출력 형식]
먼저 챕터 본문(Markdown)을 작성하고, 마지막 줄에 정확히 '{delim}' 를 쓴 뒤
다음 챕터 작성자를 위한 이어쓰기 메모를 아래 양식으로 작성하시오(메모 전체 700자 이내):
핵심 주장: (이 챕터가 확정한 사실 1~3개)
정의된 용어: (이 챕터에서 처음 정의한 용어)
인용 수치: (이 챕터가 인용한 핵심 수치 — 다음 챕터에서 일관되게 쓰도록)
미해결 실마리: (다음 챕터로 넘기는 떡밥)
"""


def _format_memos(memos: list[str]) -> str:
    """누적된 이전 챕터 메모를 프롬프트용 블록으로."""
    if not memos:
        return "[이전 챕터 메모] (없음 — 첫 챕터입니다)"
    return "[이전 챕터들의 이어쓰기 메모 — 논리·용어·수치를 일관되게 유지할 것]\n" + \
        "\n---\n".join(memos)


def _memo_path(draft: Path, chapter_id: int) -> Path:
    """챕터 이어쓰기 메모 파일 경로(ch{n}_memo.txt)."""
    return draft / f"ch{chapter_id:02d}_memo.txt"


def _load_memo(draft: Path, chapter_id: int) -> str:
    """디스크에 저장된 챕터 메모를 읽는다(없으면 빈 문자열)."""
    p = _memo_path(draft, chapter_id)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _chapter_file(draft: Path, chapter_id: int) -> Path | None:
    """이미 작성된 챕터 본문 파일(ch{n}_*.md). 없으면 None."""
    matches = sorted(draft.glob(f"ch{chapter_id:02d}_*.md"))
    return matches[0] if matches else None


def _patch_note_block(note: str) -> str:
    """재작성(patch) 시 프롬프트에 주입할 개정 지침 블록."""
    return ("[개정 지침 — 이전 검토 피드백]\n"
            "아래에 지적된 데이터 공백·논리 약점을 이번 재작성에서 반드시 해소하십시오.\n"
            f"{note}")


def _valid_chapter(body: str, chapter_id: int) -> bool:
    """챕터 본문이 '## {id}.' 제목으로 시작하는지 검사.

    claude -p 출력 앞부분이 소실된 채 저장되는 사고(2026-06-10 수소 보고서
    Ch4 — 문장 중간부터 시작, 최종 보고서 목차에서 4장 누락)를 저장 전에 잡는다.
    """
    return bool(re.match(rf"\s*##\s*{chapter_id}\.", body))


def _quant_digest(body: str, cap: int,
                  excluded_numbers: set[str] | None = None) -> str:
    """챕터 본문에서 제목·표 행·수치 문장만 추출한 다이제스트 (LLM 비용 0).

    Ch4(정량 비교)가 필요한 것은 이전 챕터의 '수치와 [등급 | 출처] 표기'이지
    산문 전체가 아니다 — 본문 통째 주입(구 PREV_BODY_CAP 40,000자) 대비
    토큰을 ~80% 줄이면서 수치 일관성 정보는 그대로 전달한다.

    excluded_numbers: 수치 인용 감사가 '원문 미확인(환각 의심)'으로 판정한
    숫자 토큰(쉼표 제거 정규화). 이 수치를 담은 문장·표 행은 다이제스트에서
    제외한다 — 재작성 전 옛 본문의 환각 수치가 Ch4에 재주입되는 경로 차단
    (21차 문제1 원인②).
    """
    if cap <= 0:
        return ""
    excluded = excluded_numbers or set()

    def _tainted(s: str) -> bool:
        if not excluded:
            return False
        norm = s.replace(",", "")
        return any(tok in norm for tok in excluded)

    parts: list[str] = []
    total = 0
    for line in body.splitlines():
        st = line.strip()
        if not st:
            continue
        if st.startswith("#") or st.startswith("|"):
            picked = [st]      # 제목(맥락)·표 행(수치의 원천)은 그대로 보존
        else:
            picked = [s for s in re.split(r"(?<=[.!?。])\s+", st)
                      if _QUANT_PATTERN.search(s)]
        picked = [s for s in picked if not _tainted(s)]
        for s in picked:
            if total + len(s) > cap:
                return "\n".join(parts)
            parts.append(s)
            total += len(s) + 1
    return "\n".join(parts)


def _prev_bodies_block(draft: Path, before_id: int,
                       excluded_numbers: set[str] | None = None) -> str:
    """종합 챕터(Ch4)용 — 앞 챕터들의 '수치 다이제스트'를 프롬프트 블록으로 묶는다.

    예산(PREV_DIGEST_CAP)은 최근(번호 큰) 챕터부터 배정한다 — 비교표와 가장
    관련 깊은 것은 직전 챕터(기술 경로·시장)이기 때문. 출력은 챕터 순서대로.
    excluded_numbers 는 _quant_digest 의 환각 수치 제외 전처리로 전달된다.
    """
    digests: dict[int, str] = {}
    total = 0
    for cid in range(before_id - 1, 0, -1):
        f = _chapter_file(draft, cid)
        if not f:
            continue
        digest = _quant_digest(f.read_text(encoding="utf-8").strip(),
                               PREV_DIGEST_CAP - total,
                               excluded_numbers=excluded_numbers)
        if not digest:
            continue
        digests[cid] = digest
        total += len(digest)
        if total >= PREV_DIGEST_CAP:
            break
    if not digests:
        return ""
    joined = "\n\n".join(digests[cid] for cid in sorted(digests))
    return ("[이전 챕터 수치 다이제스트 — 종합 대상]\n"
            f"아래는 챕터 1~{before_id - 1} 본문에서 추출한 수치 문장·표·제목입니다.\n"
            "이 챕터의 비교·종합은 아래 수치([등급 | 출처] 표기 포함)와 일관되게\n"
            "작성하십시오. 아래 수치를 우선 재사용하고, 배분된 자료의 수치는 보충으로\n"
            "사용하십시오.\n"
            f"{joined}\n\n")


def _write_one_chapter(draft: Path, c: dict, sel_blocks: list[str],
                       memos: list[str], model: str,
                       patch_note: str = "",
                       prev_bodies: str = "") -> tuple[Path, str]:
    """한 챕터를 작성해 본문 .md 와 메모 .txt 를 저장하고 (경로, 메모)를 반환한다.

    출력 검증: 본문이 '## {id}.' 제목으로 시작하지 않으면 1회 재시도하고,
    그래도 실패하면 제목을 보정해 저장하되 경고를 남긴다(18차 A1).
    """
    kp = "\n".join(f"  - {p}" for p in c.get("key_points", [])) or "  - (없음)"
    prompt = CHAPTER_PROMPT.format(
        style=PAIGE_STYLE,
        id=c["id"], title=c["title"],
        key_question=c["key_question"],
        must_include=c["must_include"],
        key_points=kp,
        memo_section=_format_memos(memos),
        patch_note=patch_note,
        prev_bodies=prev_bodies,
        documents="\n\n".join(sel_blocks)
        or "(배분된 자료 없음 — 위의 이전 챕터 메모를 종합해 작성하십시오)",
        delim=MEMO_DELIM,
    )
    body, memo = "", ""
    for attempt in (1, 2):
        raw = _call_claude(prompt, model)
        body, memo = _split_chapter(raw)
        if _valid_chapter(body, c["id"]):
            break
        print(f"  [경고] 챕터 {c['id']} 출력이 '## {c['id']}.' 제목으로 시작하지 "
              f"않습니다 (시도 {attempt}/2)")
    else:
        body = f"## {c['id']}. {c['title']}\n\n{body}"
        print(f"  [경고] 챕터 {c['id']} 재시도에도 제목이 없어 보정해 저장합니다 — "
              f"본문 앞부분 소실 여부를 확인하십시오.")

    fname = f"ch{c['id']:02d}_{c['title'].replace(' ', '')}.md"
    path = draft / fname
    path.write_text(body, encoding="utf-8")
    _memo_path(draft, c["id"]).write_text(memo, encoding="utf-8")
    return path, memo


def _split_chapter(raw: str) -> tuple[str, str]:
    """claude 출력에서 (본문, 메모) 분리. 메모 구분자 없으면 메모는 빈 문자열."""
    if MEMO_DELIM in raw:
        body, memo = raw.split(MEMO_DELIM, 1)
        return body.strip(), memo.strip()
    return raw.strip(), ""


def stage2_chapters(slug: str) -> list[Path]:
    """outline.json 을 읽어 챕터를 하나씩 Opus 로 작성. 챕터 파일 경로 목록 반환."""
    tpl = _resolve_template(slug)
    models = tpl.models(OPUS_MODEL, STRUCT_MODEL)
    draft = _drafts_dir(slug)
    outline_path = draft / "outline.json"
    if not outline_path.exists():
        raise FileNotFoundError(
            f"개요가 없습니다. 먼저 stage1(outline)을 실행하세요: {outline_path}")
    outline = json.loads(outline_path.read_text(encoding="utf-8"))

    all_docs, blocks, _q = _prepare_docs(slug)
    _check_outline_alignment(outline, all_docs)

    written: list[Path] = []
    memos: list[str] = []
    for c in outline["chapters"]:
        cid = c["id"]
        idxs = list(c.get("relevant_doc_indices") or [])
        if cid in tpl.memo_only_chapters:
            idxs = []   # 종합 챕터: 자료 없이 이전 챕터 메모만으로 작성
        elif not idxs:
            # 배분 자료가 없으면 전체에서 신뢰도 상위 일부로 폴백
            idxs = list(range(1, min(len(all_docs), 8) + 1))
        sel_blocks = [blocks[j - 1] for j in idxs if 1 <= j <= len(blocks)]

        fname = f"ch{cid:02d}_{c['title'].replace(' ', '')}.md"
        path = draft / fname

        # 이미 작성된 챕터는 스킵 (재실행 시 기존 파일 보존).
        # 메모는 디스크에서 복원해 누적 — 다음 챕터가 맥락을 잃지 않도록.
        if path.exists():
            print(f"  [stage2] 챕터 {cid} '{c['title']}' — 이미 존재, 스킵")
            written.append(path)
            memo = _load_memo(draft, cid)
            if memo:
                memos.append(f"[챕터 {cid} {c['title']}]\n{memo}")
            continue

        prev_bodies = (_prev_bodies_block(draft, cid)
                       if cid in tpl.prev_body_chapters else "")
        model = models.get(cid, OPUS_MODEL)
        model_label = "Opus" if model == OPUS_MODEL else "Sonnet"
        print(f"  [stage2] 챕터 {cid} '{c['title']}' 작성 중 "
              f"(자료 {len(sel_blocks)}건, {model_label})…")
        path, memo = _write_one_chapter(draft, c, sel_blocks, memos, model,
                                        prev_bodies=prev_bodies)
        written.append(path)
        if memo:
            memos.append(f"[챕터 {cid} {c['title']}]\n{memo}")

    print(f"  [stage2] 챕터 {len(written)}건 작성 완료 → {draft}")
    return written


# ── 2.5단계: 영향 챕터만 선별 재작성 (검증 루프용) ──────────────────

PATCH_INJECT_CAP = 8   # 패치 재작성 시 챕터 1개에 주입하는 신규 자료 상한(21차 문제2)


def _added_doc_indices(slug: str, all_docs: list[dict]) -> list[int]:
    """이번 검증 회차에 추가·승격된 문서의 1-based 전역 인덱스.

    collect_and_merge 가 기록한 last_added_urls(웹 재수집 신규)와
    promote_from_pool 이 기록한 last_promoted_urls(탈락 풀 승격, 19차)를 읽어,
    _prepare_docs 가 만든 통합 인덱스에서 해당 문서들의 위치를 찾는다.
    이 인덱스를 재작성 대상 챕터의 자료 목록에 주입해 신규 자료가 반드시 반영되게 한다.
    """
    try:
        payload = json.loads(find_raw(slug).read_text(encoding="utf-8"))
    except Exception:
        return []
    added = (set(payload.get("last_added_urls") or [])
             | set(payload.get("last_promoted_urls") or []))
    if not added:
        return []
    return [i for i, d in enumerate(all_docs, 1) if d.get("url") in added]


def _added_doc_chapter_map(all_docs: list[dict], extra_idxs: list[int],
                           query_groups: list[dict], target_ids: set[int],
                           memo_only: set[int] | None = None,
                           ) -> tuple[dict[int, list[int]], list[int]]:
    """신규·승격 문서를 쿼리 그룹별로 분류해 {챕터id: [전역 인덱스]}로 매핑한다.

    21차 문제2: 공백 챕터가 여러 개일 때 신규 자료 전부를 primary 챕터 1곳에
    몰아넣으면(16차 G) 자료가 오배분된다(실측: 32건 전부 Ch3, TRL 자료가 Ch1에
    미전달). 각 신규 문서를 판별 용어(gap_terms) 일치가 가장 높은 쿼리 그룹에
    배정하고, 그 그룹의 affected_chapters(∩ target_ids, Ch7 제외)에 라운드로빈
    분산한다. 챕터당 PATCH_INJECT_CAP 상한.

    Returns (mapping, 그룹에 배정 못 한 문서 인덱스 — 호출부가 폴백 처리).
    """
    memo_only = memo_only if memo_only is not None else MEMO_ONLY_CHAPTERS
    groups: list[tuple[set[str], list[int]]] = []
    for g in query_groups or []:
        terms = gap_terms(g.get("queries") or [])
        chs = [cid for cid in (g.get("affected_chapters") or [])
               if cid in target_ids and cid not in memo_only]
        if terms and chs:
            groups.append((terms, chs))
    if not groups:
        return {}, list(extra_idxs)

    mapping: dict[int, list[int]] = {}
    leftover: list[int] = []
    rr: dict[int, int] = {}   # 그룹별 라운드로빈 커서
    for j in extra_idxs:
        d = all_docs[j - 1]
        best_gi, best_hits = None, 0
        for gi, (terms, _chs) in enumerate(groups):
            h = term_hits(d, terms)
            if h > best_hits:
                best_gi, best_hits = gi, h
        if best_gi is None:
            leftover.append(j)
            continue
        chs = groups[best_gi][1]
        placed = False
        for k in range(len(chs)):
            cid = chs[(rr.get(best_gi, 0) + k) % len(chs)]
            if len(mapping.get(cid, [])) < PATCH_INJECT_CAP:
                mapping.setdefault(cid, []).append(j)
                rr[best_gi] = (rr.get(best_gi, 0) + k + 1) % len(chs)
                placed = True
                break
        if not placed:
            leftover.append(j)
    return mapping, leftover


def stage2_patch(slug: str, target_ids, notes: dict | None = None,
                 query_groups: list[dict] | None = None,
                 excluded_numbers: set[str] | None = None) -> list[Path]:
    """outline.json 기준으로 target_ids 챕터만 재작성하고 나머지는 보존한다.

    - target_ids: 재작성할 챕터 id 집합(데이터 공백 + 논리 약점 영향 챕터)
    - notes: {챕터id: 개정 사유 텍스트} — 프롬프트의 [개정 지침]으로 주입
    - query_groups: Flash 플랜의 공백 그룹 원본(queries·affected_chapters) —
      재수집 신규 문서를 그룹 판별 용어로 분류해 해당 그룹의 영향 챕터에
      분산 주입한다(21차 문제2). 없으면(레거시) 기존 primary 단일챕터 주입으로
      폴백하되, 챕터당 PATCH_INJECT_CAP 상한·초과분 차순위 챕터 분산을 적용.
    - excluded_numbers: 환각 의심 수치 토큰 — Ch4 다이제스트 전처리로 전달(21차 문제1).
    - 메모는 챕터 순서대로 누적: 재작성 챕터는 새 메모, 보존 챕터는 디스크 메모를 사용해
      뒤 챕터가 일관된 맥락을 받도록 한다.
    """
    notes = notes or {}
    target_ids = set(target_ids)
    tpl = _resolve_template(slug)
    models = tpl.models(OPUS_MODEL, STRUCT_MODEL)
    draft = _drafts_dir(slug)
    outline_path = draft / "outline.json"
    if not outline_path.exists():
        raise FileNotFoundError(
            f"개요가 없습니다. 먼저 stage1(outline)을 실행하세요: {outline_path}")
    outline = json.loads(outline_path.read_text(encoding="utf-8"))

    all_docs, blocks, _q = _prepare_docs(slug)
    _check_outline_alignment(outline, all_docs)
    extra_idxs = _added_doc_indices(slug, all_docs)

    # 신규 자료 → 챕터 주입 계획: 그룹 분류 우선, 미배정분은 primary 폴백.
    inject: dict[int, list[int]] = {}
    if extra_idxs:
        inject, leftover = _added_doc_chapter_map(
            all_docs, extra_idxs, query_groups or [], target_ids,
            memo_only=tpl.memo_only_chapters)
        if leftover:
            # 폴백: 기존 관련자료가 많은 대상 챕터 순으로 상한까지 채워 분산
            cands = sorted(
                (c for c in outline["chapters"]
                 if c["id"] in target_ids and c["id"] not in tpl.memo_only_chapters),
                key=lambda c: -len(c.get("relevant_doc_indices") or []))
            for c in cands:
                room = PATCH_INJECT_CAP - len(inject.get(c["id"], []))
                if room <= 0:
                    continue
                take, leftover = leftover[:room], leftover[room:]
                if take:
                    inject.setdefault(c["id"], []).extend(take)
                if not leftover:
                    break
            if leftover:
                print(f"  [stage2-patch] 챕터당 주입 상한({PATCH_INJECT_CAP}) 초과 — "
                      f"신규 자료 {len(leftover)}건 미주입 (참고자료에는 포함)")
        plan_txt = ", ".join(f"Ch{cid}:{len(v)}건"
                             for cid, v in sorted(inject.items()))
        print(f"  [stage2-patch] 신규 자료 {len(extra_idxs)}건 주입 계획 — "
              f"{plan_txt or '(없음)'}")

    written: list[Path] = []
    memos: list[str] = []
    patched: list[int] = []
    for c in outline["chapters"]:
        cid = c["id"]
        if cid in target_ids:
            idxs = list(c.get("relevant_doc_indices") or [])
            for j in inject.get(cid, []):
                if j not in idxs:
                    idxs.append(j)
            if cid in tpl.memo_only_chapters:
                idxs = []   # 종합 챕터: 메모만으로 재작성
            elif not idxs:
                idxs = list(range(1, min(len(all_docs), 8) + 1))
            sel_blocks = [blocks[j - 1] for j in idxs if 1 <= j <= len(blocks)]
            prev_bodies = (_prev_bodies_block(draft, cid,
                                              excluded_numbers=excluded_numbers)
                           if cid in tpl.prev_body_chapters else "")

            model = models.get(cid, OPUS_MODEL)
            model_label = "Opus" if model == OPUS_MODEL else "Sonnet"
            note = notes.get(cid, "")
            patch_note = _patch_note_block(note) if note else ""
            print(f"  [stage2-patch] 챕터 {cid} '{c['title']}' 재작성 "
                  f"(자료 {len(sel_blocks)}건, {model_label})…")
            path, memo = _write_one_chapter(
                draft, c, sel_blocks, memos, model, patch_note,
                prev_bodies=prev_bodies)
            written.append(path)
            patched.append(cid)
            if memo:
                memos.append(f"[챕터 {cid} {c['title']}]\n{memo}")
        else:
            existing = _chapter_file(draft, cid)
            if existing:
                written.append(existing)
            memo = _load_memo(draft, cid)
            if memo:
                memos.append(f"[챕터 {cid} {c['title']}]\n{memo}")
            print(f"  [stage2-patch] 챕터 {cid} '{c['title']}' 유지")

    print(f"  [stage2-patch] 재작성 {len(patched)}개 챕터: {patched}")
    return written


def run_partial(slug: str, target_ids, notes: dict | None = None,
                query_groups: list[dict] | None = None,
                excluded_numbers: set[str] | None = None) -> Path:
    """영향 챕터만 재작성 후 보고서를 재조합한다(검증 루프 부분 재생성 모드)."""
    stage2_patch(slug, target_ids, notes,
                 query_groups=query_groups, excluded_numbers=excluded_numbers)
    return stage3_assemble(slug)


# ── 3단계: 조합 + 핵심 요약 ────────────────────────────────────────

SUMMARY_PROMPT = """\
아래 내용을 바탕으로, 완성된 기술 보고서의 '핵심 요약'을 작성하십시오.
실제로 등장한 사실과 수치만으로 작성하고, 새로운 내용을 만들지 마십시오.

[출력 형식 — 순수 Markdown]
## 핵심 요약
(한 줄 결론을 맨 앞에 쓴 뒤, 핵심 수치 3개 이상을 bullet로)

[보고서 내용]
{body}
"""


def _build_references(all_docs: list[dict]) -> str:
    """수집 자료를 S→A→B→C 순으로 정리한 참고 자료 섹션."""
    order = {"SS": 0, "S": 1, "AA": 2, "A": 3, "B": 4, "C": 5}
    ranked = sorted(all_docs, key=lambda d: order.get(d.get("trust_grade") or "C", 6))
    lines = ["## 참고 자료\n"]
    for d in ranked:
        g = d.get("trust_grade") or "-"
        title = d.get("title", "(제목 없음)")
        url = d.get("url", "")
        lines.append(f"- [{g}] {title} — {url}")
    return "\n".join(lines)


def stage3_assemble(slug: str) -> Path:
    """챕터 파일들을 읽어 핵심 요약을 마지막에 작성하고 조합한 최종 보고서를 저장.

    핵심 요약 입력은 각 챕터의 이어쓰기 메모(누적, 챕터당 최대 700자)를 우선 사용한다.
    메모에는 이미 핵심주장·인용수치가 압축되어 있어 본문 전체(최대 60,000자)를
    다시 보내는 것보다 토큰을 크게 절감한다. 메모가 없으면(레거시/테스트) 기존처럼
    본문 앞부분으로 폴백한다.
    """
    draft = _drafts_dir(slug)
    chapter_files = sorted(draft.glob("ch??_*.md"))
    if not chapter_files:
        raise FileNotFoundError(
            f"챕터 파일이 없습니다. 먼저 stage2(chapters)를 실행하세요: {draft}")

    # 같은 챕터 번호의 파일이 2개 이상이면(스켈레톤 제목 변경 등으로 파일명이
    # 달라진 잔재) 같은 챕터가 보고서에 두 번 들어간다 — 조립 전에 차단.
    by_cid: dict[str, list[str]] = {}
    for p in chapter_files:
        by_cid.setdefault(p.name[2:4], []).append(p.name)
    dups = {k: v for k, v in by_cid.items() if len(v) > 1}
    if dups:
        raise RuntimeError(
            f"같은 챕터 번호의 드래프트가 여러 개입니다: {dups} — "
            f"오래된 파일을 정리한 뒤 다시 실행하세요: {draft}")

    bodies = [p.read_text(encoding="utf-8").strip() for p in chapter_files]
    full_body = "\n\n".join(bodies)

    tpl = _resolve_template(slug)
    memo_blocks = []
    for c in tpl.chapters:
        memo = _load_memo(draft, c["id"])
        if memo:
            memo_blocks.append(f"[챕터 {c['id']} {c['title']}]\n{memo}")
    summary_input = "\n---\n".join(memo_blocks) if memo_blocks else full_body[:60000]

    summary = _call_claude(SUMMARY_PROMPT.format(body=summary_input), STRUCT_MODEL)

    # 참고문헌은 제목·URL·등급만 필요 — 라이브러리 PDF 파싱/캐시 로드 없이
    # 경량 목록(list_library) + 선별된 수집 문서 메타데이터로 구성한다.
    selected, _queries, _raw = _selected_docs(slug)
    ref_docs = [asdict(d) for d in list_library()] + selected
    references = _build_references(ref_docs)

    report = "\n\n".join([summary.strip(), full_body, references])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"{slug}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"  [stage3] 최종 보고서 조합 완료 → {out_path}")
    return out_path


# ── CLI ────────────────────────────────────────────────────────────

def run_all(slug: str) -> Path:
    stage1_outline(slug)
    stage2_chapters(slug)
    return stage3_assemble(slug)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print('사용법: python -m src.generators.staged_report "<slug>" '
              '[--stage outline|chapters|assemble | --auto]')
        sys.exit(1)
    slug = args[0]

    stage = None
    for i, a in enumerate(sys.argv):
        if a == "--stage" and i + 1 < len(sys.argv):
            stage = sys.argv[i + 1]
    auto = "--auto" in sys.argv

    try:
        if auto:
            run_all(slug)
        elif stage == "outline":
            stage1_outline(slug)
        elif stage == "chapters":
            stage2_chapters(slug)
        elif stage == "assemble":
            stage3_assemble(slug)
        else:
            print("‼ --stage outline|chapters|assemble 또는 --auto 를 지정하세요.")
            sys.exit(1)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[오류] {type(e).__name__}: {e}")
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print(f"[오류] 제한 시간({CLAUDE_TIMEOUT}s) 초과")
        sys.exit(3)
