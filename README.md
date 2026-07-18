# Crawling — 이중언어 기술 리서치 자동화 파이프라인

주제를 입력하면 국내외 소스를 자동으로 **수집 → 검증 → 보고서 생성**하는 리서치 파이프라인입니다.
쿼리 언어(한/영)에 따라 소스를 자동으로 라우팅하여, 한 번의 실행으로 국내 정책·통계와 해외 학술·시장 자료를 함께 모읍니다.

## ✨ 주요 기능

- **수집** — 쿼리 언어에 따라 소스 라우팅
  - 🇰🇷 한국어 쿼리 → Tavily + 국내 API (KOSIS · 가스안전공사 · KIPRIS 국내 · 국회도서관)
  - 🇺🇸 영어 쿼리 → Tavily + 학술·해외 (Exa · arXiv · Semantic Scholar · EIA · KIPRIS 해외)
- **검증** — 수집 자료의 데이터 공백을 점검하고 필요 시 추가 쿼리를 생성 (`--validate`)
- **생성** — 7개 챕터 구조의 기술 리서치 보고서를 마크다운으로 출력 (`--generate`)

---

## 📋 준비물 (Prerequisites)

이 프로젝트는 하려는 작업에 따라 필요한 도구가 다릅니다.

| 하려는 것 | 필요한 것 |
|-----------|-----------|
| **자료 수집만** | Python 3.12 · 아래 API 키 (그것만 있으면 동작) |
| **보고서 생성** (`--generate`) | 위 + [Claude Code](https://claude.com/claude-code) CLI 설치·로그인 |
| **검증 루프** (`--validate`) | 위 + `agy` (Antigravity / Gemini CLI) |

> 💡 API 키 없이 코드만 받아 구조를 살펴보는 것은 문제없습니다. 실제 수집·생성을 하려면 아래 키가 필요합니다.

### 필요한 API 키 (무료 발급 가능)

`.env.example`에 목록이 있으며, 각 서비스에서 키를 발급받아 `.env`에 채웁니다.

| 키 | 용도 | 발급처 |
|----|------|--------|
| `TAVILY_API_KEY` | 웹 검색 (한/영 공통) | tavily.com |
| `EXA_API_KEY` | 학술·전문 검색 (영어) | exa.ai |
| `EIA_API_KEY` | 미국 에너지 통계 | eia.gov/opendata |
| `KOSIS_API_KEY` | 국가통계포털 | kosis.kr |
| `KIPRIS_API_KEY` | 특허 검색 | kipris.or.kr |
| `DATA_GO_KR_KEY` | 공공데이터포털 (가스안전공사 등) | data.go.kr |

---

## 🚀 설치 및 실행

### 1. 코드 내려받기

```bash
git clone https://github.com/sinsa0612-ops/ReportCreate.git
cd ReportCreate
```

### 2. 가상환경 만들기 + 패키지 설치

**Windows (PowerShell)**
```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux**
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. API 키 설정

`.env.example`을 복사해 `.env`를 만들고, 발급받은 키를 채웁니다.

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

### 4. 실행

**Windows**
```powershell
.venv\Scripts\python.exe -m src.main "쿼리1" "query2" ... --generate --validate --max-iter 1
```

**macOS / Linux** (가상환경 활성화 후)
```bash
python -m src.main "쿼리1" "query2" ... --generate --validate --max-iter 1
```

| 옵션 | 설명 |
|------|------|
| (옵션 없음) | 자료 수집만 실행 |
| `--generate` | 수집 후 보고서 자동 생성 (Claude Code CLI 필요) |
| `--validate` | 생성 후 검증 루프 실행 (`agy` 필요, `--generate` 포함) |
| `--max-iter N` | 검증-재수집 반복 최대 횟수 |
| `--template NAME` | 주제별 보고서 챕터 템플릿 지정 |

---

## 💡 쿼리 작성 팁 — 이중언어 + 여러 각도

이 파이프라인은 **쿼리 언어로 소스를 나눕니다.** 영어 쿼리가 없으면 해외 학술·통계가 아예 수집되지 않고, 한국어 쿼리가 없으면 국내 정책·통계가 빠집니다. 따라서 주제를 **한국어 3~4개 + 영어 4~5개**로, 아래 5개 각도를 고르게 커버하도록 나누는 것이 좋습니다.

1. 기술 현황·TRL  2. 시장·경제성(비용·LCOE·시장규모)  3. 국내 정책·R&D  4. 비교·경쟁 기술  5. 최근 실증·사례

**예시 — 주제 "청정수소 생산 기술 동향"**
```bash
python -m src.main \
  "청정수소 생산 기술 현황 TRL" \
  "그린수소 시장 규모 비용 LCOE 2026" \
  "한국 수소 정책 그린수소 R&D 로드맵" \
  "clean hydrogen production technology TRL review" \
  "green hydrogen electrolyzer LCOE cost reduction" \
  "blue hydrogen CCS SMR comparison technology" \
  "green hydrogen pilot demonstration project 2025" \
  --generate --validate --max-iter 1
```

자세한 운영 규칙은 [`CLAUDE.md`](CLAUDE.md) 참고.

---

## 📂 폴더 구조

| 폴더 | 설명 |
|------|------|
| `src/` | 파이프라인 핵심 코드 (`src/main.py`가 진입점) |
| `templates/` | 주제별 보고서 챕터 템플릿 (JSON) |
| `scripts/` | 보조 스크립트 |
| `tests/` | 테스트 |
| `docs/` | 프로젝트 문서 |

> 생성 결과물(`output/`), 외부 참고 PDF(`library/`)는 용량 문제로 저장소에서 제외됩니다. `output/` 하위 폴더는 실행 시 자동 생성되며, `library/`는 없어도 동작합니다(있으면 그 문서들이 소스로 추가됨).

---

## 📄 라이선스

[MIT License](LICENSE) — 누구나 자유롭게 사용·수정·배포할 수 있습니다.
