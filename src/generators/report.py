"""보고서 자동 생성 — 수집 JSON(output/raw) + 원문(_sources)을 Claude CLI(`claude -p`)에 넘겨
에너지 R&D 심층 보고서 초안(Markdown)을 만든다.

설계 메모:
  - claude -p 는 현재 Claude Code 세션 인증을 재사용하므로 ANTHROPIC_API_KEY 불필요.
  - _sources/ 폴더의 원문 전체본을 우선 사용. 수치 포함 문장을 우선 추출해 토큰을 절약한다.
  - 스니펫만 있는 경우(원문 미확보)는 기존 방식으로 폴백.
  - 프롬프트는 stdin 으로 주입하고 stdout(Markdown)을 받아 파일로 저장한다.

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.generators.report 연료전지
    .venv\\Scripts\\python.exe -m src.generators.report 연료전지 --dry-run
"""
import re
import sys
import json
import shutil
import subprocess
from pathlib import Path

RAW_DIR = Path("output/raw")
REPORT_DIR = Path("output/reports")

CLAUDE_BIN = "claude"
CLAUDE_TIMEOUT = 1800         # 심층 보고서는 더 오래 걸릴 수 있다.
FULLTEXT_CHARS = 2500         # 원문 확보된 문서당 추출 길이 (18차: 4000→2500, 수치 단락 우선 추출 전제)
LIBRARY_CHARS = 8000          # 마스터 라이브러리 문서 추출 길이 (고품질 원본이므로 넓게)
SNIPPET_CHARS = 1000          # 원문 미확보 시 스니펫 길이
MAX_DOCS = 40                 # 수집 문서 최대 수(신뢰도 상위부터, 라이브러리 제외)

# 수치 포함 문장 추출용 패턴 (정량 데이터가 있는 문장 우선)
_QUANT_PATTERN = re.compile(
    r"(\d[\d,\.]*\s*"
    r"(%|ppm|ppb|kg|g|MJ|kJ|kW|MW|GW|TW|bar|°C|K|"
    r"원|달러|\$|€|¥|USD|KRW|EUR|"
    r"톤|Mt|Gt|kg\/|g\/|MJ\/|kWh|GWh|TWh|"
    r"nm|μm|mm|cm|m²|km|"
    r"건|개|척|기|명|년|월)"
    r"|N₂O|NOx|NH₃\s*슬립|파일럿\s*연료|pilot\s*fuel"
    r"|TRL\s*\d|탄소집약도|배출계수|LCOE|CAPEX|OPEX|TCO"
    r"|Well.to.Wake|생애주기|LCA)",
    re.IGNORECASE,
)

PROMPT_TEMPLATE = """\
당신은 R&D 기술 분석 전문가입니다. 아래 수집 자료(원문 발췌 포함)를 바탕으로
한국어 심층 기술 보고서를 Markdown 형식으로 작성하십시오.

[핵심 원칙]
1. 모든 정량 수치(숫자·단위·비율) 뒤에는 반드시 [등급 | 출처명] 을 붙일 것.
   예: "효율 74.4% [A | ScienceDirect 2025]", "비용 $24/MWh [A | Energy 2026]"
2. 비교표 셀은 반드시 아래 두 유형을 구분할 것:
   - '데이터 없음' : 자료를 검색했으나 수치를 찾지 못한 경우 (추후 보완 대상)
   - '해당 없음(N/A, 사유)' : 해당 기술 구조상 지표가 적용되지 않는 경우
     예: 태양광의 비상계획구역(EPZ) → "해당 없음(N/A, 방사성물질 없음)"
3. 기술적 한계·리스크·모순을 긍정 측면과 동등한 비중으로 다룰 것.
4. 시사점 각 항목은 반드시 '[문제 인식] → [구체 행동] → [기대 효과]' 3문장 구조.
   '따라서'로 시작하거나 끝나는 반복 패턴 금지.
5. 순수 Markdown 만 출력(코드펜스로 전체를 감싸지 말 것).

[필수 보고서 구조 — 아래 섹션을 순서대로 모두 작성할 것]

## 핵심 요약
한 줄 결론을 맨 앞에 기술한 뒤, 핵심 수치 3개 이상을 bullet로 나열.

## 1. 기술 개요
기술 정의·분류 체계·현재 기술 성숙도(TRL 범위).

## 2. 부상 배경 및 시장 맥락
기술 등장 배경(사회·경제·규제 드라이버), 시장 규모·성장 전망(CAGR, 목표 연도).

## 3. 주요 기술 경로 분석
각 경로의 원리·특성·대표 사례·TRL·실증/상업화 현황을 수치·출처 포함하여 기술.
※ 반드시 이 섹션을 완성한 뒤 4장 비교표를 작성할 것.

## 4. 정량 비교 분석
3장에서 설명한 기술 경로와 직접 경쟁·대안이 되는 기술들을 수집 자료에서 파악하여 비교표 구성.
비교 축(행)과 대상(열)은 주제에 맞게 스스로 결정.
셀 표기는 원칙 2번('데이터 없음' vs '해당 없음(N/A, 사유)')을 반드시 준수.

## 5. 쟁점 및 리스크
기술적 장벽, 경제성·비용 리스크, 규제·환경·사회적 리스크를 항목별로 구분.

## 6. 주요 플레이어 및 정책 환경
기업·기관 현황(국가별 분류 권장), 정책·규제·자금 동향.

## 7. 시사점 및 권고안
단기(~2년)와 중장기(3년~)로 구분. 각 항목: [문제 인식] → [구체 행동] → [기대 효과] 3문장.

## 참고 자료
수집된 자료를 S→A→B→C 순으로 정리. 각 항목: 등급, 제목, URL.
  [S] 정부·국제기구 공식자료
  [A] 동료심사 학술논문·주요기관 보고서
  [B] 업계·싱크탱크 보고서·교육자료
  [C] 참고용(위키백과·나무위키 등 — 팩트 확인 보조용으로만 인용)

---

[검색 주제]
{queries}

[수집 자료 {count}건]
{documents}
"""


def _find_claude() -> str | None:
    return shutil.which(CLAUDE_BIN)


def find_raw(slug: str) -> Path:
    """output/raw 에서 해당 slug 의 가장 최근 JSON 을 반환."""
    matches = sorted(RAW_DIR.glob(f"{slug}_*.json"))
    if not matches:
        raise FileNotFoundError(
            f"'{slug}' 에 해당하는 수집 JSON 이 없습니다: {RAW_DIR}/{slug}_*.json")
    return matches[-1]


def _find_sources_dir(raw_path: Path) -> Path | None:
    """raw JSON 경로에 대응하는 _sources 폴더. 없으면 None.

    검증본(`{slug}_{date}_v{n}.json`)은 전용 `_v{n}_sources` 폴더가 없을 수 있다.
    검증 루프는 신규 원문을 첫 수집의 base `_sources` 폴더에 누적하므로,
    `_v{n}` suffix 를 떼어낸 base `_sources` 로 폴백한다.
    """
    sources = raw_path.parent / f"{raw_path.stem}_sources"
    if sources.is_dir():
        return sources
    base_stem = re.sub(r"_v\d+$", "", raw_path.stem)
    if base_stem != raw_path.stem:
        base = raw_path.parent / f"{base_stem}_sources"
        if base.is_dir():
            return base
    return None


def _build_fulltext_index(sources_dir: Path) -> dict[str, Path]:
    """manifest.json 을 읽어 {url: 원문파일경로} 매핑 반환.

    URL 을 키로 쓰는 이유: 문서 목록은 검증 재수집·선별로 순서가 바뀔 수 있어
    위치(index) 기반 매핑은 어긋난다. 같은 URL 이 재시도로 여러 번 기록됐으면
    뒤(최신) ok 항목이 이긴다.
    """
    manifest_path = sources_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    index: dict[str, Path] = {}
    for entry in manifest:
        if entry.get("status") not in ("ok",):
            continue
        url = entry.get("url")
        saved = entry.get("saved", [])
        if not url or not saved:
            continue
        # .txt(PDF 추출본) 우선, 없으면 .md(HTML 추출본)
        txt = next((s for s in saved if s.endswith(".txt")), None)
        md  = next((s for s in saved if s.endswith(".md")), None)
        fname = txt or md
        if fname:
            fpath = sources_dir / fname
            if fpath.exists():
                index[url] = fpath
    return index


def _extract_key_sentences(text: str, max_chars: int) -> str:
    """단락 구분이 없는 짧은 텍스트용 폴백 — 수치 문장 우선 추출."""
    sentences = re.split(r"(?<=[.!?。])\s+|(?<=\n)\s*\n", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
    priority, rest = [], []
    for s in sentences:
        (priority if _QUANT_PATTERN.search(s) else rest).append(s)
    result_parts, total = [], 0
    for s in priority + rest:
        if total + len(s) > max_chars:
            break
        result_parts.append(s)
        total += len(s) + 1
    return " ".join(result_parts)


def _extract_key_paragraphs(text: str, max_chars: int) -> str:
    """단락(문단) 단위로 핵심 내용을 추출하되, 선발된 단락의 앞뒤를 문맥으로 포함한다.

    1. 빈 줄 기준으로 단락 분리
    2. 각 단락의 정량 수치 문장 수로 점수 계산
    3. 점수 높은 단락 우선 선발, 앞뒤 단락을 문맥으로 추가
    4. 원문 순서 그대로 재조합 (논리 흐름 유지)

    단락 수가 1개 이하인 경우(줄바꿈 없는 연속 텍스트)는 문장 단위 폴백 사용.
    """
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) > 30]

    if len(paragraphs) <= 1:
        return _extract_key_sentences(text, max_chars)

    # 단락별 정량 점수 (수치 포함 문장 수)
    scores = [
        sum(1 for s in re.split(r"(?<=[.!?。])\s+", p) if _QUANT_PATTERN.search(s))
        for p in paragraphs
    ]

    # 점수 내림차순으로 선발 순서 결정
    priority_order = sorted(range(len(paragraphs)), key=lambda i: -scores[i])

    selected: set[int] = set()
    total = 0
    for idx in priority_order:
        if total >= max_chars:
            break
        para_len = len(paragraphs[idx])
        if total + para_len > max_chars:
            continue
        selected.add(idx)
        total += para_len + 2  # 단락 구분자 2자 포함
        # 앞뒤 단락을 문맥으로 포함
        for neighbor in (idx - 1, idx + 1):
            if 0 <= neighbor < len(paragraphs) and neighbor not in selected:
                n_len = len(paragraphs[neighbor])
                if total + n_len <= max_chars:
                    selected.add(neighbor)
                    total += n_len + 2

    # 원문 순서로 재구성
    return "\n\n".join(paragraphs[i] for i in sorted(selected))


def extract_quant_sentences(text: str, max_sentences: int = 2,
                            max_chars: int = 220) -> str:
    """텍스트에서 정량 수치가 포함된 문장을 앞에서부터 최대 N개 추출한다.

    stage1 개요 작성자가 자료 배분 판단에 쓸 '수치 미리보기'용 —
    프로그래밍 추출이라 LLM 비용이 들지 않는다(2026-06-10 18차).
    """
    sentences = re.split(r"(?<=[.!?。])\s+|\n+", text)
    picked: list[str] = []
    total = 0
    for s in sentences:
        s = s.strip()
        if len(s) < 15 or not _QUANT_PATTERN.search(s):
            continue
        if total + len(s) > max_chars:
            s = s[: max_chars - total]
        picked.append(s)
        total += len(s)
        if len(picked) >= max_sentences or total >= max_chars:
            break
    return " / ".join(picked)


def _doc_block(doc: dict, fulltext_index: dict[str, Path]) -> str:
    """문서 1건을 프롬프트용 텍스트 블록으로 변환.

    원문 파일이 있으면(URL 키 매핑) 수치 문장 우선 추출, 없으면 스니펫 폴백.
    """
    grade = doc.get("trust_grade") or "-"
    title = doc.get("title", "(제목 없음)")
    url   = doc.get("url", "")
    src   = doc.get("source", "?")
    if src == "arxiv":
        # arXiv 는 동료심사 전 원고(preprint) — 작성 모델이 심사 논문과
        # 구분해 인용 무게를 조절하도록 라벨을 붙인다.
        src = "arxiv (preprint·동료심사 전)"

    # 원문 우선 → 라이브러리 → 스니펫 폴백
    fulltext_path = fulltext_index.get(url)
    if fulltext_path:
        raw_text = fulltext_path.read_text(encoding="utf-8", errors="ignore")
        content = _extract_key_paragraphs(raw_text, FULLTEXT_CHARS)
        source_tag = "원문"
    elif doc.get("source") == "library":
        raw_text = (doc.get("content") or "").strip()
        content = _extract_key_paragraphs(raw_text, LIBRARY_CHARS)
        source_tag = "라이브러리"
    else:
        content = (doc.get("content") or "").strip().replace("\n", " ")
        if len(content) > SNIPPET_CHARS:
            content = content[:SNIPPET_CHARS] + " …"
        source_tag = "스니펫"

    return (
        f"### [{grade}] {title}\n"
        f"출처: {src} ({source_tag}) | {url}\n"
        f"{content}"
    )


def build_prompt(payload: dict, sources_dir: Path | None = None) -> str:
    """수집 JSON payload + 원문 디렉토리 → claude 프롬프트 문자열.

    마스터 라이브러리(library/)의 문서를 항상 앞에 배치하고,
    이후 수집 문서를 선별(select_for_report: 점수 + 목록형 쿼터)해 MAX_DOCS건 채운다.
    """
    from dataclasses import asdict
    from src.collectors.library import load_library
    from src.processors.select import select_for_report

    queries = payload.get("queries", [])
    lib_docs = [asdict(d) for d in load_library(queries=queries)]

    # 원문 인덱스를 먼저 만들어 선별 점수(원문 확보 보너스)에 반영
    fulltext_index = _build_fulltext_index(sources_dir) if sources_dir else {}
    collected_docs = select_for_report(payload.get("documents", []), MAX_DOCS,
                                       fulltext_urls=set(fulltext_index))

    lib_blocks = [_doc_block(d, {}) for d in lib_docs]
    col_blocks = [_doc_block(d, fulltext_index) for d in collected_docs]

    queries_str = " / ".join(queries)
    total = len(lib_docs) + len(collected_docs)
    if lib_docs:
        print(f"  [report] 라이브러리 {len(lib_docs)}건 포함")

    return PROMPT_TEMPLATE.format(
        queries=queries_str,
        count=total,
        documents="\n\n".join(lib_blocks + col_blocks),
    )


def generate_report(slug: str, dry_run: bool = False) -> Path:
    """slug 의 수집 JSON + 원문으로 보고서(output/reports/{slug}.md)를 생성하고 경로 반환.

    예외:
      FileNotFoundError — 해당 slug 의 raw JSON 이 없을 때.
      RuntimeError      — claude CLI 부재 / 비정상 종료 / 빈 출력.
      subprocess.TimeoutExpired — 제한 시간 초과(그대로 전파).
    """
    raw_path = find_raw(slug)
    payload  = json.loads(raw_path.read_text(encoding="utf-8"))
    sources_dir = _find_sources_dir(raw_path)

    if sources_dir:
        ft_count = len(_build_fulltext_index(sources_dir))
        print(f"  [report] 원문 {ft_count}건 확보 → 수치 추출 모드")
    else:
        print("  [report] 원문 폴더 없음 → 스니펫 모드")

    prompt = build_prompt(payload, sources_dir=sources_dir)

    report_path = REPORT_DIR / f"{slug}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if dry_run:
        (REPORT_DIR / f"{slug}.prompt.txt").write_text(prompt, encoding="utf-8")
        return report_path

    claude = _find_claude()
    if not claude:
        raise RuntimeError(
            "claude CLI 를 PATH 에서 찾을 수 없습니다. Claude Code 설치를 확인하세요.")

    # stdout 을 파일로 직접 받아 메모리 버퍼 잘림 방지
    with open(report_path, "w", encoding="utf-8") as out_f:
        result = subprocess.run(
            [claude, "-p"],
            input=prompt,
            stdout=out_f,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_TIMEOUT,
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p 실패 (exit {result.returncode}): {result.stderr.strip()[:200]}")

    markdown = report_path.read_text(encoding="utf-8").strip()
    if not markdown:
        raise RuntimeError("claude -p 가 빈 출력을 반환했습니다.")

    report_path.write_text(markdown, encoding="utf-8")
    return report_path


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry  = "--dry-run" in sys.argv[1:]
    if not args:
        print('사용법: python -m src.generators.report "<slug>" [--dry-run]')
        sys.exit(1)

    slug = args[0]
    try:
        out = generate_report(slug, dry_run=dry)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[오류] {type(e).__name__}: {e}")
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print(f"[오류] 제한 시간({CLAUDE_TIMEOUT}s) 초과")
        sys.exit(3)

    if dry:
        print(f"[dry-run] 프롬프트 저장: {REPORT_DIR / (slug + '.prompt.txt')}")
        print(f"[dry-run] 보고서 예정 경로: {out}")
    else:
        print(f"[완료] 보고서 생성: {out}")
