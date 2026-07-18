"""Wayback Machine 폴백 — 봇차단/Cloudflare 로 막힌 자료를 아카이브 스냅샷에서 회수.

archive.org 는 봇차단을 하지 않으므로, 공식 URL 이 막혀도 과거 스냅샷에서
본문을 가져올 수 있다. HTML 은 trafilatura, PDF 는 pymupdf 로 추출한다.
"""
import httpx
import trafilatura

AVAIL = "http://archive.org/wayback/available"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def wayback_snapshot(url: str, timeout: float = 30) -> str | None:
    """해당 URL 의 가장 가까운 아카이브 스냅샷 URL 을 반환(없으면 None)."""
    try:
        av = httpx.get(AVAIL, params={"url": url}, timeout=timeout,
                       follow_redirects=True).json()
    except Exception:
        return None
    return av.get("archived_snapshots", {}).get("closest", {}).get("url")


def wayback_fetch(url: str, timeout: float = 60) -> tuple[str, str | None]:
    """(추출 텍스트, 스냅샷 URL) 반환. 실패 시 ('', snapshot_or_None)."""
    snap = wayback_snapshot(url)
    if not snap:
        return "", None
    try:
        r = httpx.get(snap, headers=UA, follow_redirects=True, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return "", snap

    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or snap.lower().split("?")[0].endswith(".pdf"):
        try:
            import fitz
            with fitz.open(stream=r.content, filetype="pdf") as doc:
                text = "\n".join(p.get_text() for p in doc)
        except Exception:
            text = ""
    else:
        text = trafilatura.extract(r.text, include_links=True) or ""
    return text, snap
