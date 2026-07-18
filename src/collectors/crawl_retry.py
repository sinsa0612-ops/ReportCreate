"""crawl4ai 재시도 — manifest의 failed/thin 항목을 실제 브라우저로 재수집한다.

httpx 가 403 으로 막힌 봇차단 사이트(IEA, Wikipedia, MDPI 등)를 헤드리스
브라우저로 우회한다. 성공분만 기존 파일/manifest 를 갱신한다.

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.collectors.crawl_retry output/raw/xxx_sources
"""
import sys
import re
import json
import asyncio
from pathlib import Path

from src.collectors.fetch import _is_blocked_page, _warn_repeated_sizes

# Windows 에서 Playwright(subprocess) 구동을 위한 이벤트 루프 정책
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def safe_name(idx: int, title: str, ext: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug)[:40] or "untitled"
    return f"{idx:02d}_{slug}.{ext}"


async def crawl_retry(sources_dir: str | Path) -> None:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.async_configs import CacheMode

    sources_dir = Path(sources_dir)
    manifest_path = sources_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_index = {m["index"]: m for m in manifest}

    # pre_fetched(가스안전공사 등)는 odcloud 가상 식별자라 실제 웹페이지가 아니다 —
    # 브라우저로 재시도해도 회수 불가능하고 시간만 쓴다. 대상에서 제외.
    targets = [m for m in manifest if m["status"] in ("failed", "thin")
               and m.get("kind") != "pre_fetched"]
    print(f"[crawl] 재시도 대상 {len(targets)}건")
    if not targets:
        print("[crawl] 재시도 대상 없음 — 건너뜀")
        return

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=60000)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for t in targets:
            idx, url = t["index"], t["url"]
            try:
                result = await crawler.arun(url=url, config=run_cfg)
                if not result.success:
                    raise RuntimeError(getattr(result, "error_message", "crawl failed"))

                md = result.markdown
                text = getattr(md, "raw_markdown", None) or str(md or "")
                rec = by_index[idx]

                # 캡차/봇차단 안내 페이지가 'OK'로 위장 통과하는 것을 차단(21차 문제3
                # 실측: ScienceDirect 캡차 11건이 동일 2,519자로 ok 기록).
                # failed 로 남겨 wayback_retry 재시도 대상에 포함시킨다(kind 유지).
                if _is_blocked_page(text):
                    rec["status"] = "failed"
                    rec["error"] = "blocked page (captcha/bot-wall via crawl4ai)"
                    print(f"  [{idx:02d}] BLOCK  {len(text):>6}  {t['title'][:45]}")
                    continue

                if len(text) < 200:
                    rec["status"] = "thin"
                    print(f"  [{idx:02d}] thin   {len(text):>6}  {t['title'][:45]}")
                    continue

                md_path = sources_dir / safe_name(idx, t["title"], "md")
                md_path.write_text(text, encoding="utf-8")
                rec.update(status="ok", kind="html", chars=len(text),
                           saved=[md_path.name], via="crawl4ai")
                rec.pop("error", None)
                print(f"  [{idx:02d}] OK     {len(text):>6}  {t['title'][:45]}")
            except Exception as e:
                print(f"  [{idx:02d}] FAIL   {type(e).__name__}: {str(e)[:50]}")

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    _warn_repeated_sizes(manifest)
    ok = sum(1 for m in manifest if m["status"] == "ok")
    thin = sum(1 for m in manifest if m["status"] == "thin")
    fail = sum(1 for m in manifest if m["status"] == "failed")
    print(f"\n[crawl] 갱신 후 ok={ok} thin={thin} failed={fail} / {len(manifest)}")


if __name__ == "__main__":
    default = ("output/raw/"
               "green-hydrogen-production-electrolysis-technology_2026-06-05_sources")
    asyncio.run(crawl_retry(sys.argv[1] if len(sys.argv) > 1 else default))
