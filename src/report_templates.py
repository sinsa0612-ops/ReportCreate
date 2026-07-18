"""보고서 양식(템플릿) 시스템 — 주제 유형별로 챕터 골격을 갈아끼운다.

배경: 기존엔 7챕터 골격이 staged_report.py 에 하드코딩돼 있어 '단일 기술 TRL
분석' 형태의 보고서만 만들 수 있었다. 이 모듈은 챕터 구성(제목·핵심질문·모델
배정·종합 챕터 지정·개요 배분 규칙)을 **데이터로 분리**해, 주제에 맞는 양식을
선택할 수 있게 한다.

구성:
  - builtin  : DEFAULT_TEMPLATE(에너지 7챕터) — templates/ 가 비어도 항상 존재.
               기존 보고서 동작을 그대로 보존하는 하위호환 기본값.
  - JSON     : templates/*.json 을 읽어 등록/덮어쓰기. 새 주제 양식은 JSON 파일
               하나만 추가하면 되고 코드 수정이 필요 없다.
  - 바인딩   : 수집 시 raw payload 에 template 이름을 저장(pipeline.save_raw) →
               개요·챕터작성·검증 전 단계가 template_for_slug() 로 같은 양식을 공유.

JSON 스키마(templates/<name>.json):
  {
    "name": "petrochem-gx-ax",          # --template 인자 · payload 저장값
    "label": "석유화학 GX·AX 도입",       # 사람이 읽는 이름
    "description": "...",                # 어떤 주제에 맞는지
    "chapters": [
      {"id": 1, "title": "...", "key_question": "...", "must_include": "..."},
      ...
    ],
    "opus_chapters": [1, 2, 3, 5],       # Opus 로 쓸 챕터(나머지는 Sonnet)
    "prev_body_chapters": [4],           # 이전 챕터 수치 다이제스트를 받는 종합 챕터
    "memo_only_chapters": [7],           # 자료 없이 메모만으로 쓰는 종합 챕터
    "outline_extra_rules": "..."         # 개요 배분 프롬프트에 덧붙일 규칙
  }
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATES_DIR = Path("templates")

# 챕터 dict 에 반드시 있어야 하는 키
_REQUIRED_CHAPTER_KEYS = ("id", "title", "key_question", "must_include")


@dataclass(frozen=True)
class ReportTemplate:
    """하나의 보고서 양식 — 챕터 골격 + 챕터별 동작 설정."""

    name: str
    label: str
    description: str
    chapters: tuple[dict, ...]
    opus_chapters: frozenset[int] = field(default_factory=frozenset)
    prev_body_chapters: frozenset[int] = field(default_factory=frozenset)
    memo_only_chapters: frozenset[int] = field(default_factory=frozenset)
    outline_extra_rules: str = ""

    @property
    def chapter_ids(self) -> list[int]:
        return [c["id"] for c in self.chapters]

    @property
    def max_id(self) -> int:
        return max(self.chapter_ids) if self.chapters else 0

    @property
    def n_chapters(self) -> int:
        return len(self.chapters)

    def models(self, opus: str, sonnet: str) -> dict[int, str]:
        """챕터별 모델 배정 {id: model}. opus_chapters 만 opus, 나머지 sonnet."""
        return {c["id"]: (opus if c["id"] in self.opus_chapters else sonnet)
                for c in self.chapters}

    def skeleton_text(self) -> str:
        """개요 프롬프트용 — 'N. 제목 — 핵심질문 (포함: ...)' 여러 줄."""
        return "\n".join(
            f"{c['id']}. {c['title']} — {c['key_question']} "
            f"(포함: {c['must_include']})"
            for c in self.chapters)

    def chapters_ref(self) -> str:
        """검증기용 — 'N. 제목' 번호·제목 목록(영향 챕터 지목 근거)."""
        return "\n".join(f"{c['id']}. {c['title']}" for c in self.chapters)


def _validate_chapters(chapters) -> tuple[dict, ...]:
    if not chapters:
        raise ValueError("chapters 가 비어 있습니다.")
    out: list[dict] = []
    seen: set[int] = set()
    for c in chapters:
        for k in _REQUIRED_CHAPTER_KEYS:
            if k not in c:
                raise ValueError(f"챕터에 '{k}' 키가 없습니다: {c}")
        cid = int(c["id"])
        if cid in seen:
            raise ValueError(f"챕터 id 중복: {cid}")
        seen.add(cid)
        out.append({
            "id": cid,
            "title": str(c["title"]),
            "key_question": str(c["key_question"]),
            "must_include": str(c["must_include"]),
        })
    return tuple(out)


def from_dict(d: dict) -> ReportTemplate:
    """JSON dict → ReportTemplate. 필수 키 검증 + id 집합 정규화."""
    chapters = _validate_chapters(d.get("chapters") or [])
    valid_ids = {c["id"] for c in chapters}

    def _id_set(key: str) -> frozenset[int]:
        return frozenset(int(x) for x in (d.get(key) or []) if int(x) in valid_ids)

    return ReportTemplate(
        name=str(d["name"]),
        label=str(d.get("label") or d["name"]),
        description=str(d.get("description") or ""),
        chapters=chapters,
        opus_chapters=_id_set("opus_chapters"),
        prev_body_chapters=_id_set("prev_body_chapters"),
        memo_only_chapters=_id_set("memo_only_chapters"),
        outline_extra_rules=str(d.get("outline_extra_rules") or ""),
    )


# ── 기본(builtin) 양식 — 에너지 기술 7챕터 ─────────────────────────────
# staged_report.py 에 하드코딩돼 있던 CHAPTER_SKELETON 을 그대로 옮긴 것.
# templates/ 폴더가 비어 있어도 이 양식은 항상 존재한다(하위호환 안전망).

_DEFAULT_OUTLINE_RULES = (
    "- 챕터 4(정량 비교)에는 정량 수치(LCOE·효율·비용·시장규모·TRL 등)가 풍부한 "
    "자료를\n  우선 배분하십시오. 목록의 '수치:' 미리보기를 참고하십시오.\n"
    "- 챕터 7(시사점)에는 자료를 배분하지 마십시오(relevant_doc_indices 를 빈 배열로).\n"
    "  챕터 7은 이전 챕터들의 결론을 종합해 작성됩니다. 챕터 7에 어울려 보이는 자료는\n"
    "  내용상 가장 가까운 다른 챕터(1~6)에 배분하십시오."
)

DEFAULT_TEMPLATE = ReportTemplate(
    name="energy-default",
    label="에너지 기술 분석 (기본 7챕터)",
    description=("단일 에너지·과학기술 주제의 현황·시장·경로·비교·쟁점·정책·"
                 "시사점 심층 분석. 주제 미지정 시 기본값."),
    chapters=(
        {"id": 1, "title": "기술 개요",
         "key_question": "이 기술이 무엇이고 현재 기술 성숙도(TRL)는 어디인가?",
         "must_include": "기술 정의·분류 체계, TRL 범위"},
        {"id": 2, "title": "부상 배경 및 시장 맥락",
         "key_question": "이 기술이 왜 지금 주목받고, 시장은 얼마나 크고 빠르게 크는가?",
         "must_include": "사회·경제·규제 드라이버, 시장 규모·CAGR·목표 연도"},
        {"id": 3, "title": "주요 기술 경로 분석",
         "key_question": "어떤 기술 방식들이 경쟁하고 있고 각각의 성숙도는?",
         "must_include": "경로별 원리·특성·대표 사례·TRL·실증/상업화 현황"},
        {"id": 4, "title": "정량 비교 분석",
         "key_question": "숫자로 보면 어느 기술이 앞서는가?",
         "must_include": "비교표 — 이전 챕터(1~3)가 인용한 수치와 일관되게 구성. "
                         "셀은 '데이터 없음'(미발견)과 '해당 없음(N/A, 사유)' 구분"},
        {"id": 5, "title": "쟁점 및 리스크",
         "key_question": "무엇이 기술적·경제적·규제적 장벽인가?",
         "must_include": "기술 장벽, 경제성·비용 리스크, 규제·환경·사회 리스크 3축 구분"},
        {"id": 6, "title": "주요 플레이어 및 정책 환경",
         "key_question": "누가 개발하고 있고 각국 정부는 어떻게 지원하는가?",
         "must_include": "기업·기관 현황(국가별 분류 권장), 정책·규제·자금 동향"},
        {"id": 7, "title": "시사점 및 권고안",
         "key_question": "그래서 무엇을 해야 하는가?",
         "must_include": "단기(~2년)/중장기(3년~) 구분. 각 항목 [문제 인식]→[구체 행동]→"
                         "[기대 효과] 3문장. 이전 챕터들의 결론·수치(메모)만 근거로 사용"},
    ),
    opus_chapters=frozenset({1, 5, 6}),
    prev_body_chapters=frozenset({4}),
    memo_only_chapters=frozenset({7}),
    outline_extra_rules=_DEFAULT_OUTLINE_RULES,
)

_BUILTINS: dict[str, ReportTemplate] = {DEFAULT_TEMPLATE.name: DEFAULT_TEMPLATE}


# ── 레지스트리 ─────────────────────────────────────────────────────

def _load_json_templates(templates_dir: Path = TEMPLATES_DIR
                         ) -> dict[str, ReportTemplate]:
    """templates/*.json 을 읽어 {name: ReportTemplate}. 개별 실패는 경고 후 건너뜀."""
    reg: dict[str, ReportTemplate] = {}
    if not templates_dir.is_dir():
        return reg
    for p in sorted(templates_dir.glob("*.json")):
        try:
            t = from_dict(json.loads(p.read_text(encoding="utf-8")))
            reg[t.name] = t
        except Exception as e:
            print(f"  [warn] 템플릿 로드 실패 {p.name}: {type(e).__name__}: {e}")
    return reg


def all_templates(templates_dir: Path = TEMPLATES_DIR
                  ) -> dict[str, ReportTemplate]:
    """builtin + JSON 병합 레지스트리. 같은 name 이면 JSON 이 builtin 을 덮어쓴다."""
    reg = dict(_BUILTINS)
    reg.update(_load_json_templates(templates_dir))
    return reg


def get_template(name: str | None,
                 templates_dir: Path = TEMPLATES_DIR) -> ReportTemplate:
    """이름으로 양식을 찾는다. 없거나 빈 이름이면 기본 양식으로 폴백한다."""
    if not name:
        return DEFAULT_TEMPLATE
    t = all_templates(templates_dir).get(name)
    if t is None:
        print(f"  [warn] 템플릿 '{name}' 을(를) 찾을 수 없습니다 — 기본 양식 사용")
        return DEFAULT_TEMPLATE
    return t


def template_for_slug(slug: str) -> ReportTemplate:
    """slug 의 raw payload 에 저장된 template 이름으로 양식을 해석한다.

    payload 에 template 키가 없거나(레거시) 로드 실패면 기본 양식을 쓴다 —
    기존 보고서·테스트가 그대로 동작하도록.
    """
    from src.generators.report import find_raw  # 지연 import(순환 방지)
    try:
        payload = json.loads(find_raw(slug).read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_TEMPLATE
    return get_template(payload.get("template"))


if __name__ == "__main__":
    # 등록된 양식 목록 확인용: python -m src.report_templates
    for name, t in all_templates().items():
        src = "builtin" if name in _BUILTINS and name not in _load_json_templates() \
            else "json"
        print(f"[{name}] {t.label} — {t.n_chapters}챕터 "
              f"(opus={sorted(t.opus_chapters)}, "
              f"종합={sorted(t.prev_body_chapters)}, "
              f"메모전용={sorted(t.memo_only_chapters)})")
        print(f"    {t.description}")
