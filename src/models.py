"""파이프라인 전체가 공유하는 표준 데이터 구조."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Document:
    """수집기 종류와 무관하게 모든 자료를 동일한 형태로 표현한다."""

    title: str
    url: str
    content: str
    source: str               # 수집기 이름: tavily, exa, arxiv, eia ...
    source_type: str          # web | paper | stats | report
    published: Optional[str] = None   # 발행일 (ISO 문자열)
    score: Optional[float] = None     # 검색엔진 관련성 점수
    trust_grade: Optional[str] = None    # 신뢰도 등급 SS/S/AA/A/B/C
    trust_score: Optional[float] = None  # 신뢰도 점수 0~100
    metadata: dict = field(default_factory=dict)

    def short(self) -> str:
        """로그/미리보기용 한 줄 요약."""
        g = f" {self.trust_grade}" if self.trust_grade else ""
        sc = f" [{self.trust_score:.0f}]" if self.trust_score is not None else ""
        date = f" ({self.published[:10]})" if self.published else ""
        return f"[{self.source}]{g}{sc}{date} {self.title}"
