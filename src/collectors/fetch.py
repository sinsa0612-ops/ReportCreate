"""원문 수집기 — raw JSON의 각 URL을 로컬에 원문으로 저장한다.

- PDF  : 원본 .pdf 보존 + pymupdf 로 텍스트 추출(.txt)
- HTML : trafilatura 로 본문만 추출(.md)
- 결과/실패는 manifest.json 에 기록

실행 예시 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m src.collectors.fetch output/raw/xxx.json
"""
import sys
import re
import json
from pathlib import Path

import httpx

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def safe_name(idx: int, title: str, ext: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug)[:40] or "untitled"
    return f"{idx:02d}_{slug}.{ext}"


def extract_pdf(data: bytes) -> str:
    import fitz  # pymupdf
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def extract_html(data: bytes) -> str:
    import trafilatura
    html = data.decode("utf-8", errors="ignore")
    return trafilatura.extract(html, include_links=True) or ""


# 차단 페이지(캡차·봇월) 표준 문구 — HTTP 200 + 비어있지 않은 본문이라
# 기존 성공 판정을 통과하지만 실제 원문이 아니다(21차 실측: ScienceDirect
# 캡차 11건이 "ok 2,519 chars"로 위장 통과해 챕터 프롬프트에 주입됨).
_BLOCK_PATTERNS = re.compile(
    r"(are you a robot|confirm (that )?you are a human|completing the captcha|"
    r"captcha challenge|reference number:|detected unusual traffic|"
    r"access to this page has been denied|verify you are a human|"
    r"checking your browser before accessing|enable javascript and cookies to continue)",
    re.IGNORECASE)


def _is_blocked_page(text: str) -> bool:
    """본문이 캡차/봇차단 안내 페이지인지 검사한다(앞부분 4,000자만)."""
    return bool(_BLOCK_PATTERNS.search(text[:4000]))


def _warn_repeated_sizes(manifest: list[dict], min_repeat: int = 3) -> None:
    """같은 manifest 안에서 동일 글자 수의 'ok' 문서가 반복되면 경고한다.

    캡차 페이지 같은 정형 템플릿은 바이트 수가 똑같이 반복된다 —
    _is_blocked_page 의 문구 패턴이 놓친 차단 페이지의 2차 방어선(21차 문제3).
    """
    counts: dict[int, int] = {}
    for m in manifest:
        if m.get("status") == "ok" and m.get("chars"):
            counts[m["chars"]] = counts.get(m["chars"], 0) + 1
    for size, n in sorted(counts.items()):
        if n >= min_repeat:
            print(f"  [경고] 동일 크기({size:,}자) ok 문서가 {n}건 — "
                  f"캡차/차단 템플릿 의심, 해당 원문 내용을 확인하십시오")


def _alt_urls(d: dict) -> list[str]:
    """본문 확보 실패(thin/failed) 시 시도할 대체 원문 URL 목록.

    수집기가 이미 저장해 둔 메타데이터를 활용한다 — 추가 API 비용 0:
      - Semantic Scholar: metadata.openAccessPdf (무료 PDF 직링크).
        랜딩 페이지(semanticscholar.org)는 본문이 없어 항상 thin 이었다(실측 13건).
      - arXiv: metadata.pdf_url (전문 PDF — 초록 페이지보다 수치 추출 풀이 넓다)
      - MDPI: 논문 URL 뒤 /pdf 가 공개 PDF 직링크 (봇차단 우회)
    """
    md = d.get("metadata") or {}
    url = d.get("url") or ""
    alts: list[str] = []
    oa = md.get("openAccessPdf")
    if isinstance(oa, dict):          # S2 원시 응답 형태 대비
        oa = oa.get("url")
    if oa:
        alts.append(str(oa))
    if md.get("pdf_url"):
        alts.append(str(md["pdf_url"]))
    if "mdpi.com" in url and not url.rstrip("/").endswith("/pdf"):
        alts.append(url.rstrip("/") + "/pdf")
    return [a for a in dict.fromkeys(alts) if a and a != url]


def _fetch_one(client: httpx.Client, url: str, title: str, idx: int,
               out_dir: Path) -> tuple[str, int, list[str]]:
    """URL 하나를 받아 원문을 저장하고 (kind, chars, saved_files)를 반환. 실패 시 예외."""
    r = client.get(url)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    data = r.content

    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        pdf_path = out_dir / safe_name(idx, title, "pdf")
        pdf_path.write_bytes(data)
        text = extract_pdf(data)
        txt_path = out_dir / safe_name(idx, title, "txt")
        txt_path.write_text(text, encoding="utf-8")
        return "pdf", len(text), [pdf_path.name, txt_path.name]

    text = extract_html(data)
    if _is_blocked_page(text):
        # 차단 페이지를 '원문 확보'로 위장 저장하지 않는다 — failed 로 떨어져
        # 대체 URL 폴백·crawl_retry·wayback 재시도 대상이 된다(21차 문제3).
        raise RuntimeError("blocked page (captcha/bot-wall)")
    md_path = out_dir / safe_name(idx, title, "md")
    md_path.write_text(text, encoding="utf-8")
    return "html", len(text), [md_path.name]


def fetch_sources(json_path: str | Path, out_dir: str | Path | None = None,
                  incremental: bool = False,
                  only_urls: set[str] | None = None) -> Path:
    """raw JSON 의 각 URL 원문을 out_dir 에 저장한다.

    out_dir:      원문 저장 폴더. None 이면 `{json_path.stem}_sources`.
    incremental:  True 면 out_dir 의 기존 manifest 를 읽어 이미 처리된 URL 은
                  건너뛰고 신규 URL 만 추가(index·manifest 이어쓰기). 검증 루프에서
                  신규 문서만 기존 원문 폴더에 누적할 때 사용.
    only_urls:    지정하면 이 집합에 포함된 URL 만 저장(나머지는 manifest 에도
                  기록하지 않음 → crawl_retry/wayback 재시도 대상에서도 제외).
                  보고서 투입 후보만 받아 시간·대역폭을 아끼는 용도.
    """
    json_path = Path(json_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    docs = payload["documents"]

    if out_dir is None:
        out_dir = json_path.parent / f"{json_path.stem}_sources"
    else:
        out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    done_urls: set[str] = set()
    idx = 1
    mpath = out_dir / "manifest.json"
    if incremental and mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        done_urls = {m["url"] for m in manifest
                     if m.get("status") in ("ok", "thin", "pre_fetched")}
        idx = max((m.get("index", 0) for m in manifest), default=0) + 1

    skipped = 0
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT,
                      follow_redirects=True) as client:
        for d in docs:
            url = d["url"]
            if url in done_urls:
                continue
            if only_urls is not None and url not in only_urls:
                skipped += 1
                continue
            i = idx
            idx += 1
            rec: dict = {"index": i, "title": d["title"], "url": url,
                         "source": d["source"], "status": "ok"}

            # gas_safety URL은 odcloud.kr 가상 식별자 — content가 수집 시점에 확보됨
            if d.get("source") == "gas_safety":
                text = (d.get("content") or "").strip()
                md_path = out_dir / safe_name(i, d["title"], "md")
                md_path.write_text(text, encoding="utf-8")
                rec.update(kind="pre_fetched", chars=len(text), saved=[md_path.name])
                if len(text) < 200:
                    rec["status"] = "thin"
                print(f"  [{i:02d}] {rec['status']:<6} {rec.get('chars',0):>6} chars  "
                      f"{d['title'][:48]}")
                manifest.append(rec)
                continue

            try:
                kind, chars, saved = _fetch_one(client, url, d["title"], i, out_dir)
                rec.update(kind=kind, chars=chars, saved=saved)
                if chars < 200:
                    rec["status"] = "thin"  # 페이월/봇차단 의심
            except Exception as e:
                rec.update(status="failed", error=f"{type(e).__name__}: {str(e)[:120]}")

            # 대체 URL 폴백 — thin/failed 회수 + arXiv 초록→전문 PDF 업그레이드.
            # arXiv 초록 페이지는 1~2천 자라 'ok'로 통과하지만 전문 PDF 가 있으면
            # 그쪽이 수치 추출에 훨씬 유리하다.
            need_alt = rec["status"] in ("thin", "failed")
            upgrade = (d.get("source") == "arxiv" and rec["status"] == "ok"
                       and rec.get("chars", 0) < 3000)
            if need_alt or upgrade:
                floor = 200 if need_alt else rec.get("chars", 0)
                for alt in _alt_urls(d):
                    try:
                        kind, chars, saved = _fetch_one(
                            client, alt, d["title"], i, out_dir)
                    except Exception:
                        continue
                    if chars >= floor:
                        rec.update(status="ok", kind=kind, chars=chars,
                                   saved=saved, via=f"alt:{alt[:80]}")
                        rec.pop("error", None)
                        break

            print(f"  [{i:02d}] {rec['status']:<6} {rec.get('chars', 0):>6} chars  "
                  f"{d['title'][:48]}")
            manifest.append(rec)

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    _warn_repeated_sizes(manifest)
    ok = sum(1 for m in manifest if m["status"] == "ok")
    thin = sum(1 for m in manifest if m["status"] == "thin")
    fail = sum(1 for m in manifest if m["status"] == "failed")
    skip_note = f"  skipped={skipped}(선별 제외)" if skipped else ""
    print(f"\n[fetch] ok={ok}  thin={thin}  failed={fail}{skip_note}  -> {out_dir}")
    return out_dir


if __name__ == "__main__":
    default = ("output/raw/"
               "green-hydrogen-production-electrolysis-technology_2026-06-05.json")
    fetch_sources(sys.argv[1] if len(sys.argv) > 1 else default)
