"""검증 재개 드라이버 — 기존 queries-v{n}.json 을 재사용해
재수집(3) → 병합(4) → 보고서 재생성(5) 만 수행한다.

agy(Pro/Flash) 재호출을 건너뛴다. 이미 생성된 리뷰·쿼리 플랜이 있을 때 사용.

실행:
    .venv\\Scripts\\python.exe -m scripts.resume_validation "<slug>" [--iter N]
"""
import sys
from pathlib import Path

from src.validators.report_validator import (
    REVIEW_DIR, BAR,
    parse_query_plan, collect_and_merge,
)
from src.generators.staged_report import run_all as generate_report


def main() -> int:
    argv = sys.argv[1:]
    slugs = [a for a in argv if not a.startswith("--")]
    if not slugs:
        print('사용법: python -m scripts.resume_validation "<slug>" [--iter N]')
        return 1
    slug = slugs[0]

    iteration = 1
    for i, a in enumerate(argv):
        if a == "--iter" and i + 1 < len(argv):
            iteration = int(argv[i + 1])

    queries_path = REVIEW_DIR / f"{slug}-queries-v{iteration}.json"
    if not queries_path.exists():
        print(f"[오류] 쿼리 플랜이 없습니다: {queries_path}")
        return 2

    print(BAR)
    print(f"[검증 재개] slug={slug}  iter={iteration}")
    print(f"  기존 쿼리 플랜 재사용: {queries_path.name}")
    print(BAR)

    should, queries = parse_query_plan(queries_path)
    if not should or not queries:
        print("  재수집 불필요 → 종료")
        return 0

    _merged, added = collect_and_merge(slug, queries, iteration)
    if added == 0:
        print("  신규 문서 0건 → 재생성 생략, 종료")
        return 0

    # 재생성 전 기존 초안 제거 — staged_report 의 '기존 챕터 스킵' 로직이
    # 병합된 새 자료를 반영하지 못하게 막으므로, outline·챕터를 비워 전면 재작성한다.
    draft_dir = Path("output/drafts") / slug
    if draft_dir.exists():
        removed = 0
        for p in list(draft_dir.glob("ch??_*.md")) + \
                 [draft_dir / "outline.json", draft_dir / "outline.md"]:
            if p.exists():
                p.unlink()
                removed += 1
        print(f"  [reset] 기존 초안 {removed}개 제거 (전면 재작성 위함)")

    print(BAR)
    print("[보고서 재생성] 단계적 생성 (개요→챕터→조합)")
    print(BAR)
    report_path = generate_report(slug)
    print(f"  [report] 완료: {report_path}")

    print(BAR)
    print(f"[검증 재개 완료] output/reports/{slug}.md")
    print(BAR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
