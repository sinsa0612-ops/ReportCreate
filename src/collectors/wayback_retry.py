"""Wayback 재시도 — manifest 의 failed/thin 항목을 아카이브 스냅샷에서 회수한다.

fetch(httpx) → crawl_retry(브라우저) 로도 못 뚫은 봇차단/Cloudflare 자료를
archive.org 스냅샷으로 회수하는 3차 폴백.

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.collectors.wayback_retry output/raw/xxx_sources
"""
import sys
import re
import json
from pathlib import Path

from src.collectors.wayback import wayback_fetch


def safe_name(idx: int, title: str, ext: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug)[:40] or "untitled"
    return f"{idx:02d}_{slug}.{ext}"


def wayback_retry(sources_dir: str | Path) -> None:
    sources_dir = Path(sources_dir)
    manifest_path = sources_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_index = {m["index"]: m for m in manifest}

    # pre_fetched(가상 URL)는 아카이브에 스냅샷이 있을 수 없다 — 대상 제외.
    targets = [m for m in manifest if m["status"] in ("failed", "thin")
               and m.get("kind") != "pre_fetched"]
    print(f"[wayback] 재시도 대상 {len(targets)}건")
    if not targets:
        print("[wayback] 재시도 대상 없음 — 건너뜀")
        return

    for t in targets:
        idx, url = t["index"], t["url"]
        try:
            text, snap = wayback_fetch(url)
        except Exception as e:
            print(f"  [{idx:02d}] FAIL  {type(e).__name__}: {str(e)[:45]}")
            continue

        if text and len(text) >= 200:
            md_path = sources_dir / safe_name(idx, t["title"], "md")
            md_path.write_text(text, encoding="utf-8")
            rec = by_index[idx]
            rec.update(status="ok", kind="wayback", chars=len(text),
                       saved=[md_path.name], via="wayback", snapshot=snap)
            rec.pop("error", None)
            print(f"  [{idx:02d}] OK     {len(text):>6}  {t['title'][:44]}")
        else:
            tag = "스냅샷없음" if not snap else "본문부족"
            print(f"  [{idx:02d}] --     {tag}  {t['title'][:40]}")

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    ok = sum(1 for m in manifest if m["status"] == "ok")
    thin = sum(1 for m in manifest if m["status"] == "thin")
    fail = sum(1 for m in manifest if m["status"] == "failed")
    print(f"\n[wayback] 갱신 후 ok={ok} thin={thin} failed={fail} / {len(manifest)}")


if __name__ == "__main__":
    default = ("output/raw/"
               "green-hydrogen-production-electrolysis-technology_2026-06-05_sources")
    wayback_retry(sys.argv[1] if len(sys.argv) > 1 else default)
