"""통합 파이프라인 — 주제(쿼리) 입력 한 번으로 끝까지 자동 실행한다.

    수집(Tavily+Exa) → 원문 저장(httpx) → 봇차단 회수(crawl4ai)

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.main "green hydrogen production" "electrolyzer cost 2025"

수집·원문 확보까지 자동으로 끝나며, 보고서 작성은 Claude Code 세션이
output/raw 의 결과(JSON + 원문)를 읽어 마무리한다.
"""
import sys
import json
import asyncio

from dotenv import load_dotenv

from src.pipeline import collect, save_raw, slugify
from src.collectors.fetch import fetch_sources
from src.generators.report import MAX_DOCS
from src.generators.staged_report import run_all as generate_report
from src.processors.select import select_for_report
from src.report_templates import all_templates, get_template
from src.locks import slug_lock

load_dotenv()

BAR = "=" * 60


KNOWN_FLAGS = {"--generate", "--validate", "--max-iter", "--template"}


def parse_args(argv: list[str]) -> tuple[list[str], bool, bool, int, str | None]:
    """CLI 인자에서 쿼리 목록과 플래그를 분리한다.

    --generate         보고서 자동생성
    --validate         생성 후 agy 검증 루프 실행 (--generate 포함)
    --max-iter N       검증 루프 최대 반복 횟수 (기본 1)
    --template NAME    보고서 양식(templates/<NAME>.json). 미지정 시 기본 양식.

    알 수 없는 --옵션은 즉시 에러로 중단한다 — 오타(--validat 등)가 조용히
    무시되어 몇 시간짜리 실행이 의도와 다르게 도는 사고를 막는다.
    """
    generate = "--generate" in argv or "--validate" in argv
    validate = "--validate" in argv
    max_iter = 1
    template: str | None = None
    for i, a in enumerate(argv):
        if a == "--max-iter" and i + 1 < len(argv):
            try:
                max_iter = int(argv[i + 1])
            except ValueError:
                pass
        if a == "--template" and i + 1 < len(argv):
            template = argv[i + 1]
    skip_next = False
    queries: list[str] = []
    unknown: list[str] = []
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in ("--max-iter", "--template"):
            skip_next = True
            continue
        if a.startswith("--"):
            if a not in KNOWN_FLAGS:
                unknown.append(a)
            continue
        queries.append(a)
    if unknown:
        print(f"[오류] 알 수 없는 옵션: {' '.join(unknown)}")
        print("       사용 가능한 옵션: --generate --validate --max-iter N --template NAME")
        sys.exit(1)
    if template is not None and template not in all_templates():
        avail = ", ".join(sorted(all_templates())) or "(없음)"
        print(f"[오류] 알 수 없는 양식(--template): {template}")
        print(f"       사용 가능한 양식: {avail}")
        sys.exit(1)
    return queries, generate, validate, max_iter, template


def run(queries: list[str], per_query: int = 5,
        generate: bool = False, validate: bool = False,
        max_iter: int = 2, template: str | None = None) -> None:
    slug = slugify(queries[0])
    with slug_lock(slug):
        _run_inner(queries, slug, per_query, generate, validate, max_iter, template)


def _run_inner(queries: list[str], slug: str, per_query: int,
               generate: bool, validate: bool, max_iter: int,
               template: str | None = None) -> None:
    print(BAR)
    print(f"[1/4] 검색 수집  | queries = {queries}")
    if template:
        print(f"       양식: {get_template(template).label} ({template})")
    print(BAR)
    docs = collect(queries, per_query=per_query)
    json_path = save_raw(slug, queries, docs, template=template)
    print(f"  -> {len(docs)} unique docs · {json_path}\n")

    print(BAR)
    print("[2/4] 원문 저장  | httpx + trafilatura + pymupdf")
    print(BAR)
    # 보고서에 투입될 선별 문서만 원문을 받는다 — 전량 다운로드는 시간·대역폭의
    # ~87%가 미사용 자료에 지출되던 낭비(평가 보고서 H3). 선별 함수가 결정적이라
    # 보고서 생성 시점의 선별 결과와 동일한 집합이 fetch 된다.
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    selected = select_for_report(payload.get("documents", []), MAX_DOCS)
    only = {d["url"] for d in selected}
    print(f"  선별 {len(only)}건만 원문 저장 (전체 {len(docs)}건 중)")
    sources_dir = fetch_sources(json_path, only_urls=only)
    print()

    print(BAR)
    print("[3/4] 봇차단 회수 | crawl4ai")
    print(BAR)
    try:
        from src.collectors.crawl_retry import crawl_retry
        asyncio.run(crawl_retry(sources_dir))
    except Exception as e:
        print(f"  [skip] crawl4ai 재시도 생략: {type(e).__name__}: {e}")

    print(BAR)
    print("[4/4] 아카이브 폴백 | wayback")
    print(BAR)
    try:
        from src.collectors.wayback_retry import wayback_retry
        wayback_retry(sources_dir)
    except Exception as e:
        print(f"  [skip] wayback 폴백 생략: {type(e).__name__}: {e}")

    manifest = json.loads((sources_dir / "manifest.json").read_text(encoding="utf-8"))
    ok = sum(1 for m in manifest if m["status"] == "ok")
    print("\n" + BAR)
    print(f"[완료] 수집 {len(docs)}건 · 원문 {ok}/{len(manifest)}건 확보")
    print(f"  수집 JSON : {json_path}")
    print(f"  원문 폴더 : {sources_dir}")

    if generate:
        print(BAR)
        print("[보고서] 단계적 생성 — 개요(Sonnet) → 챕터(Opus) → 조합(Sonnet)")
        print(BAR)
        try:
            report_path = generate_report(slug)
            print(f"  -> 보고서 생성: {report_path}")
        except Exception as e:
            print(f"  [skip] 보고서 생성 실패: {type(e).__name__}: {e}")
    else:
        print(f"  다음 단계 : python -m src.generators.staged_report \"{slug}\" --auto  (또는 --generate 로 자동)")

    if validate:
        print(BAR)
        print("[검증] agy 보고서 검증·보완 루프")
        print(BAR)
        try:
            from src.validators.report_validator import run_validation_loop
            run_validation_loop(slug, max_iterations=max_iter)
        except Exception as e:
            print(f"  [skip] 검증 루프 실패: {type(e).__name__}: {e}")

    print(BAR)


if __name__ == "__main__":
    queries, generate, validate, max_iter, template = parse_args(sys.argv[1:])
    if not queries:
        print('사용법: python -m src.main "주제 또는 쿼리" ["쿼리2" ...] '
              '[--generate] [--validate] [--max-iter N] [--template NAME]')
        sys.exit(1)
    run(queries, generate=generate, validate=validate, max_iter=max_iter,
        template=template)
