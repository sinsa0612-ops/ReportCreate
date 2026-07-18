# 파이프라인 워크플로우 상세 스펙

> 최종 업데이트: 2026-06-09  
> 구현 기준: `src/main.py`, `src/pipeline.py`, `src/generators/staged_report.py`, `src/validators/report_validator.py`

---

## 개요

```
사용자 쿼리 목록
      │
      ▼
[A] 데이터 수집 (src.main / src.pipeline)
      │
      ▼
[B] 보고서 생성 (src.generators.staged_report)
   Stage 1 → Stage 2 → Stage 3
      │
      ▼
[C] 검증 루프 (src.validators.report_validator)
   Pro 리뷰 → Flash 플랜 → 재수집 → 부분 재생성 → (반복)
```

---

## A. 데이터 수집 (`src.main` + `src.pipeline`)

### 진입점

```powershell
.venv\Scripts\python.exe -m src.main "쿼리1" "쿼리2" ... [--generate] [--validate] [--max-iter N]
```

### 플래그

| 플래그 | 의미 |
|--------|------|
| (없음) | 수집만 |
| `--generate` | 수집 + 보고서 생성 |
| `--validate` | 수집 + 생성 + 검증 루프 (`--generate` 포함) |
| `--max-iter N` | 검증 루프 최대 반복 횟수 (기본 1) |

### 4단계 수집 흐름

#### [1/4] 검색 수집 — `pipeline.collect()`

쿼리 1개당 10개 소스 병렬 호출 (하나 실패해도 계속):

| 소스 | 함수 | 특성 |
|------|------|------|
| Tavily | `tavily_search` | 웹 검색, 한국어/영어 모두 |
| Exa | `exa_search` | 연구 논문 카테고리 |
| arXiv | `arxiv_search` | 영어 쿼리 필수 |
| Semantic Scholar | `semantic_scholar_search` | 영어 쿼리 필수 |
| KOSIS | `kosis_search` | 통계청 |
| 가스안전공사 | `gas_safety_search` | 국내 규제 |
| EIA | `eia_search` | 미국 에너지 정보청, 영어 필수 |
| KIPRIS 국내 | `kipris_search` | 국내 특허 |
| KIPRIS 해외 | `kipris_foreign_search` | 해외 특허 |
| 국립중앙도서관 | `nalib_search` | 국내 문헌 |

- `per_query=5` 기본값 → 쿼리 N개 × 10소스 × 5건 = 이론 최대 50N건
- URL 기준 중복 제거 → 관련성 필터(`filter_relevant`) → 신뢰도 등급 부여(`annotate`)
- 결과: `output/raw/{slug}_{date}.json`

**신뢰도 등급 체계**

```
SS > S > AA > A > B > C
```

| 등급 | 해당 출처 |
|------|----------|
| SS | 최우선 공인 기관 (IEA, EIA, IRENA 등) |
| S | 주요 학술지 (Nature, Science 등) |
| AA | 국내 에너지 기관 (KIER, KEEI 등) |
| A | 일반 학술 출처 |
| B | 일반 신뢰 웹 |
| C | 기타 |

#### [2/4] 원문 저장 — `fetch_sources()`

- httpx + trafilatura: 웹페이지 본문 추출
- pymupdf: PDF 처리
- 결과: `output/raw/{slug}_{date}_sources/` 폴더 + `manifest.json`

#### [3/4] 봇차단 회수 — `crawl_retry()`

- crawl4ai로 JS 렌더링 필요한 페이지 재시도
- 실패 시 skip (전체 중단 없음)

#### [4/4] 아카이브 폴백 — `wayback_retry()`

- Wayback Machine으로 접근 불가 URL 폴백
- 실패 시 skip

---

## B. 보고서 생성 (`src.generators.staged_report`)

### 모델 구성

| 역할 | 모델 |
|------|------|
| Stage 1 (개요) | `claude-sonnet-4-6` |
| Stage 2 Ch.1·5·6 | `claude-opus-4-8` |
| Stage 2 Ch.2·3·4·7 | `claude-sonnet-4-6` |
| Stage 3 (조합) | `claude-sonnet-4-6` |

**Selective Opus 선정 기준**: Ch.1(TRL 판단·기술 분류), Ch.5(복합 리스크 판단), Ch.6(정책 종합) → 추론·종합이 무거운 챕터만 Opus → 비용 약 40% 절감

### 자료 통합 — `_prepare_docs()` (Stage 1·2 공통)

Stage 1(개요)과 Stage 2(챕터 작성) 모두 이 함수를 통해 자료를 준비합니다.

```
lib_docs  = load_library(queries=queries)   # 로컬 라이브러리 (쿼리 관련성 필터링 후)
collected = raw JSON의 documents            # A단계에서 수집한 자료

all_docs  = lib_docs + collected            # 라이브러리가 앞번호(인덱스 우선)
```

- 라이브러리 문서는 원문 인덱스 없음 (이미 신뢰도 높은 고정 자료)
- 수집 문서는 `manifest.json` 기반 풀텍스트 인덱스 연결
- `relevant_doc_indices`는 이 통합 인덱스 기준 번호

### Stage 1 — 개요 생성

**입력**: `_prepare_docs()` 통합 자료 (라이브러리 + 수집)  
**프롬프트**: 전체 자료 목록(번호·등급·출처·제목·스니펫 140자)  
**출력**: `output/drafts/{slug}/outline.json` + `outline.md`

`outline.json` 구조:

```json
{
  "slug": "...",
  "queries": [...],
  "chapters": [
    {
      "id": 1,
      "title": "기술 개요",
      "key_question": "...",
      "must_include": "...",
      "key_points": ["Claude가 잡은 핵심 포인트 2~4개"],
      "relevant_doc_indices": [1, 3, 7, ...]
    }
  ]
}
```

### Stage 2 — 챕터별 작성

**7개 챕터 골격** (고정):

| # | 제목 | 독자 핵심 질문 | 반드시 포함 |
|---|------|----------------|------------|
| 1 | 기술 개요 | TRL 현재 어디? | 기술 정의·분류 체계, TRL 범위 |
| 2 | 부상 배경 및 시장 맥락 | 시장 규모·CAGR? | 드라이버, 시장 규모·목표 연도 |
| 3 | 주요 기술 경로 분석 | 경쟁 방식별 성숙도? | 경로별 원리·특성·대표 사례·TRL |
| 4 | 정량 비교 분석 | 숫자로 어느 기술이 앞서? | 비교표, '데이터 없음' vs 'N/A' 구분 |
| 5 | 쟁점 및 리스크 | 기술·경제·규제 장벽? | 3축 구분: 기술/경제성/규제·환경 |
| 6 | 주요 플레이어 및 정책 환경 | 누가 개발·지원? | 기업·기관 현황, 정책·자금 동향 |
| 7 | 시사점 및 권고안 | 무엇을 해야 하나? | 단기/중장기 구분, 3문장 구조 |

**챕터 작성 루프** (Ch.1 → Ch.7 순서):

```
for 챕터 c in outline.chapters:
  1. relevant_doc_indices로 자료 선택 (없으면 상위 8건 폴백)
  2. 이전 챕터 메모 누적 → [이전 챕터들의 이어쓰기 메모] 블록 구성
  3. CHAPTER_PROMPT 조립 (스타일 + 챕터 정보 + 메모 + [patch_note] + 자료)
  4. claude -p 호출 (모델: CHAPTER_MODELS[c.id])
  5. 출력 분리: <<<MEMO>>> 구분자로 본문/메모 split
  6. 저장: ch{n:02d}_{제목}.md + ch{n:02d}_memo.txt
  7. 메모 누적 → 다음 챕터 프롬프트에 전달
```

**이어쓰기 메모 구조** (700자 이내):

```
핵심 주장: (이 챕터가 확정한 사실 1~3개)
정의된 용어: (처음 정의한 용어)
인용 수치: (핵심 수치 — 다음 챕터에서 일관되게 사용)
미해결 실마리: (다음 챕터로 넘기는 떡밥)
```

**스킵 조건**: `ch{n:02d}_{제목}.md` 파일이 이미 존재하면 스킵, 단 `_load_memo()`로 디스크 메모 복원해 체인 유지

**7-A 인용 교체 규칙** (CHAPTER_PROMPT 내장):

> 등급(SS>S>AA>A>B>C)과 발행연도 두 차원이 **모두** 신규 자료가 우월할 때만 기존 인용 교체. 한 차원이라도 기존이 우위면 기존 인용 유지 + 신규 정보 별도 항목으로 보충.

**Paige 작성 스타일** (CHAPTER_PROMPT 내장):

- Julia Evans처럼 독자 과제 중심으로 쉽게, Edward Tufte의 데이터 정밀성으로 정확하게
- 표가 문장보다 정보를 더 전달하는 경우 반드시 표 사용
- 모든 정량 수치 뒤에 `[등급 | 출처명]` 부착 (예: `효율 74.4% [A | ScienceDirect 2025]`)
- 기술적 한계·리스크를 긍정 측면과 동등한 비중으로 기술
- 격식체(합니다체) + 순수 Markdown 출력

### Stage 3 — 조합

**입력**: `ch01~07_*.md` 전체  
**작업**: 60,000자로 트리밍한 본문 → Sonnet에 `핵심 요약` 생성 요청  
**참고 자료 섹션**: SS→S→AA→A→B→C 등급순 정렬  
**출력**: `output/reports/{slug}.md`

최종 보고서 구조:
```
[핵심 요약 (한 줄 결론 + 핵심 수치 3개 이상 bullet)]
[Ch.1 본문] [Ch.2 본문] ... [Ch.7 본문]
[참고 자료 — 등급순]
```

---

## C. 검증 루프 (`src.validators.report_validator`)

### 진입점

```powershell
.venv\Scripts\python.exe -m src.validators.report_validator <slug> [--max-iter N] [--skip-review]
```

### 루프 구조 (최대 `max_iter`회 반복)

```
for iteration = 1..max_iter:

  [1] Pro 리뷰   ─ Gemini 3.1 Pro (High)
  [2] Flash 플랜 ─ Gemini 3.5 Flash
  [3] 재수집     ─ pipeline.collect()  (should_recollect=True 일 때만)
  [4] 병합       ─ 기존 raw에 신규 문서 append
  [5] 부분 재생성 ─ run_partial() 또는 _full_regen() 폴백

  if 보완 대상 챕터 없음 → break
```

### [1] Pro 리뷰 — Gemini 3.1 Pro (High)

**역할 페르소나**: BMAD 프레임워크의 Paige(Technical Writer)  
**입력**: `@output/reports/{slug}.md`  
**출력 형식**: 자유 마크다운 (`output/reviews/{slug}-review-v{n}.md`)

리뷰 필수 항목:

1. **강점** — 구조·근거·완결성·명료성 측면에서 구체적으로
2. **약점** — 논리 비약, 근거 없는 주장, 챕터 번호 명시
3. **데이터 공백** — 어떤 종류의 자료(정량 스펙·시장 데이터·정책 문서)가 필요한지

**`--skip-review`**: 기존 리뷰 파일이 있으면 재사용 (Pro 재호출 생략, Flash는 항상 실행)

### [2] Flash 쿼리 플랜 — Gemini 3.5 Flash

**입력**: 보고서 + Pro 리뷰 + 기존 수집 문서 목록(최대 120건) + 챕터 번호 참조  
**출력**: `output/reviews/{slug}-queries-v{n}.json`

출력 JSON 스키마:

```json
{
  "should_trigger": true,
  "verdict": "NEEDS_RECOLLECTION",
  "query_plan": [
    {
      "rationale": "메울 공백 설명",
      "priority": "high",
      "queries": ["한국어 쿼리", "English query 1", "English query 2"],
      "affected_chapters": [3, 4]
    }
  ],
  "revision_plan": [
    { "chapter": 5, "issue": "재수집 없이 고칠 논리 약점" }
  ]
}
```

**verdict 선택지**:

| verdict | 의미 | 동작 |
|---------|------|------|
| `APPROVED` | 보완 불필요 | 루프 종료 |
| `NEEDS_REVISION` | 논리/구조만 보강 | 재수집 없음, revision_plan만 실행 |
| `NEEDS_RECOLLECTION` | 추가 자료 필요 | 재수집 + revision_plan 병행 |

**priority 필터**: `medium` 이하는 query_plan 제외 (`critical`·`high`만 실행)

**파싱 결과 — `PlanResult`**:

```python
@dataclass
class PlanResult:
    should_recollect: bool              # True면 재수집 실행
    queries: list[str]                  # 평탄화·중복 제거된 쿼리
    recollect_chapters: dict[int, str]  # {챕터id: 공백 사유} — 데이터 공백
    revision_chapters: dict[int, str]   # {챕터id: 약점 설명} — 논리 약점
```

### [3~4] 재수집 + 병합 — `collect_and_merge()`

- `pipeline.collect(queries, per_query=5)` 호출
- 기존 URL 집합과 비교 → 신규 URL만 `added` 목록 구성
- 신규 0건 → 새 파일 생성 없이 `(원본, 0)` 반환
- 신규 있음 → `{slug}_{date}_v{n}.json` 저장
  - `last_added_urls` 필드 기록 (부분 재생성용)
  - `fetch_sources(incremental=True)` → base `_sources` 폴더에 신규 원문 누적

### [5] 부분 재생성 — `run_partial()` vs `_full_regen()`

**챕터 노트 구성**:

```
chapter_notes: dict[int, list[str]] = {}
  ↑ added > 0인 경우만 recollect_chapters 포함
  ↑ revision_chapters는 항상 포함
→ notes = {cid: "\n".join(notes_list)}
```

**재생성 경로 결정**:

```
if added > 0 AND recollect_chapters == {}:
    → _full_regen()      # 신규 자료 있는데 영향 챕터 미상 → 유실 방지
elif outline/챕터 드래프트 없음:
    → _full_regen()      # FileNotFoundError 폴백
else:
    → run_partial(slug, target_chapter_ids, notes)
```

**`stage2_patch()` 상세 흐름**:

```
for 챕터 c in outline.chapters:
  if c.id in target_ids:
    idxs = outline의 relevant_doc_indices
           + extra_idxs (_added_doc_indices로 신규 자료 주입)
    patch_note = "[개정 지침 — 이전 검토 피드백]\n{note}"
    → _write_one_chapter() 재작성 (새 .md + 새 _memo.txt 덮어쓰기)
  else:
    → 기존 .md 보존 + _load_memo()로 디스크 메모 복원 (체인 유지)
```

- **`run_partial()`** = `stage2_patch()` + `stage3_assemble()`
- **`_full_regen()`** = `ch??_*.md` 전체 삭제 + `generate_report()` (outline.json 재사용, stage1 재실행 없음)

---

## 파일 시스템 레이아웃

```
output/
├── raw/
│   ├── {slug}_{date}.json                  # 초기 수집 raw
│   ├── {slug}_{date}_sources/              # 원문 폴더
│   │   ├── manifest.json
│   │   └── {hash}.txt / .pdf
│   └── {slug}_{date}_v{n}.json             # 검증 재수집 병합본 (n=반복 횟수)
├── drafts/
│   └── {slug}/
│       ├── outline.json                    # Stage 1 출력 (챕터별 자료 배분)
│       ├── outline.md                      # 사람이 검토하는 개요
│       ├── ch01_{제목}.md                  # Stage 2 챕터 본문
│       ├── ch01_memo.txt                   # 이어쓰기 메모 (디스크 영속)
│       ├── ch02_{제목}.md
│       ├── ch02_memo.txt
│       │   ...
│       └── ch07_{제목}.md / _memo.txt
├── reports/
│   └── {slug}.md                           # 최종 보고서
└── reviews/
    ├── {slug}-review-v1.md                 # Pro 리뷰
    ├── {slug}-queries-v1.json              # Flash 쿼리 플랜
    ├── {slug}-review-v2.md
    └── {slug}-queries-v2.json
```

---

## 비용 구조 (Selective Opus 적용 시)

| 단계 | 모델 | 상대 비용 |
|------|------|----------|
| Stage 1 개요 | Sonnet | 낮음 |
| Ch.2·3·4·7 | Sonnet | 낮음 |
| Ch.1·5·6 | Opus 4.8 | 높음 |
| Stage 3 조합 | Sonnet | 낮음 |
| 부분 재생성 (1챕터) | Opus or Sonnet | 전체 대비 최선 ~85% 절감 |

> 실보고서 절감률 실측 미완 — `--max-iter 1`로 실제 보고서 실행 후 측정 필요.

---

## 한계 (설계 트레이드오프)

1. **메모 비대칭**: 챕터 N 재작성 후 챕터 N+k(보존)는 변경 사항을 반영하지 못함
2. **단일 방향 메모 체인**: Ch.1→Ch.7 순서 의존, 역방향 참조 없음
3. **outline 재사용**: 검증 재수집 후 outline의 `relevant_doc_indices`는 갱신 안 됨 — `extra_idxs`로 보완하나 완전하지 않음
