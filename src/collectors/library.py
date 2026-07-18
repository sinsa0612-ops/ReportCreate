"""마스터 라이브러리 수집기 — library/ 폴더에 수동 저장된 중요 문서(PDF·텍스트)를 읽어
Document 리스트로 반환한다.

사용 방법:
  IEA·IRENA·정부 보고서 등 공신력 있는 PDF를 library/ 폴더에 넣으면
  이후 모든 보고서 생성 시 S급 참고자료로 자동 포함된다.

  하위 폴더로 분류 가능:
    library/iea/World_Energy_Outlook_2024.pdf
    library/irena/Renewable_Power_2025.pdf

PDF 청킹 전략 (2단계 압축):
  1단계 — 페이지 점수 선발:
    앞 ALWAYS_PAGES(5p)는 executive summary로 항상 포함.
    나머지는 쿼리 키워드 매칭 점수 상위 페이지를 MAX_PAGES(20p)까지 선발하고
    선발된 페이지의 앞뒤 1페이지를 문맥으로 추가 (논리 흐름 보존).
  2단계 — 단락 압축:
    report.py의 _extract_key_paragraphs 가 LIBRARY_CHARS(8000자)로 최종 압축.
  결과: 300페이지 보고서에서 주제 관련 핵심 20페이지 → 8,000자.

페이지 텍스트 캐시 (library/.cache/):
  PDF→페이지별 텍스트 추출(pymupdf)은 쿼리와 무관하게 항상 동일하므로 1회만 수행하고
  `library/.cache/{경로}.json` 에 저장한다(PDF mtime 으로 무효화). 페이지 선발·청킹은
  캐시된 텍스트 위에서 매번 수행. 검증 루프가 같은 PDF(33MB·46MB)를 2~3회 재생성하며
  매번 재파싱하던 시간 낭비를 제거한다.
"""
import re
import json
from pathlib import Path

from src.models import Document

LIBRARY_DIR = Path("library")
CACHE_DIRNAME = ".cache"
ALWAYS_PAGES = 5   # 앞 N페이지 무조건 포함 (executive summary / 목차)
MAX_PAGES = 20     # 선발할 최대 페이지 수 (인접 페이지 포함 합산)


def _query_terms(queries: list[str]) -> set[str]:
    """쿼리 문자열에서 2글자 이상 단어를 소문자로 추출."""
    terms: set[str] = set()
    for q in queries:
        for word in re.findall(r"[a-zA-Z가-힣]{2,}", q.lower()):
            terms.add(word)
    return terms


def _score_page(text: str, terms: set[str]) -> int:
    """페이지 텍스트에 쿼리 키워드가 몇 번 등장하는지 합산."""
    t = text.lower()
    return sum(t.count(term) for term in terms)


# ─── 페이지 텍스트 추출 + 캐시 ──────────────────────────────────────────────

def _extract_pages(path: Path) -> list[str]:
    """PDF 페이지별 텍스트 추출 (쿼리 무관 — 캐시 대상)."""
    import fitz  # pymupdf
    with fitz.open(str(path)) as doc:
        return [page.get_text() for page in doc]


def _cache_key(path: Path, library_dir: Path) -> str:
    """library_dir 기준 상대경로를 안전한 파일명으로 (다른 폴더 동명 PDF 충돌 방지)."""
    try:
        rel = path.relative_to(library_dir).as_posix()
    except ValueError:
        rel = path.name
    return re.sub(r"[^\w.-]", "_", rel)


def _cached_pages(path: Path, cache_dir: Path, library_dir: Path) -> list[str]:
    """페이지 텍스트를 캐시에서 읽거나, 미스 시 추출 후 캐시에 저장한다.

    캐시 무효화: PDF mtime 이 캐시 기록과 다르면 재추출.
    """
    cache_file = cache_dir / f"{_cache_key(path, library_dir)}.json"
    mtime = path.stat().st_mtime

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("mtime") == mtime and isinstance(data.get("pages"), list):
                return data["pages"]
        except (json.JSONDecodeError, OSError):
            pass  # 손상된 캐시 → 재추출

    pages = _extract_pages(path)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"mtime": mtime, "pages": pages}, ensure_ascii=False),
            encoding="utf-8")
    except OSError as e:
        print(f"  [library] 캐시 저장 실패 {cache_file.name}: {e}")
    return pages


# ─── 페이지 선발 (쿼리 의존 — 캐시 안 함) ──────────────────────────────────

def _select_pages_text(pages: list[str], query_terms: set[str]) -> str:
    """추출된 페이지 텍스트에서 쿼리 관련 페이지를 선발해 재구성한다(순수함수).

    query_terms 가 비어 있거나 짧은 문서는 전체 텍스트를 그대로 반환
    (이후 _extract_key_paragraphs 가 8000자로 압축).
    """
    if not pages:
        return ""

    # 쿼리 없거나 짧은 문서는 전체 반환
    if not query_terms or len(pages) <= ALWAYS_PAGES + 3:
        return "\n".join(pages)

    # 앞 ALWAYS_PAGES는 항상 선발
    selected: set[int] = set(range(min(ALWAYS_PAGES, len(pages))))

    # 나머지 페이지를 점수 내림차순으로 정렬
    scored = sorted(
        ((i, _score_page(text, query_terms)) for i, text in enumerate(pages) if i >= ALWAYS_PAGES),
        key=lambda x: -x[1],
    )

    for idx, score in scored:
        if len(selected) >= MAX_PAGES:
            break
        if score == 0:
            break  # 점수 0 = 관련 없는 페이지, 이후도 0이므로 중단
        selected.add(idx)
        # 앞뒤 1페이지를 문맥으로 추가
        for neighbor in (idx - 1, idx + 1):
            if 0 <= neighbor < len(pages) and neighbor not in selected and len(selected) < MAX_PAGES:
                selected.add(neighbor)

    # 원문 순서로 재구성, 건너뛴 구간에 구분자 삽입
    parts: list[str] = []
    prev = -2
    for i in sorted(selected):
        if i > prev + 1:
            parts.append(f"--- [p.{i + 1}] ---")
        parts.append(pages[i])
        prev = i

    return "\n".join(parts)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def list_library(library_dir: Path = LIBRARY_DIR) -> list[Document]:
    """내용을 읽지 않고 파일 목록만 Document(메타데이터)로 반환 — 참고문헌 용도.

    load_library 와 달리 PDF 파싱·페이지 캐시 로드를 전혀 하지 않는다.
    stage3 가 참고 자료 섹션을 만들 때 제목·URL·등급만 필요해서 분리했다.
    """
    if not library_dir.exists():
        return []
    docs: list[Document] = []
    for path in sorted(library_dir.rglob("*")):
        if not path.is_file() or CACHE_DIRNAME in path.parts:
            continue
        if path.suffix.lower() not in (".pdf", ".txt", ".md"):
            continue
        rel = path.relative_to(library_dir)
        docs.append(Document(
            title=path.stem.replace("_", " ").replace("-", " "),
            url=f"library://{rel.as_posix()}",
            content="",
            source="library",
            source_type="report",
            trust_grade="S",
            trust_score=95.0,
        ))
    return docs


def load_library(library_dir: Path = LIBRARY_DIR,
                 queries: list[str] | None = None) -> list[Document]:
    """library/ 폴더의 모든 PDF·txt·md 파일을 Document로 변환해 반환한다.

    queries 를 넘기면 PDF를 쿼리 관련 페이지 중심으로 스마트 추출한다.
    PDF 페이지 텍스트는 library/.cache/ 에 캐시되어 재파싱을 피한다.
    폴더가 없거나 비어 있으면 빈 리스트 반환.
    """
    if not library_dir.exists():
        return []

    terms = _query_terms(queries or [])
    cache_dir = library_dir / CACHE_DIRNAME
    docs: list[Document] = []

    for path in sorted(library_dir.rglob("*")):
        if not path.is_file():
            continue
        if CACHE_DIRNAME in path.parts:
            continue  # 캐시 폴더 내용은 문서로 읽지 않음
        suffix = path.suffix.lower()

        try:
            if suffix == ".pdf":
                pages = _cached_pages(path, cache_dir, library_dir)
                content = _select_pages_text(pages, terms)
            elif suffix in (".txt", ".md"):
                content = _read_text(path)
            else:
                continue
        except Exception as e:
            print(f"  [library] {path.name} 읽기 실패: {e}")
            continue

        content = content.strip()
        if not content:
            continue

        title = path.stem.replace("_", " ").replace("-", " ")
        rel = path.relative_to(library_dir)
        page_info = f", 상위 ~{MAX_PAGES}p 선발" if suffix == ".pdf" and terms else ""
        print(f"  [library] {rel} ({len(content):,}자{page_info})")

        docs.append(Document(
            title=title,
            url=f"library://{rel.as_posix()}",
            content=content,
            source="library",
            source_type="report",
            trust_grade="S",
            trust_score=95.0,
        ))

    return docs
