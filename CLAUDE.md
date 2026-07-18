# Crawling 프로젝트 — Claude Code 세션 지침

## 파이프라인 실행 규칙

### 주제 → 쿼리 변환 (필수)

사용자가 **주제**를 주면 직접 `src.main`에 넘기지 말고, 먼저 아래 **5개 각도**를 기준으로 쿼리를 생성한다. 각도당 한국어 1개 + 영어 1개씩이 기본이며, 총 **한국어 3~4개 + 영어 4~5개 = 7~9개**가 목표다.

| # | 각도 | 커버하는 보고서 챕터 |
|---|------|----------------------|
| 1 | **기술 현황·TRL** | Ch.1 기술 개요 |
| 2 | **시장·경제성** (LCOE, 비용, CAGR, 시장 규모) | Ch.2 시장 맥락, Ch.4 정량 비교 |
| 3 | **국내 정책·R&D** (한국 정부, 연구개발 로드맵) | Ch.6 플레이어·정책 |
| 4 | **비교·경쟁 기술** (대안 기술 대비 장단점) | Ch.3 기술 경로, Ch.4 비교 |
| 5 | **최근 실증·사례** (파일럿, 데모, 2024~2026 동향) | Ch.3 경로, Ch.5 쟁점 |

> **핵심**: 각도 1만으로 6개를 채우는 게 아니라, 5개 각도를 고르게 커버해야 검증 단계에서 추가 쿼리가 줄어든다.

생성한 쿼리를 사용자에게 제시하고 확인받은 뒤 실행한다.

**예시 — 주제: "2026년 청정수소 생산 기술 동향"**
```powershell
.venv\Scripts\python.exe -m src.main `
  "청정수소 생산 기술 현황 TRL" `          # 각도1 기술현황 (한국어)
  "그린수소 시장 규모 비용 LCOE 2026" `    # 각도2 시장경제성 (한국어)
  "한국 수소 정책 그린수소 R&D 로드맵" `   # 각도3 국내정책 (한국어)
  "clean hydrogen production technology TRL review" `   # 각도1 (영어)
  "green hydrogen electrolyzer LCOE cost reduction" `   # 각도2 (영어)
  "blue hydrogen CCS SMR comparison technology" `       # 각도4 비교기술 (영어)
  "green hydrogen pilot demonstration project 2025" `   # 각도5 실증사례 (영어)
  --generate --validate --max-iter 1
```

**잘못된 실행 (단일 주제 문장을 그대로 사용):**
```powershell
# 이렇게 하면 안 됨 — 영어 소스(Exa·arXiv·S2)에서 거의 0건
.venv\Scripts\python.exe -m src.main "2026년 청정수소 생산 기술 동향"
```

### 근거

`src/pipeline.py`의 `collect()`는 쿼리 언어로 소스를 라우팅한다(2026-06-10 17차) — **한국어 쿼리**는 Tavily+국내 API(KOSIS·가스안전공사·KIPRIS 국내·국회도서관), **영어 쿼리**는 Tavily+학술·해외(Exa·arXiv·Semantic Scholar·EIA·KIPRIS 해외)만 호출한다. 즉 **영어 쿼리가 없으면 학술 논문·해외 통계·해외 특허는 아예 수집되지 않고, 한국어 쿼리가 없으면 국내 정책·통계 자료가 수집되지 않는다.** 이중언어 쿼리 균형이 이전보다 더 직접적으로 수집 범위를 결정한다.

각도 균형 없이 기술 현황 쿼리만 집중하면, 시장·정책·비교 각도 데이터가 없어 검증기(agy)가 이를 "데이터 공백"으로 지적 → 검증 후 추가 쿼리가 증가한다. 초기에 각도를 고르게 커버하면 이 사이클을 줄일 수 있다.

---

## 그 외 운영 메모

- Python 가상환경: `.venv\Scripts\python.exe` (3.12.10 전용, `py -3.12` 빌드)
- 프로젝트 컨텍스트 전체: `docs/project-context.md`
- 출력 위치: `output/reports/{slug}.md`
