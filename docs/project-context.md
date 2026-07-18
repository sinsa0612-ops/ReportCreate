# 프로젝트 컨텍스트 — 에너지 자료수집·보고서 자동생성 파이프라인

> 최종 업데이트: 2026-06-12 21차 구현 완료 (실전 검증에서 발견된 4개 문제의 해결방안 전부 구현, 테스트 209→225건 green. 상세는 7절 21차 항목) + 12절 서브에이전트 운영 검토 추가(결론: 현행 구조 유지)
> 이 문서는 BMad 에이전트의 `persistent_facts`로 자동 로드된다. 세션 시작 시 현황 파악용.
> **다음 세션 시작점은 7절 21차 잔여(실보고서 런 실측) 및 8절(남은 작업)을 먼저 보라.**

## 1. 목표

특정 주제에 대해 **심도 있고 신뢰성 높은 공인 자료를 자동 수집**하여 **보고서를 생성**하는 엔드투엔드 파이프라인.

- **도메인**: 과학기술, 특히 **에너지** 분야
- **출력물**: 자율 서술형 보고서 (챕터 구성, 출처 인용·신뢰도 등급 포함). 향후 사용자 지정 서식 적용 가능
- **수집 방식**: 온디맨드 (주제 입력 1회 → 결과까지 자동). 필요 시 반복 요청
- **운영 환경**: 이 워크스페이스(`D:\AI\Crawling`)에서 Claude Code로 제어

## 2. 핵심 설계 결정 (코드만으론 알 수 없는 배경)

- **방식 A (Claude Code 주도)**: 파이프라인의 "지능" 단계(검색 쿼리 생성, 보고서 작성)를 **런타임 LLM API 대신 Claude Code 세션이 직접 수행**한다. **중요: 사용자가 주제를 주면 CLAUDE.md의 5각도 템플릿으로 쿼리를 분해한 뒤 확인받고 실행한다.** 5각도(기술현황·시장경제성·국내정책·비교기술·최근실증) 균형 커버, 한국어 3~4개 + 영어 4~5개 = 총 7~9개 목표. 각도 불균형이 검증 단계에서 추가 쿼리를 급증시키는 근본 원인이므로 초기 커버리지가 핵심. 단일 한국어 주제를 그대로 넘기면 영어 특화 소스(Exa·arXiv·S2·EIA)에서 거의 0건이 된다. 따라서 `ANTHROPIC_API_KEY`는 현재 **의도적으로 제외**(`.env.example`에 주석 처리). 향후 무인 자동화(cron 등)로 전환 시에만 추가.
- **Python 3.12 고정**: 시스템 기본은 3.14지만 crawl4ai 등 일부 라이브러리가 3.14 미지원. `.venv`는 3.12.10 전용. (`py -3.12 -m venv .venv`)
- **검색 도구**: DuckDuckGo는 비공식·차단 이슈로 배제. **Tavily**(웹/보고서) + **Exa**(학술 논문) 조합 채택. 둘 다 무료 1,000회/월로 온디맨드에 충분.
- **API 우선, 크롤링 보조**: 공식 API(EIA·arXiv·Semantic Scholar 등)가 품질·안정·법적 리스크 모든 면에서 우월. crawl4ai는 API 없는 출처의 보완재.
- **원문 보존**: 검색 요약(≤1000자)과 별개로 각 자료의 **원문을 로컬에 저장**. PDF는 원본 보존, HTML은 본문 추출.

## 3. 아키텍처 / 파이프라인 흐름

```
주제 입력
   ↓  [Claude Code가 영어 검색 쿼리로 분해]
검색 수집   src/pipeline.py             → 10개 소스 언어 라우팅(한국어→국내 API, 영어→학술·해외)
   ↓                                 → URL 중복 제거 + 목록형 소스 상한 → output/raw/{slug}_{date}.json
원문 저장   src/collectors/fetch        → httpx + trafilatura(HTML) + pymupdf(PDF)
   │                                 → 보고서 투입 선별분만 fetch (select_for_report, 17차)
   ↓                                 → output/raw/{slug}_{date}_sources/ + manifest.json
봇차단 회수  src/collectors/crawl_retry  → crawl4ai 헤드리스 브라우저로 403 우회 (2차)
   ↓ (여전히 실패분)
아카이브폴백 src/collectors/wayback_retry → archive.org 스냅샷 회수 (Cloudflare 우회, 3차)
   ↓
보고서 생성  src/generators/staged_report.py (run_all)
   │  stage1 — 개요(Sonnet): 자료→챕터 배분(수치 미리보기 포함 목록, 18차) → output/drafts/{slug}/outline.json+md
   │  stage2 — 챕터별(Selective Opus): Ch1·5·6=Opus 4.8, Ch2·3·4·7=Sonnet 4.6. 이어쓰기 메모(<<<MEMO>>>, 700자 이내) 전달 → ch01~07.md
   │           종합 챕터(18차): Ch4=배분자료+이전 챕터 본문 주입(1~3장 수치와 일관된 비교표), Ch7=자료 없이 메모만 종합
   │           출력 검증(18차): '## {id}.' 제목 누락 시 1회 재시도, 재실패 시 제목 보정+경고
   │  stage3 — 조합(Sonnet): 핵심요약 마지막 작성 + 참고자료 생성 → output/reports/{slug}.md
   ↓
보고서 검증  src/validators/report_validator.py → agy 2단계(Pro 분석 + Flash 플랜)
            → 탈락 풀 승격(19차: 공백 쿼리로 미투입 자료 키워드 검색, 고등급+최신이면 웹 생략)
            → (남은 공백만) 웹 재수집 → 보고서 재생성
```

### 보고서 검증 단계 (2단계 모델 분업 고도화 — 2026-06-08 8차)

**핵심 설계: Pro=분석, Flash=액션 플랜으로 역할 분리.** YAML 스키마 강제를 폐기 → Pro 리뷰는 자유 서술로 품질↑, 파싱은 Flash JSON 한 번으로 단순화.

**동작 방식:**
1. `request_review()` — **agy `--model "Gemini 3.1 Pro (High)"`** 로 tech-writer(Paige) 관점 검토. **강점·약점·데이터공백을 자유 마크다운**으로 서술 → `output/reviews/{slug}-review-v{n}.md` (구조 강제 없음)
2. `plan_queries()` — **agy `--model "Gemini 3.5 flash"`** 로 위 Pro 리뷰를 읽고 **재수집 여부 판단 + 검색 쿼리 플랜 + 영향 챕터를 JSON**으로 산출 → `output/reviews/{slug}-queries-v{n}.json`. 서로 다른 공백이라도 같은 자료로 메울 수 있으면 묶어 중복 수집 감소. **기존 수집 문서 목록을 `[투입|등급]`/`[미투입|등급]` 라벨로 Flash 프롬프트에 주입(19차, `_existing_docs_block`)** — [투입]이 충분히 다루는 주제만 쿼리 제외, [미투입]으로 메울 수 있는 공백은 쿼리 작성(내부 승격 대상). `find_raw` 실패 시 `"(목록 로드 실패)"` 폴백. Flash 쿼리 생성 규칙: 영어 최소 2개(학술+웹 각도 핀포인트), medium 이하 우선순위 공백 제외. **15차 추가**: `query_plan[].affected_chapters`(데이터 공백이 보강할 챕터) + `revision_plan[{chapter,issue}]`(재수집 없이 고칠 논리 약점) 필드. **19차 추가**: `PlanResult.query_groups`(공백 그룹 원본 보존)
2.5 `promote_from_pool()` — **탈락 풀 승격(19차)**: 공백 쿼리로 미투입 자료를 키워드 검색해 그룹당 최대 4건 승격(무경쟁 투입 보장, `promoted_urls`), '고등급+최신' 충족 공백은 웹 재수집 생략. 상세는 7절 19차 항목.
3. `parse_plan()` — Flash JSON 파싱(`_extract_json`이 코드펜스·잡텍스트 robust 처리) → `PlanResult(should_recollect, queries, recollect_chapters, revision_chapters)` 반환(`_as_chapter_id`로 1~7 검증). 구버전 `parse_query_plan()`은 호환용 보존
4. `collect_and_merge()` — 쿼리로 `pipeline.collect()` 재수집 → 기존 raw JSON에 URL 중복 제거 후 병합 → `{slug}_{date}_v{n}.json` 저장(+`last_added_urls` 기록 — 부분 패치가 신규 자료를 대상 챕터에 주입하는 근거)
5. **부분 재생성** — `run_partial(slug, target_ids, notes)`(=`stage2_patch`+`stage3_assemble`): 데이터 공백(신규 자료)+논리 약점 영향 챕터만 재작성, 나머지 `ch0N.md` 보존. 폴백: 신규 자료 있는데 영향 챕터 미상 → `_full_regen()` 전체 재생성. 챕터 간 메모는 `ch{n}_memo.txt` 디스크 저장으로 부분 재작성 시에도 맥락 유지
6. `max_iterations`(기본 1)까지 반복, 보완 대상 챕터 없음(APPROVED 등) 시 조기 종료

**Flash 쿼리 플랜 JSON 포맷:**
```json
{
  "should_trigger": true,
  "verdict": "APPROVED | NEEDS_REVISION | NEEDS_RECOLLECTION",
  "query_plan": [
    {
      "rationale": "이 쿼리 묶음이 메우는 공백 설명",
      "priority": "critical | high | medium | low",
      "queries": ["한국어 1개+", "English query 1", "English query 2"]
    }
  ]
}
```
- `verdict == APPROVED` 또는 `should_trigger == false` → 재수집 없이 루프 종료
- `_extract_json`: ① 그대로 파싱 → ② ```json 코드펜스 제거 → ③ 첫 `{`~마지막 `}` 순으로 시도

**테스트:** `tests/validators/test_report_validator.py` 28건 green (전부 mock — agy/네트워크/claude 미호출). `_extract_json` robust 추출 5건, `parse_query_plan`(구버전) 6건, `collect_and_merge` 중복병합 4건, `plan_queries` 기존문서주입·폴백 2건, **15차 추가**: `_as_chapter_id` 4건 + `parse_plan` 영향챕터 6건. 부분 재생성은 `tests/generators/test_staged_report.py`(stage2_patch·메모영속·`_added_doc_indices`)에서 검증.

**검증 완료 사항 (이전 1단계 YAML 버전, SOFC 보고서 기준):** 재수집 78→149건(+71), SOEC 열화 메커니즘 섹션 보강 확인. (2단계 모델 분업 버전의 실보고서 검증은 다음 세션 과제)

**⚠️ 필수 사전조건 — agy 파일쓰기 권한 (2026-06-08 8차 해결):**
agy 는 기본 `toolPermission=request-review` 모드라, write_to_file 시 **대화형 승인 프롬프트**를 띄운다. 무인 서브프로세스(이 검증 루프)에서는 승인 주체가 없어 **무한 hang**(CPU 스핀, 0바이트 로그). 이게 문서 11④에서 "무인 자동화 부적합"이라 결론냈던 원인.
- **해결**: agy 설정 `~/.gemini/antigravity-cli/settings.json` 의 `permissions.allow` 에 아래 두 줄 추가 → write 도구가 해당 경로 내에서 프롬프트 없이 자동 승인:
  ```json
  "write_file(D:/AI/Crawling)",
  "write_file(D:/AI/Crawling/*)"
  ```
- **유효 권한 액션**(agy 바이너리 검증 정규식): `command | read_file | write_file | read_url | mcp | execute_url | unsandboxed`. 형식은 `write_file(경로)`. 모든 쓰기 도구(write_to_file·edit_file·create_file·replace_file_content)가 `write_file` 하나로 통합 매핑됨. 잘못된 액션명(예: `Edit(...)`)은 `ignoring invalid allow entry` 경고로 무시되니 주의.
- **검증 완료**: 권한 추가 전 Flash write_to_file hang → 추가 후 동일 작업 `exit 0` + 파일 정상 생성 확인. settings 로드 로그에 `ignoring invalid` 경고 없이 `allow=7` 로 로드 확인.
- 백업: `settings.json.bak-20260608`. 범위는 D:/AI/Crawling 로 한정(타 워크스페이스는 여전히 프롬프트).

**✅ 해결 — 재수집 시 원문 저장 (2026-06-08 8차, USER 발견 → 수정 완료):**
(이전 버그) `collect_and_merge()`가 검색(URL+스니펫)만 하고 원문 저장을 건너뛰어, 재수집 문서가 `_sources/`에 안 쌓이고 보고서 재생성 시 스니펫만 사용됨.
**수정 내용:**
1. `fetch.fetch_sources(json_path, out_dir=None, incremental=False)` — `incremental=True` 면 out_dir 기존 manifest 의 처리된 URL 을 skip 하고 신규 URL 만 append(index 이어쓰기).
2. `collect_and_merge()` — `_v{n}.json` 저장 후, **첫 수집의 base `{slug}_{date}_sources/` 폴더에 신규 문서만 incremental fetch**. 기존 URL 재크롤링 안 함(시간·봇차단 회피). gas_safety 등 정부 API 문서도 `pre_fetched` 로 manifest 등록 → 보고서가 인식.
3. `report._find_sources_dir()` — `_v{n}` 검증본은 전용 `_sources` 폴더가 없으면 `_v\d+` 를 떼어낸 base `_sources` 로 폴백.
**테스트:** fetch incremental 5건 + `_find_sources_dir` 폴백 4건 + collect_and_merge fetch 호출 1건 green. **잔여**: crawl_retry/wayback(봇차단·Cloudflare 회수)은 검증 루프에서 미적용(httpx fetch 만) — 신규 thin/failed 회수가 필요하면 추후 추가.

**✅ 동시 실행 가드 구현 완료** (2026-06-10 17차) — `src/locks.py::slug_lock`. `output/locks/{slug}.lock` pidfile. `main.run()`·`run_validation_loop()` 진입 시 획득, 죽은 PID(stale)는 자동 회수, 같은 프로세스 재진입(--validate 내부 호출)은 허용.

## 4. 디렉토리 구조

```
D:\AI\Crawling\
├─ .venv\                     # Python 3.12.10 전용 가상환경
├─ .env / .env.example        # API 키 (Tavily, Exa, EIA). .env는 .gitignore
├─ requirements.txt
├─ src\
│  ├─ models.py               # Document 표준 데이터 모델
│  ├─ pipeline.py             # 수집 오케스트레이터 (collect/save_raw/slugify)
│  ├─ collectors\
│  │  ├─ search.py            # tavily_search, exa_search
│  │  ├─ academic.py          # arXiv, Semantic Scholar (공식 학술 API)
│  │  ├─ fetch.py             # 원문 저장 (httpx/trafilatura/pymupdf) — 1차
│  │  ├─ crawl_retry.py       # crawl4ai 봇차단 재시도 — 2차
│  │  ├─ wayback.py           # archive.org 스냅샷 회수 함수
│  │  ├─ wayback_retry.py     # Wayback 폴백 재시도 — 3차
│  │  ├─ gov_kr.py            # 한국 정부 API (KOSIS + 가스안전공사 구현 완료 / 특허 진행중)
│  │  └─ library.py          # 마스터 라이브러리 — library/ 폴더 PDF·txt를 S급 자료로 로드 (쿼리기반 페이지 선발)
│  ├─ processors\
│  │  ├─ trust.py             # 신뢰도 등급 SS/S/AA/A/B/C (1년 이내→승급, URL연도추론, 인용수, 한국 R&D기관 S급)
│  │  └─ relevance.py         # 주제 관련성 필터 (강신호 2단계 + 약어 동의어 확장)
│  ├─ generators\
│  │  ├─ report.py           # 보고서 유틸리티 (build_prompt, find_raw, _extract_key_paragraphs 등) — staged_report가 사용
│  │  └─ staged_report.py    # 단계적 보고서생성 (개요→챕터별 Opus작성→조합). 챕터간 '이어쓰기 메모'로 일관성 유지 ★ 현재 기본
│  └─ validators\
│     └─ report_validator.py # agy 검증 루프 (request_review/parse_review/collect_and_merge/run_validation_loop)
├─ library\                   # 수동 관리 마스터 자료 (IEA·DNV 등 대형 PDF). 보고서 생성 시 항상 S급 포함
│  └─ .cache\                 # PDF 페이지 텍스트 추출 캐시 ({상대경로}.json, mtime 무효화) — 재파싱 회피
├─ tests\                     # unittest (무의존) — test_relevance, generators/test_report·test_staged_report
├─ output\
│  ├─ raw\                    # 수집 JSON + {slug}_sources\ 원문·manifest. 검증본은 {slug}_{date}_v{n}.json
│  ├─ drafts\{slug}\          # 단계적 생성 중간산출물 — outline.json·md, ch01~07.md
│  ├─ reports\                # 생성된 보고서(.md)
│  └─ reviews\                # agy 검토 파일 — {slug}-review-v{n}.md (YAML front matter + Markdown)
└─ docs\                      # 이 문서 등 프로젝트 지식
```

테스트 실행: `.venv\Scripts\python.exe -m unittest discover tests` (또는 `-m unittest tests.test_relevance`)

## 5. 기술 스택

| 레이어 | 도구 |
|--------|------|
| 검색 | tavily-python, exa-py |
| 학술 API | arXiv(httpx+xml.etree), Semantic Scholar(httpx, 인용수) |
| HTTP/원문 | httpx, trafilatura(HTML 본문), pymupdf(PDF) |
| 봇차단 우회 | crawl4ai + Playwright(chromium) |
| 설정 | python-dotenv |
| LLM 단계 | Claude Code 세션 (수집 쿼리) + `claude -p` 서브프로세스 (보고서 생성). 런타임 Anthropic API 미사용 |

출처 등급용 도메인 화이트리스트는 `src/processors/trust.py`의 `S_DOMAINS`/`A_DOMAINS`에 정의(IEA·IRENA·DOE·NREL·Nature·ScienceDirect·KIER·KEEI 등). (pipeline.py의 구 `TRUSTED_DOMAINS`는 미사용 중복이라 17차에서 제거)

## 6. 실행 방법 (프로젝트 루트에서)

```powershell
# 수집
.venv\Scripts\python.exe -m src.pipeline "query1" "query2" "query3"
# 원문 저장
.venv\Scripts\python.exe -m src.collectors.fetch output\raw\{파일}.json
# 봇차단 재시도
.venv\Scripts\python.exe -m src.collectors.crawl_retry output\raw\{파일}_sources
# 아카이브 폴백
.venv\Scripts\python.exe -m src.collectors.wayback_retry output\raw\{파일}_sources

# 통합 (위 4단계를 한 번에)
.venv\Scripts\python.exe -m src.main "주제 또는 쿼리" ["쿼리2" ...]
# 수집~보고서 원스텝 (--generate 로 마지막에 보고서까지 자동 생성)
.venv\Scripts\python.exe -m src.main "주제 또는 쿼리" ["쿼리2" ...] --generate

# 보고서만 따로 생성 — 단계적 생성 (개요→챕터(Opus)→조합) ★ 현재 기본
.venv\Scripts\python.exe -m src.generators.staged_report "<slug>" --auto        # 3단계 전체 자동 실행
.venv\Scripts\python.exe -m src.generators.staged_report "<slug>" --stage outline   # 1단계만
.venv\Scripts\python.exe -m src.generators.staged_report "<slug>" --stage chapters  # 2단계만
.venv\Scripts\python.exe -m src.generators.staged_report "<slug>" --stage assemble  # 3단계만

# 레거시 1-shot 생성 (유틸리티 — 현재 직접 호출 불필요)
.venv\Scripts\python.exe -m src.generators.report "<slug>" --dry-run  # 프롬프트 확인용(claude 미호출)

# 검증 루프 (agy 검토 → GAP 재수집 → 보고서 재생성, 최대 N회)
.venv\Scripts\python.exe -m src.validators.report_validator "<slug>"              # 기본 2회
.venv\Scripts\python.exe -m src.validators.report_validator "<slug>" --max-iter 1 # 1회만
.venv\Scripts\python.exe -m src.validators.report_validator "<slug>" --skip-review # 기존 리뷰 재사용

# 수집~보고서~검증 원스텝
.venv\Scripts\python.exe -m src.main "주제 또는 쿼리" ["쿼리2" ...] --validate
```

## 7. 현재 구현 상태 (2026-06-08 7차)

- ✅ 검색 레이어 (Tavily + Exa) — 동작 검증
- ✅ 수집 오케스트레이터 + JSON 저장
- ✅ 원문 저장 (httpx/trafilatura/pymupdf)
- ✅ crawl4ai 봇차단 회수
- ✅ **통합 파이프라인 `src/main.py`** — 주제 한 줄로 수집→원문→봇차단회수 자동 (검증: 페로브스카이트 10/10)
- ✅ **신뢰도 스코어링 `src/processors/trust.py`** — 출처 등급 S/A/B/C + 최신성 가점 자동 부여, 점수순 정렬 (pipeline 통합). 부수: `src/__init__.py`에서 콘솔 UTF-8 재구성
- ✅ **공식 학술 API `src/collectors/academic.py`** — arXiv + Semantic Scholar(인용수→신뢰도 가점) 직접 연동. `_safe` 래퍼로 한 소스가 rate-limit/실패해도 전체 수집 계속 (현재 4소스: Tavily·Exa·arXiv·S2)
- ✅ **관련성 필터 `src/processors/relevance.py`** — 쿼리 핵심용어 매칭으로 주제 무관 자료 제외("출처 신뢰도 ≠ 내용 관련성" 해결). 검증: 그린수소 43건 중 물리/AI 노이즈 8건 제거. pipeline 통합
- ✅ **원문 회수 3차 폴백 `src/collectors/wayback.py` + `wayback_retry.py`** — 봇차단/Cloudflare 자료를 archive.org 아카이브 사본에서 회수. `main.py` 4단계(fetch→crawl_retry→wayback) 통합. 검증: 그린수소 38→41/43, **IEA(S급) Cloudflare 우회 회수**
- ✅ 첫 엔드투엔드 검증 완료: **"그린수소 생산 기술 현황"** 보고서 1건 생성, 원문 24/30 확보(8MB)
- 시범 산출물: `output/reports/green-hydrogen-production-2026-06-05.md`
- ✅ **한국어 지원 + 한국 R&D 기관 S급** (2026-06-06) — `relevance.py` query_terms가 한글 어절(`[가-힣]{2,}`) 추출, `trust.py` S_DOMAINS에 KETEP·KISTEP·KIPRIS·NTIS·에너지공단·data.go.kr 등 추가. 검증: 한국어 쿼리 "분산형 연료전지 신뢰성"에서 에너지경제연구원 PDF가 S급 분류. **목적: KETEP 신재생에너지 R&D 사업계획서 사전 자료조사**(파이프라인은 하나 — 한국어+영어 쿼리 병행으로 한국 정책자료 + 글로벌 학술 동시 수집)
- ⚠️ 콘솔 인코딩: PowerShell은 cp949라 한글 깨짐 → 실행 시 `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $env:PYTHONUTF8=1` 프리픽스 필요
- ✅ **KETEP 사전조사 실전 검증** (2026-06-06) — "분산형 연료전지 신뢰성 평가" 한국어3+영어3 쿼리 병행 → 61건 수집·원문 59건. 한국 정책(에경연·KIER·정부)+글로벌 학술(DOE·NREL·PEMFC 리뷰) 동시 확보. 산출물: `output/reports/분산형연료전지-신뢰성평가-사전조사-2026-06-06.md`. **결론: 파이프라인은 하나, 쿼리 이중언어로 KETEP 사전조사 완결**
- ✅ **한국 정부 API 통합 완료** (2026-06-06 2차) — `src/collectors/gov_kr.py` 신규 생성. `kosis_search`(통계청 통계목록) + `gas_safety_search`(가스안전공사 수소 R&D 2019~2024) 구현. `pipeline.collect()` 통합 완료. 신뢰도: KOSIS→S[90], 가스안전공사→S[94](odcloud.kr S_DOMAINS 등록). 관련성 필터: 정부 API 소스(kosis·gas_safety)에 강신호 면제 적용. 검증: "연료전지"+"수소에너지" 쿼리 → KOSIS·가스안전공사 총 6건 정상 수집
- ✅ **관련성 필터 노이즈 개선 완료** (2026-06-06 2차) — `relevance.py` 강신호(도메인 핵심어) 2단계 필터 + 약어 동의어 확장. **첫 프로젝트 테스트 디렉토리 `tests/` 도입**(unittest, 무의존). 상세는 8절 로드맵 6번 참조
- ✅ **보고서 자동생성 구현 완료** (2026-06-07) — `src/generators/report.py`. 수집 JSON(`output/raw/{slug}_*.json` 최신 자동선택) → `build_prompt`(핵심필드 title/url/content 1500자/trust_grade만 추려 토큰절약) → `claude -p` 서브프로세스 → `output/reports/{slug}.md`. **ANTHROPIC_API_KEY 불필요**(현재 Claude Code 세션 인증 재사용). TDD 8테스트 green(`tests/generators/test_report.py`, 전부 mock). 에러처리: claude부재→RuntimeError, raw부재→FileNotFoundError, dry_run→subprocess미호출, exit≠0/빈출력→RuntimeError, timeout전파. **실전 검증**: "연료전지" 보고서 생성 — S급(가스안전공사 5과제) 표 우선·출처 URL 각주·자료없는 부분 "추가조사필요" 표시·IEA수치는 2차인용 명시. 산출물 `output/reports/연료전지.md`
- ✅ **보고서 품질 대폭 개선** (2026-06-07 2차) — `src/generators/report.py` 전면 개편. **핵심 변경 3가지**:
  1. **원문 직접 연결**: `_find_sources_dir()` + `_build_fulltext_index()` 로 `_sources/manifest.json`을 읽어 수집된 원문 파일 경로를 문서 인덱스로 매핑. `_doc_block()`이 원문 파일 있으면 우선 읽고(최대 4000자), 없으면 Tavily 스니펫 폴백(1000자). 이전엔 스니펫만 사용해 수치 누락 심각.
  2. **정량 수치 문장 우선 추출**: `_QUANT_PATTERN` 정규식 (숫자+단위·%·KRW·LCOE·CAPEX·TRL·N₂O·NOx·NH₃슬립·Well-to-Wake 등) 으로 원문에서 수치 포함 문장 우선 수집 후 나머지로 채움.
  3. **범용 구조화 프롬프트**: 6개 필수 섹션(핵심요약/배경/경쟁기술비교/기술경로/쟁점리스크/시사점) 강제. 비교표를 LLM이 주제별 자동 결정(이전엔 암모니아·연료전지 도메인 하드코딩). 수치 없는 셀 "데이터 없음" 의무화. 인용 형식 `[등급 | 출처명]` 강제.
  - **신규 상수**: `FULLTEXT_CHARS=4000`, `SNIPPET_CHARS=1000`, `CLAUDE_TIMEOUT=600`
  - **검증 완료**: ① 암모니아 선박 추진(`친환경-선박용-암모니아-추진-시스템.md`, 21KB) — 원문 34건 활용, N₂O 681,000명 조기사망·LCOE·NOx 수치 포함. ② SMR(`소형-모듈-원자로-smr-기술-현황.md`, 25KB) — 완전히 다른 도메인에서도 LCOE·건설단가·설비이용률 비교표 자동 생성. **범용성 확인**.
  - **⚠️ 알려진 한계**: `--generate` 플래그로 수집~보고서 원스텝 실행 시, 수집 문서가 많으면 claude -p 호출이 600s 타임아웃에 걸릴 수 있음. 해결책: 수집(`src.main`) 후 보고서(`src.generators.report`)를 별도로 실행.
  - **API 발견 사항 (다음 세션 주의)**: `filter_relevant()`는 `(kept, dropped)` 튜플 반환(len()=2 오해 주의). 신뢰도 함수명은 `grade_document(doc: Document)` — `score_document` 아님.
- ✅ **보고서 검증·보완 루프 구현 완료** (2026-06-08) — `src/validators/report_validator.py` 신규. Antigravity(agy) CLI를 검토자로 활용. `agy --print "@report.md" + write_to_file` 패턴으로 권한 프롬프트 없이 동작 확인. YAML front matter 파싱 → priority 임계값 이상 GAP만 추출 → `pipeline.collect()` 재수집 → 기존 raw JSON 병합(`_v{n}.json`) → `claude -p` 보고서 재생성. `main.py --validate` 플래그로 원스텝 통합. **검증**: SOFC 보고서 78→149건(+71), SOEC 메커니즘 GAP 보강 확인. **⚠️ 잔존 취약점**: 동시 실행 가드 미구현(pidfile 필요), agy 파일 미생성 시 상위 예외 미처리.
- ✅ **보고서 프롬프트 구조 개선** (2026-06-07 4차) — `src/generators/report.py` `PROMPT_TEMPLATE` 개편. **핵심 변경 4가지**:
  1. **챕터 구조 재편(7장)**: 기존 6섹션(배경→비교→경로→쟁점→실증·이해관계자→시사점) → 7섹션(기술개요→시장맥락→**기술경로**→**정량비교**→쟁점→플레이어·정책→시사점). 기술 경로를 먼저 설명한 뒤 비교표를 제시하는 논리적 순서로 교정. 실증현황·이해관계자 혼합 챕터를 3장(실증 포함)·6장(플레이어)으로 분리.
  2. **비교표 공백 유형 구분**: `데이터 없음`(수치 미발견, 추후 보완 대상)과 `해당 없음(N/A, 사유)`(구조적 적용 불가)을 명시적으로 구분. 로드맵 11번 검증 단계의 파싱 전제조건 충족.
  3. **권고안 문체 규칙**: 기존 "수치 근거 → 따라서 ~해야 한다" 반복 패턴 금지 → `[문제 인식] → [구체 행동] → [기대 효과]` 3문장 구조로 교체. 단기(~2년)/중장기(3년~) 구분 추가.
  4. **출처 [C]등급 명시**: 참고 자료 섹션에 `[C] 참고용(위키백과·나무위키 등)` 4번째 등급 추가. 위키류를 [B]급과 혼용하던 문제 해소.
  5. **핵심요약 한 줄 결론 위치**: 수치 나열 후 결론이 아닌, **한 줄 결론을 맨 앞**에 배치하도록 지시 변경.

- ✅ **단락 기반 청킹 + 마스터 라이브러리 + 단계적 보고서생성** (2026-06-08 7차) — 보고서 품질 3종 개선:
  1. **단락 기반 청킹** `report.py::_extract_key_paragraphs` — 기존 문장 단위 추출(수치 문장만 골라 순서 뒤섞임)을 단락 단위로 교체. 수치 많은 단락 우선 선발 + **앞뒤 단락을 문맥으로 포함** → 원문 순서로 재조합(논리 흐름 보존). 단락 1개 이하면 문장 폴백.
  2. **마스터 라이브러리** `src/collectors/library.py` + `library/` 폴더 — IEA·DNV 등 수동 PDF를 항상 S급(95점)으로 보고서에 포함. **IEA는 Cloudflare 차단으로 자동수집 불가 → 수동 PDF 관리가 정답**(A 도메인검색 방식은 스니펫만 얻어 폐기 결정). PDF 청킹 2단계: ①쿼리 키워드 점수로 관련 페이지 선발(앞 5p 항상+상위 20p, 앞뒤 1p 문맥) ②`_extract_key_paragraphs`로 8000자 압축. 현재 보유: `library/IEA/EnergyTechnologyPerspectives2026.pdf`(33MB), `library/DNV/DNV_ETO_Hydrogen Forecast_2026.pdf`(46MB).
  3. **단계적 보고서생성** `src/generators/staged_report.py` — 1-shot 대신 3단계. **stage1**(개요, Sonnet): 7챕터 골격 고정 + 자료를 `relevant_doc_indices`로 챕터 배분 → `output/drafts/{slug}/outline.json`+`outline.md`(사용자 검토 체크포인트). **stage2**(챕터별, **Opus 4.8**): 배분 자료만 집중 투입, Paige 글쓰기원칙 프롬프트 내장, 챕터 완성 후 `<<<MEMO>>>` 이어쓰기 메모(핵심주장·용어·수치·실마리·문체) 추출 → 다음 챕터 프롬프트에 주입해 일관성 유지 → `ch01~07.md`. **stage3**(조합, Sonnet): 챕터 읽고 핵심요약을 **마지막에** 작성(먼저 쓰면 환각) + 참고자료 프로그래밍 생성 → `output/reports/{slug}.md`. 실행: `python -m src.generators.staged_report "<slug>" --stage outline|chapters|assemble` 또는 `--auto`. **claude -p --model claude-opus-4-8 동작 검증 완료**. 테스트: `test_staged_report.py` 12건 green. **협업**: Paige(Tech Writer)가 단계 설계·챕터별 독자질문·요약 타이밍 자문, Amelia 구현.
  - **알려진 한계**: ① stage2는 챕터를 순차 호출(N회 claude -p)이라 1-shot보다 느림·토큰多. 품질 우선 선택. ② outline의 `relevant_doc_indices`가 비면 상위 8건으로 폴백. ③ Selective Opus 적용(10차) — Ch1·5·6만 Opus, 나머지 Sonnet으로 약 40% 비용 절감.
- ✅ **쿼리 5각도 다각화 + 검증 루프 개선** (2026-06-09 9차) — 3가지 개선:
  1. **CLAUDE.md 5각도 쿼리 템플릿**: 기술현황·시장경제성·국내정책·비교기술·최근실증 5각도를 각도별 한국어 1 + 영어 1 균형 커버(총 7~9개). 각도 불균형이 검증 단계 추가 쿼리 급증의 근본 원인임을 확인 → 초기 커버리지로 사전 차단.
  2. **기존 수집 문서 목록 Flash 주입**: `plan_queries()`가 `find_raw()`로 raw JSON을 읽어 수집 문서 제목·등급(최대 120건)을 Flash 프롬프트에 주입 → 이미 있는 주제 재쿼리 방지. 실패 시 폴백 처리.
  3. **Flash 쿼리 규칙 강화**: 영어 최소 2개(학술+웹), medium 이하 우선순위 공백 제외.
  - 테스트: `test_report_validator.py` 14 → 17건 green (`PlanQueriesExistingDocsTest` 2건 + `CollectAndMergeTest` 1건 신규).

- ✅ **신뢰도 등급 SS/AA 최신성 승급** (2026-06-09 12차) — 발행일 기준 1년 이내 자료를 S→SS(95점), A→AA(80점)로 승급. 발행일 미기재 시 `infer_year_from_url()`로 URL에서 연도 추론(arXiv ID 우선, 경로 내 2020~2029 연도 패턴 폴백). 스니펫·제목 텍스트 연도는 오탐 위험으로 제외(인용 연도 혼재). 참고문헌 정렬도 SS→S→AA→A→B→C 순으로 갱신. **실전 검증**: 247건 기준 SS 16건·AA 31건 식별, URL 추론으로 추가 10건 승급(RSC 2026 논문·SKKU 2026 페이지 등).

- ✅ **파이프라인 평가 후속 일괄 수정 (17차)** (2026-06-10) — `docs/pipeline-evaluation-2026-06-10.md`의 권고 전체 구현. **배경**: 수집 250~320건 중 보고서 투입은 trust_score 상위 40건뿐이었고, 그 40건이 정부 API 메타데이터 스텁으로 도배(실측: 수소 런 top40의 37건이 500자 미만, 학술·웹 자료 0건). 검증 루프 재수집 자료는 목록 끝에 append되어 `[:MAX_DOCS]` 절단으로 **0건 반영**(158건 중 0).
  1. **C1 재수집 미반영 버그 수정** — `staged_report.py::_selected_docs` 신설: `validation_added_urls`(collect_and_merge가 누적 기록) 문서를 기본 선별과 분리해 **항상 뒤에 append**(상한 `ADDED_DOCS_CAP=12`). 기본 선별이 재수집 후에도 동일해 outline 위치 인덱스 안정 유지.
  2. **C2 선별 로직** — `src/processors/select.py::select_for_report`: 선별점수 = trust_score + 내용충실도(-8~+8, 본문 길이) + 목록형 소스 그룹 쿼터(특허 5·통계 3·목록 5). 등급 체계는 불변, 투입 순위만 별도 계산. `staged_report._prepare_docs`·`report.build_prompt` 적용.
  3. **M1 원문 매핑 URL 키 전환** — `_build_fulltext_index`가 `{index: path}` → `{url: path}`. `_doc_block(doc, fulltext_index)` 시그니처 변경. 위치↔manifest index 결합(재정렬·incremental 재시도 시 어긋남)을 제거.
  4. **H1 gas_safety 강신호 매칭** — 매칭 어절에서 `GENERIC_TERMS`('기술'·'동향' 등) 제외, 강신호 없으면 빈 반환. 태양전지 보고서에 수소 R&D 9건이 S급 침투하던 노이즈 차단.
  5. **H2 EIA 정리** — 수소→petroleum 매핑 제거, 무매칭 시 total-energy 기본값 제거(빈 반환), `hydro`가 `hydrogen`에 부분 매칭되던 오류도 수정(hydropower로 교체).
  6. **H3 fetch 선별화** — `fetch_sources(only_urls=...)` 추가. `main.py`는 선별 문서만 fetch(이전: 300건 전량 113~216MB), `collect_and_merge`는 이번 회차 신규 URL만 fetch. 선별 제외 문서는 manifest에 없어 crawl_retry/wayback 대상에서도 제외.
  7. **H4 언어 라우팅** — `collect()`: 한국어 쿼리 → Tavily+KOSIS+가스안전+KIPRIS국내+국회도서관, 영어 쿼리 → Tavily+Exa+arXiv+S2+EIA+KIPRIS해외. API 호출 ~40% 절감.
  8. **H5 런 단위 상한** — `pipeline.RUN_CAPS`: gas_safety/kipris/kipris_foreign/nalib 8, kosis/eia 5. URL 중복 제거 후 적용.
  9. **M2 stage3 경량화** — 참고문헌은 `list_library()`(내용 미파싱) + 선별 메타데이터로 구성, 라이브러리 PDF 캐시 재로드 제거.
  10. **M4 동시 실행 가드** — `src/locks.py::slug_lock` (3절 참조). **M3** Exa `max_characters=1000` 서버측 절단. **M5** pipeline `TRUSTED_DOMAINS`/`trusted_only` 제거(미사용 중복).
  - **테스트**: 134 → **170건 green**. 신규: `tests/processors/test_select.py` 8건, `tests/test_pipeline.py` 7건(라우팅·상한), `tests/test_locks.py` 6건, `tests/collectors/test_gov_kr.py` 5건, `_selected_docs` 회귀 3건, fetch only_urls 3건, validator 누적 URL 2건. EIA 테스트 2건은 새 동작으로 갱신.
  - **잔여**: 실보고서로 선별 품질·절감률 실측 미완(다음 런에서 확인). ~~FULLTEXT_CHARS 축소(14-③)는 C2로 진짜 원문이 투입되기 시작한 후 재검토.~~ → 18차에서 2500자로 적용.

- ✅ **보고서 품질·토큰 일괄 개선 (18차)** (2026-06-10) — 최신 수소 보고서 실측(선별 52건 중 원문 38건, stage2 자료블록 186k자≈62k토큰) + agy 리뷰 2건 공통 지적사항 기반. **사용자 결정: 7챕터 골격 유지(챕터 추가 거절), 보고서 분량 축소 거절** — 용도별(KETEP 등) 보고서 구조화는 추후 별도 작업.
  1. **A1 챕터 출력 검증·재시도** — `staged_report.py::_valid_chapter` + `_write_one_chapter` 재시도 루프. **배경(실증 버그)**: 수소 보고서 ch04 드래프트가 문장 중간부터 시작(`## 4.` 제목 소실) → stage3가 무검사 조합 → 최종 보고서 목차에서 4장 누락 → agy 검증 1회전을 구조 결함에 낭비. 본문이 `## {id}.` 로 시작하지 않으면 동일 프롬프트 1회 재시도, 재실패 시 제목 보정 저장+경고(파이프라인 중단 없음).
  2. **A2/B2 종합 챕터 전환** — `PREV_BODY_CHAPTERS={4}`·`MEMO_ONLY_CHAPTERS={7}`. **Ch4(정량 비교)**: 배분 자료에 더해 이전 챕터 본문(`_prev_bodies_block`, 상한 40k자, 초과 시 최근 챕터 우선 보존)을 주입 — 1~3장이 인용한 수치와 일관된 비교표 작성(리뷰 반복 지적: "3장 수치가 4장 표에 없음" 해소). **Ch7(시사점)**: 자료 블록 없이 이전 챕터 메모만으로 작성(~27k자→~5k자, 권고안이 본문 결론에서 도출되도록). outline 배분 규칙에 "Ch7 자료 배분 금지 + Ch4 정량 자료 우선" 추가. stage2_patch도 동일 로직(Ch7은 신규 자료 주입 primary 후보에서 제외).
  3. **A3 특허·통계 스텁 규칙 + 쿼터 누적** — CHAPTER_PROMPT에 "제목만 있는 특허·통계는 동향 집계로만 언급" 규칙. `select.py::count_groups` + `select_for_report(initial_counts=)` 로 검증 재수집 배치가 기본 선별의 그룹 쿼터를 이어받음(**버그 수정**: 배치별 쿼터 리셋으로 특허가 5+5=10건 투입되던 문제 — 수소 보고서 실측).
  4. **A4 '데이터 없음' 결론 금지 규칙** — CHAPTER_PROMPT에 "데이터 없음으로 표기한 지표를 결론의 근거로 사용하지 않는다" 추가(리뷰 2건 공통 지적 패턴 선제 차단).
  5. **A5 수치 미리보기** — `report.py::extract_quant_sentences`(프로그래밍 추출, LLM 비용 0) → `_doc_listing(all_docs, blocks)` 가 문서당 수치 문장 1~2개를 '수치:' 줄로 표시 → stage1 배분 정확도 개선(LCOE 보유 자료가 Ch2·4로 가도록).
  6. **A6 원문 확보 가점** — `selection_score(doc, fulltext_urls)` +6점. 스니펫은 짧지만 원문 PDF가 확보된 문서의 탈락 방지. `_selected_docs`·`build_prompt` 가 manifest 기반 URL 집합 전달(fetch 후 고정이라 결정성 유지. main.py의 fetch 대상 선별은 의도적으로 가점 없음 — fetch 전이므로).
  7. **B3 FULLTEXT_CHARS 4000→2500** — 수치 단락 우선 추출(`_extract_key_paragraphs`) 전제로 자료 블록 ~35% 절감. 다음 실보고서 런에서 리뷰 평가로 품질 영향 확인 예정.
  - **토큰 효과 추정**: stage2 입력 기준 Ch7 자료 제거(–27k자) + FULLTEXT 축소(–35% of fulltext 블록) — Ch4는 이전 본문 주입으로 소폭 증가(품질 우선 의도적 선택). 합산 stage2 입력 약 25~35% 절감 추정, 실측은 다음 런.
  - **테스트**: 170 → **188건 green**. 신규: select 가점·쿼터누적 5건, `_valid_chapter`·재시도 4건, 종합 챕터 2건, 수치 미리보기 5건(extract 3 + listing 2), 프롬프트 규칙 2건, `_selected_docs` 쿼터 누적 회귀 1건. 기존 1건(`primary_target`)은 출력 검증 통과하도록 mock 제목 수정.
  - **잔여**: 실보고서 1건으로 품질(리뷰 verdict)·절감률 실측. ch04 앞부분 소실의 근본 원인(claude -p stdout 소실 의심)은 재발 시 로그로 추적.

- ✅ **선별·검증 루프 구조 개선 (19차)** (2026-06-10) — 핵심 통찰(USER): "화이트리스트 등급은 신뢰성 보장이지 필요성 보장이 아니다" + "수집 305건 중 253건이 영구 미사용". 실측 근거: 수소 보고서 리뷰가 '가장 치명적 공백'이라 지적한 LCOE·시장규모·CAGR 키워드를 가진 **탈락 문서 33건** 발견 — 자료는 있었는데 B급이라 투입 경쟁에서 탈락, 보고서엔 '데이터 없음'. 게다가 검증 루프가 이를 구조적으로 구제 불가(Flash에 "이미 수집됨=재수집 불필요"로 전달 + 재수집 결과가 기존 URL 중복으로 폐기). **설계 원칙: '무엇을 쓸지'는 필요가 정하고, '누구 말을 믿을지'는 등급이 정한다.**
  1. **각도 균형 선별** — `select.py::ANGLE_KEYWORDS`(market·policy·compare·demo 4각도, 기술현황은 자연 커버라 제외) + `doc_angles()` + `_balance_angles()`. 점수순 선발 후 각도별 최소 `ANGLE_MIN=3`건 미달 시 탈락분에서 해당 각도 최고점 문서를 끌어올리고, '수호자 아닌'(모든 소속 각도가 최소선 초과 또는 무각도) 최저점 문서를 내림. 그룹 쿼터 연동, 결정적. `angle_min=0`으로 비활성(검증 추가 배치는 공백 겨냥 수집이라 비활성). 영어 키워드는 \b 경계 매칭('usd'≠'used'), 한국어는 부분 매칭.
  2. **탈락 풀 승격** — `report_validator.py::promote_from_pool`. Flash 공백 쿼리(그룹)마다 ① `_gap_terms`로 판별 용어 추출(STOPWORDS·GENERIC_TERMS 제외) ② 풀(수집됐으나 미투입)을 키워드 검색, 2개 이상 일치 문서를 (일치수, 선별점수)순 상위 `PROMOTE_PER_GAP=4`건 승격 ③ 승격분에 '고등급(SS/S/AA/A) **그리고** 최신(1년 내, published→URL 연도 추론 폴백)' 자료가 있으면 그 공백의 웹 쿼리 생략, 아니면(B/C급 임시 충당) 웹 재수집 병행 — **최신성 우려(USER 지적) 대응: 낮은 품질 승격이 더 좋은 신규 자료 발견을 차단하지 않음**. 승격 문서는 `promoted_urls`(누적)·`last_promoted_urls`(회차) 기록 + 원문 incremental fetch. LLM·네트워크 비용 0.
  3. **승격 무경쟁 투입 보장** — `staged_report._selected_docs`: promoted_urls 문서는 기본 선별·추가 배치와 분리된 **제3 차선**으로 맨 뒤 무조건 append(쿼터·점수 재경쟁 없음 — "경쟁은 승격 시점에 한 번만"). 재생성 때 순위에 밀려 조용히 탈락하는 일 차단(USER 지적 반영). `_added_doc_indices`는 last_promoted_urls 도 챕터 주입 대상에 포함.
  4. **투입/미투입 라벨** — `_existing_docs_block()`: Flash에 주는 수집 문서 목록을 `[투입|등급]`(보고서 전달됨) / `[미투입|등급]`(승격 가능, 선별점수순) 으로 구분. `_QUERY_PROMPT` 규칙 변경: "[미투입] 자료로 메울 수 있는 공백도 쿼리를 작성하라(시스템이 내부 승격 우선)" — 기존 "수집된 주제는 재수집 불필요" 규칙이 미투입 자료의 공백을 영구 방치하던 문제 해소. 토큰 증가는 라벨 몇백 자 수준.
  5. **PlanResult.query_groups** — parse_plan이 공백 그룹 원본(queries·affected_chapters·rationale) 보존 — 그룹 단위 승격·선별적 웹 재수집의 근거.
  6. **`_base_sources_dir` 버그 수정(잠복)** — 검증본 `_v{n}.json`이 검증 실행일 날짜로 생성되면 base `_sources` 폴더 stem 계산이 첫 수집일과 어긋나 잘못된 폴더에 fetch하던 문제. 승격이 같은 회차에 v{n}을 먼저 쓰면서 현실화 → 글롭 폴백으로 기존 base 폴더를 정확히 탐색.
  - **검증 루프 흐름(개정)**: Pro 리뷰 → Flash 플랜(라벨 목록) → **풀 승격** → (못 메운 공백만) 웹 재수집 → 부분 재생성. 사용자 합의 3중 방어: 각도 균형(사전) → 풀 승격(사후) → 웹 재수집(최후).
  - **테스트**: 188 → **209건 green**. 신규: doc_angles 4건 + 각도균형 6건, promote_from_pool 5건, _existing_docs_block 2건, query_groups 1건, promoted 무경쟁 투입 2건, _added_doc_indices 승격 포함 1건.
  - **잔여**: 실보고서 런으로 각도 균형·승격 효과 실측(리뷰 verdict 변화, 웹 재수집 절감). ANGLE_KEYWORDS·ANGLE_MIN 은 실측 후 조정 여지.

- ✅ **등급·토큰·품질 일괄 수정 (20차)** (2026-06-12) — 18-19차 실측에서 메커니즘은 정상이었으나 **원문 확보율 68%·등급 오승급(14건+)·Ch4 토큰 낭비(40k자)**가 보고서 품질 상한을 제한한다는 진단에 따른 일괄 수정. USER 질문("등급 매기는 로직에 문제 없냐")이 계기.
  1. **등급 연도버그 수정** (`trust.py`) — `infer_year_from_url`/`_resolve_year`에 **연도 범위 검증(1900~올해+1)** 추가. MDPI ISSN(예: 2071-1050)·로드맵 목표연도(net-zero-2050)가 발행연도로 오인되어 AA/SS로 오승급되던 버그(실측 14건+) 및 EIA `published="2121-12"` 쓰레기 값이 SS|100으로 오승급되던 버그를 모두 차단. `report_validator._doc_year`도 동일 검증 적용(검증 루프의 `_is_strong()` 최신성 판정에도 영향).
  2. **등급 도메인 보강** (`trust.py`) — S_DOMAINS에 DOE 산하 9곳(netl 격인 osti.gov·ornl.gov 등)+EU/영국 정부, A_DOMAINS에 해외 출판사 7곳+국내 학회지 5곳(journal.hydrogen.or.kr·koreascience.kr 등) 추가. 신규 **C_DOMAINS**(LinkedIn·네이버블로그·나무위키 등 15곳+위키백과 → C 강등)로 B 버킷에서 소셜·블로그가 학술지와 동점이던 문제 해소. 신규 **INDUSTRY_DOMAINS**(BNEF·DNV·h2news 등 14곳, B 등급 유지하되 +8점)로 "LCOE 자료가 전부 B라 비교에서 밀림" 문제 보정.
  3. **Ch4 수치 다이제스트** (`staged_report.py`) — `PREV_BODY_CHAPTERS`(Ch4)에 주입하던 이전 챕터 본문 전체(상한 40,000자)를 `_quant_digest`(제목·표·수치 문장만 추출, 상한 8,000자 `PREV_DIGEST_CAP`)로 교체. 실측 10,365자→7,106자(-31%, 표·수치는 보존). 보고서당 약 2~3만 토큰 절감.
  4. **개요 재사용 + 정합성 가드** — `_doc_fingerprint(all_docs)`(URL 목록 sha1)를 outline.json에 `doc_count`+`doc_fingerprint`로 저장. stage1은 자료 구성이 동일하면 LLM 호출 없이 기존 개요 재사용(중단 후 재실행 시 토큰·시간 절약). stage2/stage2_patch는 `_check_outline_alignment`로 자료 변경 시 즉시 중단. 자료 변경 시 `_archive_stale_drafts`가 기존 챕터를 `_stale_{타임스탬프}/`로 보관 후 전체 재생성(삭제 없음 — `_full_regen`의 수동 삭제 로직 제거). 개요 JSON 파싱 1회 재시도 + `relevant_doc_indices` 범위검증. stage3는 동일 챕터번호 중복 파일 발견 시 중단.
  5. **fetch 대체 URL 폴백** (`fetch.py`) — thin/failed 문서는 S2 `openAccessPdf` → arXiv `pdf_url` → MDPI `/pdf` 순으로 재시도(API 비용 0). arXiv는 ok여도 3,000자 미만 초록이면 전문 PDF로 업그레이드. `crawl_retry`/`wayback_retry`는 `kind=="pre_fetched"`(가스안전공사 가상 식별자) 대상 제외. 원문 확보율 68%→80%대 기대.
  6. **검증 재수집 fetch 선별** (`report_validator.collect_and_merge`) — 추가 수집분 전량(실측 159건) 대신 `_selected_docs`와 교집합(실측 12건)만 fetch.
  7. **수치 인용 감사 신설** (`src/validators/citation_audit.py`, 신규 파일) — 보고서의 `[등급|출처]` 인용 직전 80자(룩백, 한국어 서술어 개입 대응)에서 수치를 추출해 로컬 원문 코퍼스(raw JSON+`_sources`+`library`) 전체에서 검색, 미발견 수치를 Pro 리뷰 프롬프트(`{audit_section}`)에 첨부. **LLM 비용 0**으로 환각 수치 검증 공백을 메움. 실측: SOFC 보고서 60/60 인용 확인, 합성 가짜수치 2/4 정확 검출.
  8. **기타** — `pipeline.py`: `_dedup_by_title`(제목 정규화 기준 중복 제거, 실측 ~3%) + `_report_source_counts`(Tavily/학술/국내정부 소스 0건 경고). `main.py`: 미등록 `--` 플래그 시 즉시 에러 종료(`KNOWN_FLAGS`). `report.py`: arXiv를 "arxiv (preprint·동료심사 전)"로 라벨링. `report_validator.parse_plan`: 재수집 쿼리에 한국어/영어 한쪽이 없으면 소스 라우팅 경고.
  - **검증**: 10개 파일 `py_compile`+임포트 스모크 통과. 연도추론 5케이스·등급 8케이스·다이제스트 압축률·지문 결정성·제목중복·대체URL 추출·EIA 쓰레기연도 회귀·인용감사(실데이터+합성) 전부 수동 테스트 통과.
  - **사용 시 주의**: 기존 슬러그(지문 없는 구버전 outline)는 다음 stage1 실행 시 재사용 안 되고 챕터가 `_stale_*/`로 보관 후 전체 재생성됨 — 의도된 동작.
  - **잔여**: 실보고서 런으로 원문 확보율·토큰 절감·등급 분포 변화 실측 미완(다음 런에서 확인).

- ✅ **수소 저장 보고서 실전 검증 — 4가지 문제 발견 + 구현 완료 (21차)** (2026-06-12) — "수소 저장 기술 개발 현황" 8쿼리(5각도 균형) → `--generate --validate --max-iter 1` 풀 런 실측. **결론: 토큰 절약 메커니즘(선별 fetch, Selective Opus, 다이제스트, 메모 요약, 풀 승격)은 전부 의도대로 작동.** 그러나 "검증이 지적한 문제를 재생성이 실제로 해소했는가"를 확인하는 마지막 고리가 비어 있음을 실측으로 확인. **→ 같은 날 4건 모두 구현 완료** (아래 각 문제의 해결방안이 그대로 구현됨, 구현 요약은 이 항목 끝 참조).

  **문제 1 (심각) — 인용 감사가 지적한 환각 수치가 최종 보고서에 잔존.**
  - **증상**: `citation_audit`(20차 신설)이 "CAPEX EUR ~244.5M [AA|MDPI 2025]" 수치를 원문에서 찾을 수 없다고 Pro 리뷰에 첨부 → Pro 리뷰가 "환각 의심"으로 정확히 재지적(Ch.3/Ch.4) → 그러나 최종 보고서에는 Ch4 표 1곳 + Ch7(시사점) 2곳, 총 3곳에 동일 수치가 그대로 남음.
  - **원인 ①**: Ch7은 `MEMO_ONLY_CHAPTERS`로 이번 회차 재작성 대상(`target_ids`={1..6})에서 빠져 `stage2_patch`가 "유지"로 보존했는데, Ch7이 바로 그 환각 수치를 인용 중이었음.
  - **원인 ②**: Ch4는 재작성됐지만, `_prev_bodies_block`(Ch4용 수치 다이제스트, `PREV_BODY_CHAPTERS={4}`)이 **재작성 전 옛 챕터 본문**에서 추출되어 환각 수치를 그대로 다시 주입 → Ch4 재작성 결과에도 잔존.
  - **해결방안**:
    1. `citation_audit.run_audit()`이 반환하는 "미확인 수치 목록"(현재는 Pro 리뷰 프롬프트 첨부용으로만 쓰임)을 `report_validator._validation_loop_inner`에서 별도로 보존하고, 해당 수치를 인용한 챕터 ID를 `_locate_chapter_by_quote()`(신규, 각 `ch0N.md`를 grep)로 찾아 `chapter_notes`에 `[환각 의심 수치 제거]` 패치 노트로 강제 추가한다 — Pro/Flash의 자유 서술 누락과 무관하게 항상 동작하는 "결정적(deterministic) 가드".
    2. 환각 수치를 인용한 챕터가 `MEMO_ONLY_CHAPTERS`(Ch7)에 속하면, `stage2_patch`의 "유지" 분기에서도 **그 챕터만** 예외적으로 `target_ids`에 추가한다(자료 없이 메모만으로 재작성하므로 비용 미미).
    3. `_prev_bodies_block`이 호출되는 시점을 **재작성 완료 후**로 늦추거나, 환각 수치가 확정된 경우 `_quant_digest` 추출 전에 해당 패턴을 제거하는 전처리를 추가한다.

  **문제 2 (중간) — 신규 자료 32건의 단일 챕터 집중 주입으로 인한 자료 오배분 + 요약-본문 모순.**
  - **증상**: Flash 플랜의 공백 챕터가 [1,2,3,4,5,6] 전체였는데, `stage2_patch`의 "신규 자료는 기존 관련자료가 가장 많은 1개 챕터(primary)에만 주입" 규칙(16차 G)에 따라 신규 32건이 전부 Ch3에 들어감. 그 결과 ① Ch3 자료가 11→43건으로 폭증(프롬프트 비대화, Sonnet 챕터인데도 토큰 급증), ② DOE TRL/MRL 분석 문서 등 TRL 공백을 메울 자료가 정작 Ch1(TRL 공백 지적 챕터)에 전달되지 않아 Ch1의 TRL 표가 재작성 후에도 전부 "데이터 없음", ③ 핵심 요약은 (메모 기반이라 일부 구버전 잔재로) "TRL 8-9" 같은 수치를 인용해 **Ch1 본문과 핵심 요약이 정면 모순**.
  - **원인**: 16차에서 "여러 챕터에 동일 자료 중복 전송 방지"를 위해 단일 챕터 주입으로 단순화했는데, 이번처럼 공백 챕터가 6개나 되고 신규 자료의 주제가 다양하면 단일 챕터로는 분배가 부정확해짐. `PlanResult.query_groups`(19차, `affected_chapters` 포함)는 이미 그룹별 영향 챕터 정보를 갖고 있는데 `stage2_patch`가 이를 쓰지 않고 `_added_doc_indices`(전체 신규 URL 평탄화)만 사용.
  - **해결방안**:
    1. `_added_doc_indices`를 그룹 단위로 쪼갠 `_added_doc_indices_by_group(slug, all_docs, query_groups)`(신규)로 교체 — 각 query_group의 `queries`로 수집된 신규 문서만 그 그룹의 `affected_chapters`에 매핑한다(그룹별 신규 URL은 `collect_and_merge`가 쿼리 그룹 단위로 재수집을 호출하도록 함께 조정 필요 — 현재는 전체 `remaining` 쿼리를 한 번에 `collect()` 호출하므로, 호출을 그룹별로 분리하거나 결과 문서를 그룹별 판별 용어(`_gap_terms`)로 사후 분류).
    2. 한 챕터에 주입되는 신규 자료 수에 상한(`PATCH_INJECT_CAP`, 예: 6~8건)을 둬 Ch3처럼 11→43건 폭증을 방지하고, 초과분은 차순위 관련 챕터로 분산.
    3. 핵심 요약(stage3)의 메모 기반 입력에 "각 챕터 메모와 본문 제목 수치가 일치하는지" 체크는 과설계이므로, 대신 **Ch1이 재작성될 때 핵심 요약도 함께 재생성**(현재 `run_partial`은 항상 `stage3_assemble` 호출하므로 이미 그렇게 동작 — 다만 입력 메모 자체가 "데이터 없음"이라면 요약도 "데이터 없음"으로 일치해야 함. 문제 2-1·2-2가 해결되면 메모-요약 불일치는 자동 해소될 가능성이 높음. 별도 조치 불필요, 1·2 해결 후 재검증으로 확인).

  **문제 3 (중간) — CAPTCHA/차단 페이지가 "원문 확보(ok)"로 위장 통과, 토큰 낭비 + 확보율 통계 왜곡.**
  - **증상**: crawl4ai가 ScienceDirect 11건을 "OK, 2519 chars"로 기록했으나 실제 내용은 "Are you a robot? / Please confirm you are a human by completing the captcha challenge"로 동일한 2,519자 캡차 페이지였음. 이 텍스트가 `_doc_block`을 통해 챕터 프롬프트에 "원문"으로 주입되어 토큰을 낭비하고, `[fetch] ok=59 thin=7 failed=6` 같은 확보율 통계도 실제보다 부풀려짐(실질 ok는 약 48건).
  - **원인**: `crawl_retry.py`/`fetch.py`의 성공 판정이 "HTTP 200 + 비어있지 않은 본문"만 보고, 본문 내용이 차단 페이지인지 검사하지 않음.
  - **해결방안**:
    1. `fetch.py`(또는 공유 유틸)에 `_is_blocked_page(text: str) -> bool` 추가 — "Are you a robot", "completing the captcha", "Reference number:", "we have detected unusual traffic" 등 차단 페이지 표준 문구를 정규식으로 매칭. `crawl_retry.py`의 성공 판정 직후 이 검사를 통과하면 `status="failed"`로 강제 전환(`kind`는 유지해 재시도 이력 보존).
    2. 동일 바이트 수(예: 2,519자)가 같은 manifest 내에서 N건(예: 3건) 이상 반복되면 의심 신호로 로그 경고 — 캡차 페이지 같은 정형 템플릿의 특징이므로 패턴 미일치 차단 문구를 놓쳐도 2차 방어선이 됨.
    3. `failed`로 재분류된 항목은 `wayback_retry`의 재시도 대상에 자동 포함되도록(이미 `status != "ok"`이면 대상이므로 1번 수정만으로 충족).

  **문제 4 (경미) — 무관 문서가 관련성 필터·선별을 통과해 보고서 참고자료에 혼입 + Semantic Scholar 429 누락.**
  - **증상**: "METHODS OF DOSING AND ADMINISTRATION OF ENGINEERED ISLET CELLS"(줄기세포 치료) · "COMPOSITIONS AND METHODS OF ENHANCING TUMOR REACTIVE LYMPHOCYTES"(면역항암) — 둘 다 KIPRIS 해외 특허, SS등급(최신성 가점) — 그리고 "Citation-Enforced RAG for Fiscal Document Intelligence"(세금 컴플라이언스 RAG 논문)가 참고자료 목록에 포함됨. 또한 1차 수집·재수집 양쪽에서 `semantic_scholar_search`가 429(rate limit)로 전량 실패(경고만 출력, 파이프라인은 계속 진행).
  - **원인 ①**: KIPRIS 해외 특허는 `relevance.py`의 GOV_SOURCES 강신호 면제 대상이라 본문 없이도(제목만으로) 관련성 검사를 약하게 통과. 제목에 "hydrogen"이 없는데도 특허 검색 API가 광범위 매칭으로 반환한 결과로 추정.
  - **원인 ②**: `semantic_scholar_search` 호출 간 지연이 없어 같은 런 내 5개 영어 쿼리가 연속 호출되며 429를 유발(`_safe`가 예외를 삼켜 경고만 남기고 0건으로 계속).
  - **해결방안**:
    1. `relevance.py`: GOV_SOURCES 면제 대상 중 `kipris_foreign`/`kipris`(특허, 본문 없음)는 면제에서 제외하거나, 제목에 한해 강신호 매칭을 **필수**로 적용(가스안전공사 등 본문 있는 정부 소스는 면제 유지).
    2. `academic.py::semantic_scholar_search` 호출부(또는 `pipeline.collect()` 루프)에 쿼리 간 `time.sleep(1.0)` 등 최소 지연 추가. 또는 429 수신 시 1회 재시도(짧은 backoff)로 완전 누락보다 부분 수확 시도.
    3. (선택) `_report_source_counts`의 "학술 소스 0건 경고"가 이미 출력되므로, 이 경고가 뜨면 `collect()`가 S2만 재시도하는 보조 경로를 추가하는 것은 과설계 — 1·2로 충분.

  - **공통 패턴**: 문제 1·2는 "검증 루프가 지적/계획한 것을 재생성이 실제로 반영했는지 사후 확인하는 단계 부재"로 요약된다. 향후 검증 루프 재구성 시 `_validation_loop_inner` 마지막에 "재생성 후 재검증(lightweight)" 단계 — 예: citation_audit을 재실행해 미확인 수치 건수가 줄었는지만 확인 — 를 추가하는 것을 다음 큰 개선(22차 후보)으로 고려할 것.

  **✅ 구현 완료 (2026-06-12, 같은 날 후속 세션)** — 4건 전부 위 해결방안대로 구현:
  - **문제 1**: `citation_audit.audit_findings()`(구조화 결과 — chapter·ctx·missing 토큰) 신설, `audit_report`는 이를 마크다운으로 변환. `report_validator._hallucination_guard()`(신규) — 감사 결과를 드래프트 `ch0N.md` grep(`_locate_chapters_by_numbers`)으로 챕터에 매핑해 `[환각 의심 수치 제거]` 노트를 `chapter_notes`에 **강제 추가**(Flash 누락과 무관한 결정적 가드, Ch7 메모 전용 챕터도 target에 포함됨). 미확인 토큰 집합은 `run_partial(excluded_numbers=)`→`stage2_patch`→`_prev_bodies_block`→`_quant_digest` 로 전달되어 Ch4 다이제스트에서 환각 수치 문장·표 행을 제거(쉼표 정규화 매칭).
  - **문제 2**: `staged_report._added_doc_chapter_map()`(신규) — 신규·승격 문서를 `gap_terms` 일치 최다 쿼리 그룹에 배정 후 그 그룹의 `affected_chapters`(∩target, Ch7 제외)에 라운드로빈 분산. 챕터당 `PATCH_INJECT_CAP=8` 상한, 무매칭·초과분은 기존 primary 폴백(관련자료 많은 챕터 순 분산). `run_partial`/`stage2_patch`에 `query_groups=` 파라미터 추가, 검증 루프가 `plan.query_groups` 전달. `_gap_terms`/`_term_hits`는 임포트 순환 회피를 위해 `relevance.py`의 `gap_terms`/`term_hits`로 이동(validator는 기존 이름으로 재노출).
  - **문제 3**: `fetch._is_blocked_page()`(차단 문구 정규식, 앞 4,000자) — `_fetch_one`의 HTML 추출 직후 검사해 차단 페이지면 RuntimeError→failed(대체URL·crawl_retry·wayback 재시도 경로 유지). `crawl_retry`도 저장 전 동일 검사(BLOCK→failed, wayback 대상화). `_warn_repeated_sizes()` — 동일 글자 수 ok 문서 3건+ 반복 시 경고(fetch·crawl_retry 요약부에서 호출, 2차 방어선).
  - **문제 4**: `relevance.PATENT_SOURCES`={kipris, kipris_foreign} — GOV 면제 대신 **제목 강신호 ≥1 필수**(강신호 없는 쿼리는 제목 총매칭 폴백, 본문 있는 정부 소스는 면제 유지). `academic.semantic_scholar_search` 호출 간 최소 1.5초 간격(`_S2_MIN_INTERVAL`, 기존 `_get` 429 backoff와 별개의 1차 방어).
  - **테스트**: 209 → **225건 green**. 신규 16건: 환각 가드 3(챕터 매핑·Ch7 포착·실패 무해), 다이제스트 제외 3, 그룹 분산 맵 3(라우팅·Ch7 제외·상한), 차단 페이지 5(검출 3·동일크기 경고 2), 특허 제목 강신호 2. 갱신 4건: run_partial mock 시그니처, Ch4 "수치 다이제스트" 문구(20차 미반영 stale), main 미등록 플래그 에러종료(20차 stale), 승격 테스트 날짜 하드코딩 제거.
  - **잔여**: 실보고서 풀 런으로 효과 실측(환각 수치 잔존 0건·신규 자료 분산 배분·캡차 ok 위장 차단·무관 특허 혼입 0건 확인). "재생성 후 재검증(lightweight)" 단계는 22차 후보로 유지.

## 7-A. 인-세션 보고서 작성 가이드 (claude -p 없이 직접 수행할 때)

Claude Code 세션에서 사람이 직접 보고서를 작성하는 경우(`report.py` 서브프로세스 미사용), 아래 순서를 반드시 따를 것.

### 소스 파일 읽기 순서

1. **`_sources/manifest.json`** 읽기 → `status: "ok"` 항목의 `saved` 파일 목록 확보
2. **`status: "ok"` 소스 파일들** 읽기 (`.md` / `.txt`)
3. **⚠️ 반드시 추가**: raw JSON(`output/raw/{slug}_*.json`)에서 `source == "gas_safety"` 문서의 `content` 필드도 읽을 것

### gas_safety 누락 방지 (핵심)

`gas_safety` 문서는 수집 시 `Document.content`에 과제명·기관·기간 등 메타데이터가 이미 저장된다.
그러나 `fetch.py` 수정 전 실행된 manifest에는 `status: "failed"`로 기록되어 소스 파일이 없다.

- **fetch.py 수정 후 실행된 manifest**: gas_safety가 `status: "ok"`, `kind: "pre_fetched"`로 저장됨 → 소스 파일 읽기로 자동 포함
- **기존 manifest(수정 전 실행)**: `status: "failed"` → 소스 파일 없음 → raw JSON에서 직접 읽어야 함

```python
# 인-세션에서 gas_safety 내용을 raw JSON에서 꺼내는 패턴
payload = json.loads(Path("output/raw/{slug}_*.json").read_text())
gas_safety_docs = [d for d in payload["documents"] if d["source"] == "gas_safety"]
# 각 d["content"]를 보고서 챕터 6(주요 플레이어·정책)에 한국 정부 R&D 과제로 반영
```

### 노이즈 필터링 (인-세션)

`relevance.py`가 걸러내지 못한 노이즈(arXiv 다이아몬드 X-ray광학, AI리스크 분류체계, 비디오 이해 등)를 제목·소스 확인 후 수동 제외. 원문 내용이 주제와 무관하면 건너뜀.

### 데이터 소스 교체 우선순위 원칙 (보고서 챕터 보강 시 필수)

챕터를 신규 수집 자료로 보강할 때 기존 인용을 교체하려면 **두 차원 모두** 우월해야 한다.

| 비교 조건 | 판단 |
|-----------|------|
| 등급 동일 → 발행연도가 더 최신 | 교체 가능 |
| 발행연도 동일 → 등급이 더 높음 (SS>S>AA>A>B>C) | 교체 가능 |
| 등급 동일 + 연도 동일 | 교체 불가, 보충만 |
| 어느 한 차원이라도 기존보다 열위 | 교체 불가, 보충만 |

**규칙 요약:**
1. **최신성 우선 (동급 비교)** — 같은 신뢰도 등급이면 발행연도가 더 최신인 자료를 사용한다.
2. **등급 우선 (동년 비교)** — 같은 발행연도라면 더 높은 등급(SS>S>AA>A)의 자료를 사용한다.
3. **기존 데이터 보존** — 신규 데이터는 기존 인용을 *보충(supplement)*한다. 두 차원 모두 우월할 때만 교체한다. 등급이나 최신성 중 하나라도 기존이 우위면 기존 인용을 유지하고 신규를 부가 정보로 추가한다.
4. **신규 정보는 무조건 추가** — 기존 자료에 없던 수치·사실(예: 새 기술의 LCA 범위, 신규 정책 예산)은 기존 인용 등급과 무관하게 별도 항목으로 추가한다.

> **실수 사례 (2026-06-09)**: Ch2.2에서 `[SS | IEA Breakthrough Agenda 2025]`(SS, 2025)를 `[S | IRENA 2025]`(S, 2025)로 교체 → 등급 하락, 교체 불가. 올바른 처치: SS 인용 유지 + IRENA S를 배터리·91% 수치에 한해 *추가*. Ch2.3에서 `[S | DNV ETO 2026]`(S, 2026)을 IEA 2021 시나리오(AA, 2025 논문에 인용)로 교체 → 연도 하락, 교체 불가. 올바른 처치: DNV 2026 유지 + IEA NZE를 "(참고)" 행으로 *추가*.

---

## 8. 남은 작업 (로드맵)

1. ~~파이프라인 통합~~ ✅ **완료** (2026-06-05, `src/main.py`)
2. ~~신뢰도 스코어링(B단계)~~ ✅ **완료** (2026-06-05, `src/processors/trust.py`)
3. ~~공식 API 어댑터~~ ✅ **완료** (2026-06-05, `src/collectors/academic.py` — arXiv·Semantic Scholar). EIA는 키 발급 대기
4. ~~원문 회수 보강~~ ✅ **완료** (2026-06-05) — Wayback 3차 폴백으로 IEA(Cloudflare)·greenskills 회수, 그린수소 41/43. 잔여 2건(eh2·PMC)은 아카이브 스냅샷도 없어 종료(B급)
5. ~~**한국 정부 API 연동**~~ ✅ **완료** (2026-06-06 2차) — KOSIS + 가스안전공사 수소 R&D pipeline 통합. 특허(KIPRIS)는 미완. **상세는 10절 참조**
6. ~~**관련성 필터 노이즈 개선**~~ ✅ **완료** (2026-06-06 2차) — `relevance.py` 2단계 필터. ① `GENERIC_TERMS` blocklist로 쿼리 용어를 범용어/강신호로 분리, **강신호(도메인 핵심어) ≥1 필수** → LLM평가·지진·줄기세포·전력망툴 등 범용어만 맞춘 노이즈 제외. ② `SYNONYMS` 약어 확장(PEMFC·SOFC 등)으로 약어만 쓴 연료전지 논문 복구. ③ gov 소스는 강신호 면제(서버사이드 필터). **검증**: 분산형연료전지 61→53(노이즈 8 제외, PEMFC 복구), 그린수소 물리/AI 노이즈 8건 정확 제거(회귀 없음), 페로브스카이트 과잉제외 0. 테스트: `tests/test_relevance.py` 10건 green (unittest). 잔여: bare "FC" 약어 자료 1건은 스크랩 본문 부실로 미복구(모호성 때문에 의도적 보류)
7. ~~**보고서 생성 자동화**~~ ✅ **완료** (2026-06-07) — `src/generators/report.py`. `claude -p` 서브프로세스 방식으로 Anthropic API 키 없이 구현(세션 인증 재사용). 7절 참조. **`main.py --generate` 플래그로 수집~보고서 원스텝 연결 완료** — `parse_args`로 플래그 분리(테스트 `tests/test_main.py` 4건 green), `run(..., generate=True)` 시 마지막에 `generate_report(slug)` 호출
8. ~~**보고서 품질 개선 — 원문 기반 수치 추출 + 범용 템플릿**~~ ✅ **완료** (2026-06-07 2차) — 7절 상세 참조. 두 이종 도메인(암모니아 선박, SMR) 검증 완료.
9. ~~**EIA 공식 API**~~ ✅ **완료** (2026-06-07 3차) — `src/collectors/eia.py` 신규. `match_routes()`로 한국어·영어 쿼리 → EIA v2 경로 매핑, `_route_meta()`로 설명·날짜범위·하위 데이터셋 메타 취득. `pipeline.collect()` 통합. `relevance.py` GOV_SOURCES에 `"eia"` 추가(강신호 면제). 신뢰도: `eia.gov` S_DOMAINS 기존 등록 → S급 자동. 테스트 19건 green (`tests/collectors/test_eia.py`). **주의**: EIA v2에 `renewable-energy` 최상위 경로 없음 → 빈 메타 폴백(URL 정상, fetch.py 가능).
10. ~~**특허(KIPRIS) 어댑터**~~ ✅ **완료** (2026-06-08) — `gov_kr.py::kipris_search`(국내) + `kipris_foreign_search`(해외) 구현.
    - **국내**: `kipo-api/kipi/.../getAdvancedSearch`, `ServiceKey`, `word`+`inventionTitle` 병행
    - **해외**: `openapi/rest/ForeignPatentAdvencedSearchService/advancedSearch`, `accessKey`, `free`+`inventionName` 병행, 국가=US/EP/WO(PCT), `collectionValues` 반복 파라미터
    - 공통: URL 중복 제거, `pipeline.collect()` 통합, GOV_SOURCES 면제, kipris.or.kr S급 기존 등록
    - 잔여: `word`/`free` 검색의 노이즈 향후 relevance 필터 조정으로 정리
11. ~~**보고서 검증·보완 단계**~~ ✅ **완료** (2026-06-08 8차, 9차 개선) — `src/validators/report_validator.py`. **2단계 모델 분업으로 고도화**: agy Pro(`Gemini 3.1 Pro (High)`, tech-writer 관점 강점·약점·데이터공백 자유 서술) → agy Flash(`Gemini 3.5 flash`, 재수집 쿼리 플랜 JSON) → 재수집 → 보고서 재생성. YAML 스키마 강제 폐기. agy 권한 hang은 settings.json `write_file()` 화이트리스트로 해결. 테스트 17건 green(9차에서 14→17). 상세는 3절 참조. ~~잔여: 동시 실행 pidfile 가드~~ → ✅ 17차 `src/locks.py` 완료.

12. ~~**재수집 원문 저장 버그 수정**~~ ✅ **완료** (2026-06-08 8차, USER 발견) — `fetch_sources` incremental 모드 + `collect_and_merge`가 신규 문서만 base `_sources` 폴더에 누적 + `_find_sources_dir` _v 폴백. 상세는 3절 "✅ 해결" 참조. 테스트 10건 green. 잔여: 봇차단 회수(crawl_retry/wayback) 미적용.

13. ~~**마스터 라이브러리 파싱 캐시**~~ ✅ **완료** (2026-06-08 8차, USER 제안) — `library.py` 리팩토링: PDF→페이지 텍스트 추출(`_extract_pages`, 쿼리 무관)과 페이지 선발(`_select_pages_text`, 쿼리 의존)을 분리. `_cached_pages()`가 페이지 텍스트를 `library/.cache/{상대경로}.json`에 1회 저장(PDF mtime 비교로 무효화, 손상 캐시 자동 재추출). 캐시 키는 library 기준 상대경로라 다른 폴더 동명 PDF 충돌 없음. `load_library`는 `.cache/` 내용을 문서로 로드하지 않음. **효과**: 검증 루프가 33MB IEA·46MB DNV PDF를 2~3회 재생성하며 매번 재파싱하던 시간 낭비 제거. 테스트 11건 green(`tests/collectors/test_library.py`, `_extract_pages` mock).

14. **토큰 최적화** — 보고서 재생성 1회 Opus API 환산 약 $1.23, `--max-iter 1` 기준 약 $2.46. Pro 플랜 5시간 한도 소진 방지 목적.
    - ~~① **Selective Opus**~~: ✅ **완료** (2026-06-09 10차) — `CHAPTER_MODELS` dict로 Ch1·5·6=Opus 4.8, Ch2·3·4·7=Sonnet 4.6 분기. Opus 호출 7→3회, 약 40% 절감. 테스트 14건 green.
    - ② **챕터별 문서 상한(per-chapter doc cap)**: ⏸ **보류** — trust_grade 정렬 후 상위 N건 cap 방식. stage1이 배분한 문서를 추가 차단하는 구조라 중요 수치 소실 위험 존재. 실전 측정 후 재검토.
    - ~~③ **FULLTEXT_CHARS 축소**~~: ✅ **완료** (2026-06-10 18차) — 4000 → 2500자. 핵심 수치 단락 보존(`_extract_key_paragraphs`) 전제. 다음 런에서 품질 영향 실측.
    - ~~④ **MEMO 길이 제한**~~: ✅ **완료** (2026-06-09 10차) — `CHAPTER_PROMPT` 출력 형식에 "메모 전체 700자 이내" 지시 추가. 맥락 손실 없이 누적 메모 비대화 방지.
    - ⑤ **stage3 본문 한도**: ❌ **스킵** — 챕터 앞 N자만 잘라 stage3에 투입 시 챕터 후반 결론·수치 소실 위험이 커 채택 불가. 현재 `full_body[:60000]` 안전장치로 충분.
    - ~~⑥ **[큰 공사 ⭐⭐⭐⭐] 검증 루프 — 영향 챕터만 선별 패치**~~: ✅ **완료** (2026-06-09 15차) — 재수집 후 7챕터 전체 재생성 대신 데이터 공백(gap)+논리 약점(weakness) 영향 챕터만 선별 재작성. **구현 내역**:
      1. **메모 디스크 저장** (선결 과제) — `staged_report.py`: `_write_one_chapter()` 헬퍼가 본문 `.md` 와 함께 `ch{n:02d}_memo.txt` 저장. `stage2_chapters` 가 스킵 시에도 `_load_memo()` 로 디스크 메모 복원해 누적(기존 메모 유실 잠재버그도 해소).
      2. **`stage2_patch(slug, target_ids, notes)`** — outline.json 기준 target 챕터만 `_write_one_chapter` 로 재작성, 나머지는 기존 `ch0N.md` 보존. 챕터 순서대로 메모 누적(재작성=새 메모, 보존=디스크 메모)해 뒤 챕터 맥락 유지. `notes` 는 `[개정 지침]`(`{patch_note}` 프롬프트 슬롯)으로 주입.
      3. **신규 자료 주입** — `collect_and_merge` 가 병합 raw JSON 에 `last_added_urls` 기록 → `_added_doc_indices()` 가 통합 인덱스에서 위치 찾아 모든 target 챕터 자료에 주입(outline 재실행 불필요, 기존 인덱스 안정).
      4. **`affected_chapters`/`revision_plan` 필드** — Flash `_QUERY_PROMPT` 에 7챕터 참조 + `query_plan[].affected_chapters`(데이터 공백) + `revision_plan[{chapter,issue}]`(논리 약점) 추가. `parse_plan()`(신규, `PlanResult` 데이터클래스 반환)이 둘을 파싱, `_as_chapter_id()` 로 1~7 범위 검증. 기존 `parse_query_plan`(구버전 17테스트용)은 그대로 보존.
      5. **`run_validation_loop` 재구성** — `parse_plan` → 공백 챕터(added>0) ∪ 약점 챕터 노트 구성 → `run_partial(slug, target_ids, notes)`(=`stage2_patch`+`stage3_assemble`). 폴백: 신규 자료 있는데 영향 챕터 미상 → `_full_regen()` 전체 재생성. NEEDS_REVISION(재수집 없는 약점만)도 부분 패치 처리.
      - **테스트**: 신규 20건 green (`test_staged_report.py` +13: 메모영속·stage2_patch 5·`_added_doc_indices` 3·run_partial 등 / `test_report_validator.py` +7: `_as_chapter_id` 4·`parse_plan` 6). 전체 130건 green.
      - **잔여**: 보존 챕터(N+k)는 재작성 챕터(N) 변경을 반영 못 함(메모 비대칭 — 합의된 절감 트레이드오프). 실보고서로 절감률 실측 미완.
    - ~~⑦ **[C'+D+G] 자료 중복 배분 축소 + stage3 요약 메모기반화 + 패치 신규자료 단일챕터 주입**~~: ✅ **완료** (2026-06-10 16차) — 동일 자료 블록(최대 4000~8000자)이 여러 챕터 프롬프트에 중복 전송되며 토큰을 배가시키는 구조적 비효율 3곳을 한 번에 해소(품질·데이터 손실 없음, Opus 배분 변경 없음).
      1. **C' — outline 배분 규칙**: `OUTLINE_PROMPT`에 "[배분 규칙]" 신설 — 자료 1건은 가장 관련성 높은 챕터 1~2개에만 배분, 3개 이상 챕터에 중복 배분 금지. 평균 중복도 ~2-3배 → ~1.2-1.5배로 감소 추정(stage2 약 30-40% 절감).
      2. **D — stage3 요약을 메모 기반으로 전환**: `SUMMARY_PROMPT`/`stage3_assemble`이 `full_body[:60000]`(~2만 토큰) 대신 7챕터 누적 이어쓰기 메모(챕터당 최대 700자, 합계 ≤4,900자)를 입력으로 사용. 메모에 이미 핵심주장·인용수치가 압축돼 있어 정보 손실 없이 해당 호출 토큰 90%+ 절감. 메모가 하나도 없으면(레거시/테스트) `full_body[:60000]` 폴백 유지. 검증 루프 매 반복(`run_partial`→`stage3_assemble`)마다 절감 누적.
      3. **G — 패치 신규자료 단일챕터 주입**: `stage2_patch`에서 재수집 신규 문서(`_added_doc_indices`)를 모든 target 챕터가 아닌, 기존 `relevant_doc_indices`가 가장 많은(=가장 관련성 높은) 1개 챕터(동률 시 `max()` 순회상 낮은 id)에만 주입.
      - **테스트**: 신규 4건 green — C'(`OUTLINE_PROMPT` 배분규칙 문구) 1건, D(메모기반 요약 + 본문폴백) 2건, G(단일챕터 주입·동률 낮은id) 1건. 전체 134건 green.
      - **잔여**: 실보고서 절감률 실측 미완. 추가 절감 필요 시 ③(FULLTEXT_CHARS 4000→2000~2500) 다음 단계로 검토.

15. **검증 루프 반복 한도 권고** (2026-06-09 9차) — 플랜별 `--max-iter` 권고치. 5시간 창 내 반복 횟수 = (보고서 재생성 횟수 = max-iter + 1회 초기 생성).
    - **Pro 플랜 ($20/월)**: `--max-iter 1` 권고 (총 2회 생성, 약 $2.46). `--max-iter 2`는 한도 초과 위험.
    - **Max 플랜 ($100/월)**: `--max-iter 2` 안전 (약 $3.70, 5시간 내 3~5회 여유).
    - **Max 플랜 ($200/월)**: 제약 없음.
    - 권고 실행 예시: `.venv\Scripts\python.exe -m src.main <queries...> --generate --validate --max-iter 1`

## 9. 협업 방식

BMad Method 기반 **5인 가상 회의**로 진행 중:
- 📋 John (PM) · 📊 Mary (분석가) · 💻 Amelia (개발자) · 🏗️ Winston (아키텍트) · 🔍 도메인 리서치
- 의사소통/문서 언어: 한국어
- **용도 분리 논의 결론**: "공부(전문성)용 vs KETEP용" 두 파이프라인으로 쪼개자는 논의가 있었으나, **"파이프라인은 하나"로 결론**. study는 KETEP 제안서의 선행 심층이해 단계일 뿐. 쿼리를 한국어+영어 병행으로 주면 한 파이프라인에서 한국 정책자료+글로벌 학술이 동시에 모임. (profiles.py·워크플로우 분기 등은 과잉설계로 폐기)

## 10. 한국 정부 API 현황 (진행 중 — 다음 세션 이어받기 시작점)

KETEP 사전조사의 **정량 근거(통계)·특허맵** 보강을 위해 한국 정부 공식 API 연동 중. `.env`에 키 3개 추가 완료.

### 키 (.env에 저장됨)
- `KOSIS_API_KEY` — ✅ **검증 완료**(동작 확인)
- `KIPRIS_API_KEY` — KIPRIS Plus 사이트(plus.kipris.or.kr) serviceKey. 단, 공공데이터포털 경로가 더 단순해 안 쓸 수도 있음
- `DATA_GO_KR_KEY` — 공공데이터포털 일반 인증키. **특허·가스안전공사 공용**

### 출처별 상태
- **KOSIS** ✅ **완료** — `gov_kr.py::kosis_search` 구현 + `pipeline.collect()` 통합. "연료전지" 검색 → `DT_337001N_A022`(신재생에너지보급실적) 포함 5건 반환 확인. 신뢰도 S[90]. `source_type='stats'`, url=`statHtml.do?orgId=&tblId=`.
- **특허(KIPRIS)** ⚠️ — 현재 신청한 건 "특허·실용 공개 등록공보"(data.go.kr **15065437**, 공보 조회용 → 키워드 검색 약함). **KETEP 키워드 특허검색엔 "특허청_특허실용신안 정보 검색 서비스"(data.go.kr 15058788) 추가 신청 필요.** 둘 다 `DATA_GO_KR_KEY`로 호출. getWordSearch 류로 키워드 검색.
- **국회도서관**(data.go.kr **9720000**) ✅ **완료** (2026-06-09 11차)
  - `gov_kr.py::nalib_search` 구현. `searchservice/basic`(목록) → controlno별 `detailinfoservice/detail`(상세) 2단계 호출.
  - `detail`의 `출처` 필드(원문 URL) → Document.url로 사용. 없으면 `dl.nanet.go.kr` 카탈로그 URL 폴백.
  - `trust.py` S_DOMAINS에 `nl.go.kr`, `nanet.go.kr`, `prism.go.kr` 추가.
  - `relevance.py` GOV_SOURCES에 `"nalib"` 추가(강신호 면제).
  - `pipeline.collect()` 통합 — 소스 10개(+1)로 확장.
  - 테스트: `tests/collectors/test_nalib.py` 16건 green.
  - **키**: `DATA_GO_KR_KEY` 공용 (신규 키 불필요).
  - **커버리지**: 도서자료·학위논문·국내외 기사·정부간행물 — 각도3(국내정책·R&D) 보강에 직접적.

- **가스안전공사 수소 R&D**(data.go.kr **3069894**) ✅ **완료** — `gov_kr.py::gas_safety_search` 구현 + `pipeline.collect()` 통합.
  - 6개 연도 endpoint(2019~2024) 전부 등록. 최신(2024)부터 순차 시도, 결과 나오면 이전 연도 skip.
  - 키워드 필터: API 서버사이드 검색 없음 → perPage=200 전체 가져와 title/field/org 로컬 매칭.
  - 신뢰도: odcloud.kr → S_DOMAINS 추가 → S[94](2024 최신성 보너스 포함).
  - UDDI 목록: 2024=`f1faf6a7-...`, 2023=`c647b493-...`, 2022=`92ab5382-...`, 2021=`a3ef2f33-...`, 2020=`e16ca9a8-...`, 2019=`b44a7f87-..._201909192039`

### 다음 세션 시작 순서 (권장)
1. ~~KOSIS + 가스안전공사 구현·통합~~ ✅ 완료
2. (선택) 공공데이터포털 "특허 정보검색"(15058788) 활용신청 → 특허 키워드검색 어댑터
3. 관련성 필터 노이즈 개선(로드맵 6번) — min_hits 상향 또는 강신호 용어 가중
4. (선택) 보고서 생성 자동화(`src/generators/`) — 무인 모드 전환 시
6. 가상 컨소시엄 역할 설계 템플릿 정의 및 파이프라인 연계
7. 정책 1차 소스 수집기 및 HWP 파서 도입을 위한 기술 검토 (Amelia 협업)

---

## 11. 신규 분석 합의 사항 및 개발 가이드 (2026-06-06 추가)

### ① RFP 부재 시 대응 전략 (RFP-less Scenario)
*   **과거 유사 과제 벤치마킹:** 공고된 RFP가 없는 사전 기획 단계의 경우, NTIS API를 통해 수집된 과거 유사 R&D 과제의 스펙(RFP 상의 예산 규모, 기간, TRL 기준 등)을 벤치마킹하여 '가상 RFP 스펙'을 추정하고 역설계합니다.
*   **표준 KPI/TRL 매핑:** 미국 DOE 수명 가이드라인 및 국내 표준 시험 절차 등을 기반으로, 해당 기술 영역의 디폴트 KPI와 가시적인 R&D 이정표(Milestone)를 자동 추천하도록 기획합니다.

### ② 가상 컨소시엄 역할 자동 설계
*   보고서의 완성도를 극대화하기 위해, R&D의 이해관계자 구도를 가상으로 설계하여 역할을 자동 부여합니다.
    *   **앵커 대기업 (수요처):** 최종 제품 사양 제시 및 실증 피드백, 사업화 판로 제공.
    *   **소부장 중소/중견기업 (공급처/주관):** 핵심 부품(스택, 인버터 등) 및 시스템 레벨 평가 장비 개발.
    *   **정부출연연구기관 (정출연):** 원천 기술 지원, 표준 시험 인증 및 신뢰성 평가 프로토콜 검증.
    *   **지자체/공공기관 (실증):** 수소 인프라 연계 및 실증 부지 제공, 규제 샌드박스 지원.
    *   **대학 (학계):** AI RUL 모델 설계, 기초 노화 메커니즘 해석 및 전문 인력 양성.

### ③ 개발팀(Amelia) 검토 필요 사항 (비용 및 구현 가능성)
*   **차등적 기간 수집 (Hybrid Search Window):**
    *   원천 연구(All-time)와 최신 연구(최근 3년 제한) 수집을 병행할 시, API 호출 횟수 및 토큰 소모 비용을 평가하여 최적의 임계값 설정 필요.
*   **정책 최신화 파이프라인:**
    *   대한민국 정책브리핑(`korea.kr`) 등 정책 1차 소스 웹스크래퍼 구축 난이도 검토.
    *   보도자료 첨부파일의 `.hwp`/`.hwpx` 포맷 텍스트 추출을 위한 경량 파서 패키지 도입 검토.

---

## 12. 파이프라인의 Claude Code "서브에이전트" 운영 검토 (2026-06-12, 결론: 현행 구조 유지)

**질문**: 파이프라인 각 단계(수집→원문회수→보고서 생성→검증)를 Claude Code Agent tool 서브에이전트로 쪼개, 파일을 주고받으며 단계 간 검증을 거치는 방식으로 운영하면 효율적인가?

### 결론

**전면 재설계는 비권고**. 이미 "단계 분리 + 파일 핸드오프 + 검증 게이트"는 현재 구조에서 달성되어 있고, Agent tool은 이 구조에 구조적으로 맞지 않는다. 단, 검증 루프 내 좁은 "사실 확인" 보조용으로 선택적 사용은 가치 있음(아래 "남는 활용처" 참조).

### 근거 1 — 이미 사실상 "여러 에이전트 + 파일 핸드오프 + 검증"이다

현재 파이프라인을 다시 보면:

- **결정론적 단계**(Python, LLM 없음): `pipeline.collect()` → `output/raw/{slug}_{date}.json`, `fetch`/`crawl_retry`/`wayback_retry` → `_sources/`+`manifest.json`. 여기엔 "에이전트"가 필요 없다.
- **지능 단계**(이미 독립 프로세스 단위로 분리됨):
  - stage1(개요, Sonnet `claude -p`) → `outline.json`
  - stage2(챕터별, **챕터마다 별도 `claude -p` 서브프로세스**, Selective Opus/Sonnet) → `ch0N.md` + `ch0N_memo.txt` — 이어쓰기 메모로 다음 챕터에 압축 컨텍스트 전달
  - stage3(조합, Sonnet `claude -p`) → `output/reports/{slug}.md`
- **검증 게이트**(agy 2단계, 이미 "리뷰 에이전트 → 플래너 에이전트" 분업): Pro 리뷰(`review-v{n}.md`, 자유서술) → Flash 플랜(`queries-v{n}.json`, 구조화) → `collect_and_merge` → `run_partial`(영향 챕터만 부분 재생성)

즉 "각 단계가 파일을 주고받으며 검증한다"는 목표는 21차까지의 누적 설계(메모 디스크 저장, `_hallucination_guard`, `_added_doc_chapter_map`, citation_audit 등)로 이미 구현되어 있다. 차이는 "Agent tool"이 아니라 `claude -p`/`agy` CLI 서브프로세스를 단위로 쓴다는 점뿐이다.

### 근거 2 — Agent tool은 이 오케스트레이터와 구조적으로 안 맞는다

- Agent tool은 **Claude Code 대화 세션 내부에서만 호출 가능**하다. `src/pipeline.py`, `src/generators/staged_report.py` 등은 `.venv\Scripts\python.exe -m src.main`으로 실행되는 순수 Python 프로세스라 Agent tool을 호출할 수 없다. "파이프라인을 서브에이전트로 쪼갠다"는 것은 오케스트레이터 자체를 이 대화(Claude Code 세션)로 옮긴다는 뜻인데, 이는 CLAUDE.md의 "방식 A(Claude Code 주도, 단 Python CLI가 오케스트레이션)" 설계를 뒤집는 큰 재설계다.
- Agent tool 서브에이전트는 **매번 콜드스타트로 컨텍스트를 재도출**한다(Agent tool 자체 설명에도 명시: "Each spawn starts cold and re-derives context"). 반면 현재 설계는 메모(`ch0N_memo.txt`, 700자 이내)·다이제스트(`_quant_digest`)로 **필요한 컨텍스트만 압축해 다음 단계에 전달**하도록 정교화돼 있다(16~21차). Agent tool로 바꾸면 이 압축 설계의 이점이 콜드스타트 재도출 비용으로 상쇄된다.
- **결정론적 단계의 서브에이전트화는 순손실**이다. collect/fetch/relevance 필터는 LLM 판단이 필요 없는 코드이며, 서브에이전트로 감싸면 비용·시간만 늘어난다(테스트 가능성도 떨어짐).
- **stage2 챕터 생성은 병렬화 여지가 작다.** Ch1→Ch2→Ch3 순서로 메모를 이어받고, Ch4는 1~3 종합, Ch7은 전체 종합이라 의존성 체인이 강하다. Ch1·5·6처럼 독립적인 챕터가 일부 있지만, 메모 체인을 끊고 병렬화하면 "이어쓰기 일관성"(20차까지 핵심 설계 목표)이 깨진다.

### 남는 활용처 — 검증 루프의 "좁은 사실확인" 보조

전면 재설계 대신, **이 Claude Code 세션이 검증 결과를 검토할 때** 한정적으로 Agent(Explore/general-purpose) 도구를 보조적으로 쓸 수 있다. 예:

- agy 리뷰가 "Ch3의 LCOE 수치가 출처와 맞는지 의심된다"고 지적했을 때, raw JSON·원문 `_sources/`를 뒤져 해당 수치의 근거를 빠르게 찾는 좁은 조사 작업.
- 단, `citation_audit.audit_findings()`(21차)가 이미 수치-출처 매칭을 결정론적으로 수행하므로, 이 용도는 audit가 못 잡는 **정성적 주장**(예: "이 비교 결론이 출처 내용과 맞는가") 검증에 한해 가치가 있다.

이는 "파이프라인 재구조화"가 아니라, 검증 단계에서 이 세션이 필요시 선택하는 도구일 뿐이다.

**Why**: 사용자가 효율적인 운영 방식을 재검토 요청 → 분석 결과 현재 구조가 이미 목표를 달성하고 있어 재설계 비용 대비 이득이 없음을 문서화.
**How to apply**: 다음에 "서브에이전트로 바꾸자"는 논의가 다시 나오면 이 절을 먼저 참조. 만약 향후 무인 자동화(cron, ANTHROPIC_API_KEY 도입)로 전환한다면 전제가 바뀌므로 재검토 필요(현재는 Claude Code 세션 주도 전제).

### ④ 멀티에이전트 오케스트레이션 검토 — agy(Antigravity) CLI (2026-06-07, **폐기**)
*   **시도한 설계:** 주제 1회 입력 → Gemini Flash(검색 쿼리 확장) + Gemini Pro(수집·보고서 검토 ×2) + Claude Sonnet(보고서 작성)을 `agy -p`/`claude -p` 서브프로세스로 엮어, 자료 부족 시 재수집하는 최대 3회 자기수정 루프(model tiering으로 비용 최적화).
*   **폐기 사유(핵심 발견):**
    1.  `agy -p` 는 파일쓰기 등 툴 실행 전 **대화형 권한 승인 프롬프트**를 띄운다. TTY 없는 서브프로세스/자동화 환경에는 승인 주체가 없어 **무한 대기**(좀비 프로세스·CPU 스핀, `--print-timeout` 무시). 사용자 터미널 직접 실행 시에만 동작.
    2.  `agy` stdout 은 TTY 직접 렌더링이라 **캡처 불가** → 결과 회수는 프롬프트에 `save it as [절대경로]` 명시로 파일 경유만 가능.
    3.  `--dangerously-skip-permissions` 로 우회 가능하나 Claude Code 안전분류기가 자기권한확장으로 차단(`.claude/settings.json` 에 사용자가 직접 `Bash(agy *)` 허용 추가해야 함).
*   **결론:** 현 시점 무인 자동화 부적합. 보고서 생성은 `claude -p`(세션 인증 재사용·권한 프롬프트 없음) 단독으로 충분(7절). 무인 멀티모델이 꼭 필요해지면 그때 각 모델 **공식 API**(Anthropic / Google AI SDK) 직접 호출로 재설계. 시도 경위 전문: `docs/agy-conversation.md`.
*   **⚠️ 반례 (2026-06-08 8차):** 위 폐기 사유 ①(권한 프롬프트 hang)은 `--dangerously-skip-permissions` 없이도 **agy settings.json `permissions.allow` 에 `write_file(경로)` 화이트리스트 추가**로 해결됨. 검증 루프(`report_validator.py`)에서 agy 2단계(Pro 분석 + Flash 쿼리플랜) 무인 동작 확인. 상세는 3절 검증단계 "필수 사전조건" 참조. 단, stdout 캡처 불가(폐기 사유 ②)는 여전 → write_to_file 파일 경유 방식 유지.
