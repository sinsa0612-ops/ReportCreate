"""한국 정부 공식 API 어댑터 — KOSIS(통계청) + 가스안전공사 수소 R&D + KIPRIS(특허) + 국회도서관.

단독 점검:
    .venv\\Scripts\\python.exe -m src.collectors.gov_kr
"""
import os
import re
import xml.etree.ElementTree as ET
import httpx

from src.models import Document
from src.processors.relevance import GENERIC_TERMS

KOSIS_SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"
KOSIS_STAT_HTML  = "https://kosis.kr/statHtml/statHtml.do"

GAS_BASE = "https://api.odcloud.kr/api"
# 연도 내림차순 — 최신 데이터 우선
GAS_ENDPOINTS: dict[int, str] = {
    2024: "3069894/v1/uddi:f1faf6a7-a6a1-4de8-a2e2-b030972763d9",
    2023: "3069894/v1/uddi:c647b493-0948-4a90-886e-dae2ad69fc63",
    2022: "3069894/v1/uddi:92ab5382-a51a-4603-9ecb-c36a06021af6",
    2021: "3069894/v1/uddi:a3ef2f33-e13d-4714-871c-83fb331064f3",
    2020: "3069894/v1/uddi:e16ca9a8-635e-45a5-a33f-8c0e6b2e6e25",
    2019: "3069894/v1/uddi:b44a7f87-73aa-4a5a-868f-9c2563b055e2_201909192039",
}

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEADERS = {"User-Agent": "Crawling-Research-Pipeline/1.0 (gov-kr collector)"}


def kosis_search(query: str, limit: int = 5) -> list[Document]:
    """KOSIS 통계목록 키워드 검색. source_type='stats'."""
    api_key = os.environ.get("KOSIS_API_KEY", "")
    if not api_key:
        raise ValueError("KOSIS_API_KEY not set")

    params = {
        "method":   "getList",
        "apiKey":   api_key,
        "vwCd":     "MT_ZTITLE",
        "searchNm": query,
        "format":   "json",
        "jsonVD":   "Y",
    }
    r = httpx.get(KOSIS_SEARCH_URL, params=params, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()

    raw = r.json()
    # KOSIS 응답 형식: 직접 배열 or {"err":..., "list":[...]}
    items: list[dict] = raw if isinstance(raw, list) else raw.get("list", [])

    docs: list[Document] = []
    for item in items[:limit]:
        org_id = item.get("ORG_ID", "")
        tbl_id = item.get("TBL_ID", "")
        title  = item.get("TBL_NM") or item.get("ITEM_NM") or ""
        org    = item.get("ORG_NM", "")
        url    = f"{KOSIS_STAT_HTML}?orgId={org_id}&tblId={tbl_id}"
        docs.append(Document(
            title=title,
            url=url,
            content=f"[KOSIS 통계표] {org} — {title}",
            source="kosis",
            source_type="stats",
            metadata={"org_id": org_id, "tbl_id": tbl_id, "org_nm": org},
        ))
    return docs


def gas_safety_search(query: str, limit: int = 5) -> list[Document]:
    """가스안전공사 수소 R&D 데이터셋 — 로컬 키워드 필터 후 반환. source_type='report'.

    API 자체에 키워드 검색이 없으므로 전체를 가져와 제목·분야에서 query 어절 매칭.
    매칭 어절은 강신호(도메인 핵심어)만 사용한다 — '기술'·'동향' 같은 범용어는
    거의 모든 R&D 과제와 매칭돼, 무관한 주제(예: 태양전지 보고서)에 수소 과제가
    S급으로 침투하는 노이즈가 됐다(평가 보고서 H1). 강신호가 없으면 빈 결과.
    최신 연도부터 시도하고, 충분한 결과가 나오면 이전 연도는 건너뜀.
    """
    api_key = os.environ.get("DATA_GO_KR_KEY", "")
    if not api_key:
        raise ValueError("DATA_GO_KR_KEY not set")

    terms = [t for t in (w.strip().lower() for w in query.split())
             if len(t) >= 2 and t not in GENERIC_TERMS]
    if not terms:
        return []

    docs: list[Document] = []
    for year, path in GAS_ENDPOINTS.items():
        params = {
            "page":       1,
            "perPage":    200,
            "serviceKey": api_key,
            "returnType": "JSON",
        }
        try:
            r = httpx.get(
                f"{GAS_BASE}/{path}",
                params=params, timeout=TIMEOUT, headers=HEADERS,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"  [gov_kr] 가스안전공사 {year} 실패: {type(exc).__name__}: {str(exc)[:60]}")
            continue

        for idx, item in enumerate(data.get("data", [])):
            # 필드명이 연도마다 다를 수 있어 값 전체를 text로 합산
            title  = str(item.get("사업명") or item.get("과제명") or item.get("title") or "")
            field  = str(item.get("분야") or item.get("기술분야") or item.get("세부분야") or "")
            org    = str(item.get("수행기관") or item.get("주관기관") or item.get("기관명") or "")
            period = str(item.get("사업기간") or item.get("연구기간") or "")
            combined = f"{title} {field} {org}".lower()

            if terms and not any(t in combined for t in terms):
                continue

            record_id = item.get("연번") or item.get("no") or str(idx + 1)
            doc_url = f"https://api.odcloud.kr/api/3069894/v1?year={year}&id={record_id}"

            content_lines = [f"[가스안전공사 수소 R&D {year}년]"]
            for k, v in item.items():
                if v and str(v).strip():
                    content_lines.append(f"{k}: {v}")

            docs.append(Document(
                title=title or f"수소 R&D 과제 ({year})",
                url=doc_url,
                content="\n".join(content_lines[:20]),
                source="gas_safety",
                source_type="report",
                published=f"{year}-09-30",
                metadata={"year": year, "org": org, "field": field, "period": period},
            ))
            if len(docs) >= limit:
                return docs

        if docs:
            break  # 최신 연도에서 충분히 나오면 이전 연도 불필요

    return docs


KIPRIS_SEARCH_URL = "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch"
KIPRIS_FOREIGN_URL = "http://plus.kipris.or.kr/openapi/rest/ForeignPatentAdvencedSearchService/advancedSearch"
KIPRIS_FOREIGN_COLLECTIONS = ["US", "EP", "WO"]  # 미국·유럽·PCT


def kipris_search(query: str, limit: int = 5) -> list[Document]:
    """KIPRIS Plus — 특허·실용신안 키워드 검색. source_type='patent'."""
    api_key = os.environ.get("KIPRIS_API_KEY", "")
    if not api_key:
        raise ValueError("KIPRIS_API_KEY not set")

    base_params = {
        "ServiceKey": api_key,
        "patent":     "true",
        "utility":    "true",
        "numOfRows":  limit,
        "pageNo":     1,
        "sortSpec":   "AD",
        "descSort":   "true",
    }

    seen_urls: set[str] = set()
    docs: list[Document] = []
    for search_params in [{"word": query}, {"inventionTitle": query}]:
        r = httpx.get(KIPRIS_SEARCH_URL, params={**base_params, **search_params},
                      timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()

        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            app_no    = (item.findtext("applicationNumber") or "").strip()
            title     = (item.findtext("inventionTitle") or "").strip()
            applicant = (item.findtext("applicantName") or "").strip()
            app_date  = (item.findtext("applicationDate") or "").strip()
            ipc       = (item.findtext("ipcNumber") or "").strip()
            abstract  = (item.findtext("astrtCont") or "").strip()
            reg_no    = (item.findtext("registerNumber") or "").strip()
            url = f"https://plus.kipris.or.kr/portal/data/service/SDOAPS/view.do?appl_no={app_no}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            content_parts = [f"[특허] {title}", f"출원인: {applicant}",
                             f"출원번호: {app_no}", f"출원일: {app_date}", f"IPC: {ipc}"]
            if abstract:
                content_parts.append(f"초록: {abstract[:500]}")
            docs.append(Document(
                title=title or f"특허 {app_no}",
                url=url,
                content="\n".join(content_parts),
                source="kipris",
                source_type="patent",
                published=f"{app_date[:4]}-{app_date[4:6]}-{app_date[6:8]}" if len(app_date) >= 8 else None,
                metadata={"app_no": app_no, "applicant": applicant, "ipc": ipc, "reg_no": reg_no},
            ))
    return docs


def kipris_foreign_search(query: str, limit: int = 5,
                          collections: list[str] = KIPRIS_FOREIGN_COLLECTIONS) -> list[Document]:
    """KIPRIS Plus — 해외특허(US/EP/PCT 등) 키워드 검색. source_type='patent'."""
    api_key = os.environ.get("KIPRIS_API_KEY", "")
    if not api_key:
        raise ValueError("KIPRIS_API_KEY not set")

    # collectionValues는 국가별로 파라미터를 반복 전달
    base_params: list[tuple[str, str]] = [
        ("accessKey",   api_key),
        ("currentPage", "1"),
        ("sortField",   "AD"),
        ("sortState",   "true"),
    ] + [("collectionValues", c) for c in collections]

    seen_urls: set[str] = set()
    docs: list[Document] = []
    for extra in [("free", query), ("inventionName", query)]:
        params = base_params + [extra]
        r = httpx.get(KIPRIS_FOREIGN_URL, params=params,
                      timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()

        root = ET.fromstring(r.content)
        count = 0
        for item in root.iter("searchResult"):
            if count >= limit:
                break
            ltrtno    = (item.findtext("ltrtno") or "").strip()
            title     = (item.findtext("inventionName") or "").strip()
            applicant = (item.findtext("applicant") or "").strip()
            app_date  = (item.findtext("applicationDate") or "").strip()
            app_no    = (item.findtext("applicationNo") or "").strip()
            ipc       = (item.findtext("ipc") or "").strip()
            open_no   = (item.findtext("openNumber") or "").strip()
            country   = (item.findtext("countryCode") or "").strip()
            url = f"https://plus.kipris.or.kr/portal/data/service/SDOAPS/view.do?ltrtNo={ltrtno}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            content = "\n".join(filter(None, [
                f"[해외특허/{country}] {title}",
                f"출원인: {applicant}",
                f"출원번호: {app_no}",
                f"출원일: {app_date}",
                f"IPC: {ipc}",
                f"문헌번호: {ltrtno}",
            ]))
            pub_date = None
            if len(app_date) >= 8:
                pub_date = f"{app_date[:4]}-{app_date[4:6]}-{app_date[6:8]}"
            docs.append(Document(
                title=title or f"해외특허 {ltrtno}",
                url=url,
                content=content,
                source="kipris_foreign",
                source_type="patent",
                published=pub_date,
                metadata={"ltrtno": ltrtno, "country": country, "app_no": app_no,
                          "open_no": open_no, "ipc": ipc},
            ))
            count += 1
    return docs


# ── 국회도서관 ──────────────────────────────────────────────────────────────

NALIB_SEARCH_URL  = "https://apis.data.go.kr/9720000/searchservice/basic"
NALIB_DETAIL_URL  = "https://apis.data.go.kr/9720000/detailinfoservice/detail"
NALIB_CATALOG_URL = "https://dl.nanet.go.kr/Search/DetailView.do"
NALIB_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _nalib_detail_fields(api_key: str, controlno: str) -> dict[str, str]:
    """detailinfoservice/detail 호출 → {name: value} dict. 실패 시 {}."""
    try:
        r = httpx.get(
            NALIB_DETAIL_URL,
            params={"serviceKey": api_key, "controlno": controlno},
            timeout=NALIB_TIMEOUT,
            headers=HEADERS,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        return {
            (it.findtext("name") or "").strip(): (it.findtext("value") or "").strip()
            for it in root.iter("item")
            if (it.findtext("name") or "").strip()
        }
    except Exception:
        return {}


def nalib_search(query: str, limit: int = 5) -> list[Document]:
    """국회도서관 자료검색 API — 도서·학위논문·기사·정부간행물. source_type='report'.

    searchservice/basic 으로 목록을 얻고, 각 건마다 detailinfoservice/detail 을
    호출해 원문 출처 URL(출처 필드)과 추가 메타데이터를 보강한다.
    """
    api_key = os.environ.get("DATA_GO_KR_KEY", "")
    if not api_key:
        raise ValueError("DATA_GO_KR_KEY not set")

    params = {
        "serviceKey": api_key,
        "pageno":       "1",
        "displaylines": str(min(limit, 10)),   # API 최대 10건/페이지
        "search":       f"전체,{query}",
    }
    r = httpx.get(NALIB_SEARCH_URL, params=params, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    if (root.findtext("header/resultCode") or "").strip() != "00":
        return []

    docs: list[Document] = []
    for record in root.findall("record"):
        # <record> 하위 <item><name>...<value>... 파싱
        basic: dict[str, str] = {
            (it.findtext("name") or "").strip(): (it.findtext("value") or "").strip()
            for it in record.findall("item")
            if (it.findtext("name") or "").strip()
        }

        controlno = basic.get("controlno", "").strip()
        if not controlno:
            continue

        # 상세정보 조회 — 출처 URL + 초록 등 추가 필드
        detail = _nalib_detail_fields(api_key, controlno)
        source_url = detail.pop("출처", "").strip()

        # 원문 출처가 있으면 사용, 없으면 디지털도서관 목록 URL
        url = source_url or f"{NALIB_CATALOG_URL}?cn={controlno}"

        # 모든 필드 합산 (basic이 기본, detail이 덮어씀)
        fields: dict[str, str] = {**basic, **detail}

        # 제목 파싱: "자료명 = 영문자료명 /저자명" 에서 "/" 앞까지
        raw_title = fields.get("자료명/저자사항", "")
        title = raw_title.split("/")[0].strip() or f"국회도서관 자료 {controlno}"

        # 발행연도 파싱 (출판사항: "서울:기관명,2024")
        pub_str = fields.get("출판사항", "")
        year_m = re.search(r"\b(19|20)\d{2}\b", pub_str)
        published = f"{year_m.group()}-01-01" if year_m else None

        # content 구성 — 핵심 필드 우선 + 나머지 보충
        priority = ["자료명/저자사항", "출판사항", "초록", "주제명", "분류기호", "DB", "공개"]
        used: set[str] = {"controlno"} | set(priority)
        content_parts = [f"[국회도서관] {title}"]
        for k in priority:
            v = fields.get(k, "")
            if v and k != "자료명/저자사항":
                content_parts.append(f"{k}: {v}")
        for k, v in fields.items():
            if k not in used and v:
                content_parts.append(f"{k}: {v}")

        docs.append(Document(
            title=title,
            url=url,
            content="\n".join(content_parts[:20]),
            source="nalib",
            source_type="report",
            published=published,
            metadata={"controlno": controlno},
        ))

    return docs


# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    q = "연료전지"
    print(f"=== KOSIS: '{q}' ===")
    try:
        for d in kosis_search(q, limit=5):
            print(" ", d.short(), "|", d.url)
    except Exception as e:
        print("  KOSIS 실패:", type(e).__name__, str(e)[:120])

    print(f"\n=== KIPRIS 국내특허: '{q}' ===")
    try:
        for d in kipris_search(q, limit=5):
            print(" ", d.short(), "|", d.url)
    except Exception as e:
        print("  KIPRIS 실패:", type(e).__name__, str(e)[:120])

    print(f"\n=== KIPRIS 해외특허(US/EP/WO): '{q}' ===")
    try:
        for d in kipris_foreign_search(q, limit=5):
            print(" ", d.short(), "|", d.metadata.get('country',''), "|", d.url)
    except Exception as e:
        print("  KIPRIS 해외 실패:", type(e).__name__, str(e)[:120])

    print(f"\n=== 가스안전공사 수소 R&D: '{q}' ===")
    try:
        results = gas_safety_search(q, limit=5)
        if results:
            print(f"  첫 번째 레코드 필드 확인:")
            for line in results[0].content.split("\n")[:8]:
                print("   ", line)
            print(f"  --- 전체 {len(results)}건 ---")
            for d in results:
                print(" ", d.short())
        else:
            print("  결과 없음 (키워드 미매칭 또는 API 오류)")
    except Exception as e:
        print("  가스안전공사 실패:", type(e).__name__, str(e)[:120])

    print(f"\n=== 국회도서관: '{q}' ===")
    try:
        results = nalib_search(q, limit=3)
        if results:
            for d in results:
                print(" ", d.short(), "|", d.url[:80])
                for line in d.content.split("\n")[1:4]:
                    print("     ", line)
        else:
            print("  결과 없음")
    except Exception as e:
        print("  국회도서관 실패:", type(e).__name__, str(e)[:120])
