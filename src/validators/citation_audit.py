"""수치 인용 감사 — 보고서의 '수치 [등급 | 출처]' 인용이 수집 원문에 실재하는지 검사한다.

LLM 비용 0: 정규식으로 보고서의 인용 수치를 뽑고, 로컬 자료 전체(_sources/ 원문,
raw JSON 스니펫, 라이브러리 캐시·텍스트)에서 그 수치 문자열을 검색한다.
어디에서도 발견되지 않는 수치는 '미확인'으로 표시해, Pro 리뷰가 환각(지어낸 수치)
여부를 집중 점검하게 한다 — 기존 검증 루프는 구조·논리·공백만 보고 수치의 실재
여부는 아무도 확인하지 않던 공백을 메운다.

검사 방식 (룩백):
  한국어 문장은 수치와 인용 괄호 사이에 서술어가 끼는 경우가 대부분이다
  ("효율이 74.4%에 달하는 것으로 보고되었습니다 [A | X]"). 그래서 '숫자가
  괄호 바로 앞'인 패턴만 보면 실측 87건 중 16건만 잡힌다. 대신 각 인용
  괄호의 '직전 구간'(같은 줄에서 이전 인용 이후 ~LOOKBACK자)에 등장하는
  모든 숫자 토큰을 검사 대상으로 삼는다.

한계 (의도된 보수성):
  - 출처명을 특정 파일에 매핑하지 않는다 — '어느' 원문이든 수치가 있으면 통과.
    출처명은 자유 표기("ScienceDirect 2025" 등)라 매핑이 불안정하다.
  - 반올림·단위 환산된 수치(1.4 GW ↔ 1,400 MW)는 미확인으로 나올 수 있다.
    그래서 결과는 '오류'가 아니라 '점검 필요' 로만 보고한다.
  - 연도(2026 등)와 한두 자리 수는 어디에나 있어 자동 통과된다 — 변별력 있는
    수치(74.4, 1400 등)만 실질 검사된다. 이것이 의도된 동작이다.

실행 예시 (단독 점검):
    .venv\\Scripts\\python.exe -m src.validators.citation_audit <slug>
"""
import re
import sys
import json
from pathlib import Path

from src.generators.report import find_raw, _find_sources_dir

REPORT_DIR = Path("output/reports")
LIBRARY_DIR = Path("library")
LIBRARY_CACHE = LIBRARY_DIR / ".cache"

# 인용 괄호 — PAIGE_STYLE 의 [등급 | 출처] 표기
_CITE_BRACKET = re.compile(r"\[(SS|S|AA|A|B|C)\s*\|\s*([^\]]{1,80})\]")
_NUM_TOKEN = re.compile(r"\d[\d,\.]*")
_CHAPTER_PAT = re.compile(r"^##\s*(\d)\.")

LOOKBACK = 80       # 인용 괄호 앞에서 수치를 찾는 구간(자)
MAX_FINDINGS = 30   # Pro 리뷰 프롬프트에 첨부할 미확인 항목 상한


def _span_numbers(span: str) -> list[str]:
    """인용 직전 구간에서 검증 대상 숫자 토큰을 뽑는다 (연도 제외)."""
    toks: list[str] = []
    for m in _NUM_TOKEN.finditer(span):
        t = m.group().replace(",", "").rstrip(".")
        if not t or re.fullmatch(r"(19|20)\d{2}", t):
            continue  # 연도는 어디에나 있어 변별력 없음
        if t not in toks:
            toks.append(t)
    return toks


def _build_corpus(slug: str) -> str:
    """수집 자료 전체를 하나의 검색용 문자열로 (쉼표 제거 — 천 단위 표기 통일)."""
    parts: list[str] = []

    raw = find_raw(slug)
    payload = json.loads(raw.read_text(encoding="utf-8"))
    for d in payload.get("documents", []):
        parts.append(d.get("title") or "")
        parts.append(d.get("content") or "")

    sources = _find_sources_dir(raw)
    if sources:
        for f in sources.iterdir():
            if f.suffix in (".md", ".txt"):
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass

    if LIBRARY_CACHE.exists():
        for f in LIBRARY_CACHE.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                parts.extend(data.get("pages") or [])
            except (json.JSONDecodeError, OSError):
                pass
    if LIBRARY_DIR.exists():
        for f in LIBRARY_DIR.rglob("*"):
            if (f.is_file() and f.suffix in (".md", ".txt")
                    and ".cache" not in f.parts):
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass

    return "\n".join(parts).replace(",", "")


def audit_findings(slug: str) -> tuple[int, list[dict]]:
    """보고서의 정량 인용을 검사해 (정량 인용 수, 미확인 항목 구조 목록)을 반환.

    각 [등급 | 출처] 괄호마다 직전 구간(같은 줄, 이전 인용 이후 최대
    LOOKBACK자)의 숫자 토큰을 모아 원문 코퍼스에서 검색한다.

    각 항목: {"chapter": "1"~"7" 또는 "?", "ctx": 인용 직전 문맥+괄호,
              "missing": [미발견 수치 토큰(쉼표 제거 정규화)]}
    검증 루프의 결정적 가드(report_validator._hallucination_guard)가 이 구조를
    직접 소비한다 — Pro/Flash 자유 서술이 누락해도 챕터 패치 노트로 강제 반영(21차).
    """
    report_path = REPORT_DIR / f"{slug}.md"
    if not report_path.exists():
        raise FileNotFoundError(f"보고서를 찾을 수 없습니다: {report_path}")
    text = report_path.read_text(encoding="utf-8")
    corpus = _build_corpus(slug)

    findings: list[dict] = []
    flagged: set[str] = set()
    total = 0
    chapter = "?"
    for line in text.splitlines():
        m_ch = _CHAPTER_PAT.match(line.strip())
        if m_ch:
            chapter = m_ch.group(1)
        prev_end = 0
        for m in _CITE_BRACKET.finditer(line):
            span = line[prev_end:m.start()][-LOOKBACK:]
            prev_end = m.end()
            toks = _span_numbers(span)
            if not toks:
                continue  # 정량 인용 아님 (서술 인용)
            total += 1
            missing = [t for t in toks if t not in corpus]
            if not missing:
                continue
            key = ",".join(missing) + m.group(0)
            if key in flagged:
                continue
            flagged.add(key)
            ctx = (span[-40:] + m.group(0)).strip()
            findings.append({"chapter": chapter, "ctx": ctx, "missing": missing})
            if len(findings) >= MAX_FINDINGS:
                return total, findings
    return total, findings


def audit_report(slug: str) -> tuple[int, list[str]]:
    """audit_findings 를 사람이 읽는 마크다운 줄 목록으로 변환 (리뷰 프롬프트용)."""
    total, findings = audit_findings(slug)
    lines = [f'- (Ch{f["chapter"]}) "…{f["ctx"]}" — '
             f'미발견 수치: {", ".join(f["missing"])}' for f in findings]
    return total, lines


def run_audit(slug: str, out_path: Path | None = None) -> str:
    """감사를 실행해 리뷰 프롬프트용 마크다운 블록을 반환 (미확인 0건이면 빈 문자열).

    out_path 를 주면 결과를 파일로도 남긴다 (검증 이력 추적용).
    """
    total, findings = audit_report(slug)
    if not findings:
        print(f"  [audit] 수치 인용 {total}건 모두 수집 원문에서 확인됨")
        if out_path:
            out_path.write_text(
                f"수치 인용 {total}건 모두 수집 원문에서 확인되었습니다.\n",
                encoding="utf-8")
        return ""

    header = (
        f"[자동 수치 인용 감사 — 정규식·로컬 검색 결과 (LLM 아님)]\n"
        f"보고서의 수치 인용 {total}건 중 아래 {len(findings)}건은 수집 원문 "
        f"어디에서도 같은 수치를 찾지 못했습니다. 반올림·단위 환산일 수도 있으나, "
        f"근거 없는 수치(환각)라면 해당 챕터의 약점으로 반드시 지적하세요.\n")
    md = header + "\n".join(findings) + "\n"
    if out_path:
        out_path.write_text(md, encoding="utf-8")
    print(f"  [audit] 미확인 수치 {len(findings)}/{total}건"
          + (f" → {out_path.name}" if out_path else ""))
    return md


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('사용법: python -m src.validators.citation_audit "<slug>"')
        sys.exit(1)
    result = run_audit(sys.argv[1])
    print(result or "(미확인 수치 없음)")
