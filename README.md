# Crawling — 이중언어 기술 리서치 자동화 파이프라인

주제를 입력하면 국내외 소스를 자동 수집·검증하여 기술 리서치 보고서(마크다운)를 생성하는 파이프라인입니다.

## 개요

- **수집**: 쿼리 언어에 따라 소스를 라우팅합니다.
  - 한국어 쿼리 → Tavily + 국내 API (KOSIS · 가스안전공사 · KIPRIS 국내 · 국회도서관)
  - 영어 쿼리 → Tavily + 학술·해외 (Exa · arXiv · Semantic Scholar · EIA · KIPRIS 해외)
- **검증**: 수집 자료의 데이터 공백을 점검하고 필요 시 추가 쿼리를 생성합니다.
- **생성**: 7개 챕터 구조의 보고서를 `output/reports/`에 출력합니다.

## 폴더 구조

| 폴더 | 설명 |
|------|------|
| `src/` | 파이프라인 핵심 코드 |
| `templates/` | 주제별 보고서 챕터 템플릿 (JSON) |
| `scripts/` | 보조 스크립트 |
| `tests/` | 테스트 |
| `docs/` | 프로젝트 문서 |

## 시작하기

```powershell
# 1. 가상환경 (Python 3.12) 및 패키지 설치
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. API 키 설정: .env.example 을 복사해 .env 로 만들고 값 채우기
copy .env.example .env

# 3. 실행 (주제를 여러 각도의 한/영 쿼리로 나눠 전달)
.venv\Scripts\python.exe -m src.main "쿼리1" "query2" ... --generate --validate --max-iter 1
```

필요한 API 키 목록은 [`.env.example`](.env.example) 참고.

## 참고

- 생성 결과물(`output/`), 외부 참고자료(`library/`)는 용량 문제로 저장소에서 제외됩니다.
- 실행·쿼리 작성 규칙 등 상세 지침은 [`CLAUDE.md`](CLAUDE.md) 참고.
