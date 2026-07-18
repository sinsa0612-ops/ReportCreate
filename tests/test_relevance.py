"""관련성 필터 테스트 — 강신호(도메인 핵심어) 요구 로직 검증.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.test_relevance -v
"""
import unittest

from src.models import Document
from src.processors.relevance import (
    query_terms,
    strong_terms,
    filter_relevant,
)

# "분산형 연료전지 시스템 신뢰성 평가" + 영어 동의어 (실전 쿼리 재현)
QUERIES = [
    "분산형 연료전지 시스템 신뢰성 평가",
    "distributed fuel cell system reliability assessment evaluation prediction",
]


def doc(title: str, content: str = "", source: str = "tavily",
        source_type: str = "web") -> Document:
    return Document(title=title, url=f"http://x/{hash(title) & 0xffff}",
                    content=content, source=source, source_type=source_type)


class StrongTermsTest(unittest.TestCase):
    def test_generic_words_excluded_from_strong(self):
        """system·reliability·evaluation·cell 등 범용어는 강신호에서 제외된다."""
        strong = strong_terms(query_terms(QUERIES))
        for generic in ("system", "reliability", "evaluation", "assessment",
                        "prediction", "cell", "시스템", "신뢰성", "평가"):
            self.assertNotIn(generic, strong,
                             f"'{generic}' 는 범용어이므로 강신호가 아니어야 한다")

    def test_domain_anchors_kept_as_strong(self):
        """fuel·연료전지 같은 도메인 핵심어는 강신호로 남는다."""
        strong = strong_terms(query_terms(QUERIES))
        self.assertIn("fuel", strong)
        self.assertIn("연료전지", strong)


class FilterRelevantTest(unittest.TestCase):
    def test_generic_only_noise_dropped(self):
        """범용어만 매칭되는 노이즈(LLM·지진·줄기세포)는 제외된다."""
        noise = [
            doc("Measuring Five-Nines Reliability: Sample-Efficient LLM Evaluation",
                "We evaluate large language model reliability and prediction."),
            doc("Transfer Learning for T-Cell Response Prediction",
                "Immunology cell response prediction system."),
            doc("Field Teams Coordination for Earthquake-Damaged Distribution System",
                "Power distribution system reliability after earthquakes."),
            doc("Modeling adult skeletal stem cell response to laser-machined",
                "Stem cell tissue engineering evaluation."),
        ]
        kept, dropped = filter_relevant(noise, QUERIES)
        self.assertEqual(kept, [], "도메인 핵심어 없는 자료는 모두 제외되어야 한다")
        self.assertEqual(len(dropped), 4)

    def test_domain_doc_kept(self):
        """연료전지/fuel 을 포함한 자료는 통과한다."""
        good = [
            doc("군사용 휴대 연료전지 시스템의 열 신뢰성 평가",
                "연료전지 시스템 신뢰성 평가 연구"),
            doc("Lifetime Prediction Analysis of Proton Exchange Membrane Fuel Cell",
                "PEM fuel cell durability and degradation prediction."),
        ]
        kept, dropped = filter_relevant(good, QUERIES)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_strong_hit_required_even_with_many_generic(self):
        """범용어를 여러 개 맞춰도 강신호가 없으면 제외된다 (min_hits 만으로는 불충분)."""
        d = doc("System reliability assessment and prediction methods",
                "A generic study on system reliability, evaluation, and prediction.")
        kept, dropped = filter_relevant([d], QUERIES)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_gov_source_exempt_from_strong_requirement(self):
        """정부 API 소스는 범용어만 매칭돼도(강신호 0) 통과한다.

        동일 문서를 web 소스로 두면 강신호 0 이라 제외되는 것과 대비된다.
        """
        title, content = "에너지 시장 동향 평가", "시장 동향 평가 통계"
        gov = doc(title, content, source="kosis", source_type="stats")
        web = doc(title, content, source="tavily", source_type="web")
        kept, dropped = filter_relevant([gov, web], QUERIES)
        self.assertIn(gov, kept, "kosis 는 범용어만 맞춰도 통과해야 한다")
        self.assertIn(web, dropped, "동일 내용이라도 web 은 강신호 0 이라 제외")

    def test_fallback_when_no_strong_terms(self):
        """쿼리가 전부 범용어면 강신호 요구를 끄고 기존 min_hits 동작으로 폴백한다."""
        generic_queries = ["system reliability evaluation"]
        d = doc("System reliability evaluation framework",
                "system reliability evaluation across domains")
        kept, dropped = filter_relevant([d], generic_queries)
        self.assertEqual(len(kept), 1,
                         "강신호 용어가 없는 쿼리에서는 min_hits 만으로 판정해야 한다")

    def test_fuel_cell_abbreviation_kept(self):
        """약어(PEMFC/SOFC)만 쓴 연료전지 논문도 강신호로 인정해 통과한다.

        쿼리에 '연료전지'/'fuel' 앵커가 있으면 동의어 확장으로 약어를 강신호 처리.
        """
        d = doc("A Review of Life Prediction Methods for PEMFCs in Electric Vehicles",
                "Life prediction methods for PEMFCs durability and reliability.")
        kept, dropped = filter_relevant([d], QUERIES)
        self.assertIn(d, kept, "PEMFC 논문은 연료전지 자료이므로 통과해야 한다")

    def test_unrelated_with_generic_still_dropped(self):
        """약어 확장 후에도 전력망 분석 툴(ETAP 등) 같은 비연료전지 자료는 제외."""
        d = doc("Reliability Assessment | Distribution Network Analysis | ETAP",
                "Power distribution network reliability assessment software.")
        kept, dropped = filter_relevant([d], QUERIES)
        self.assertIn(d, dropped)

    def test_patent_without_title_strong_signal_dropped(self):
        """특허 소스는 제목에 강신호가 없으면 제외된다(21차 문제4 — 무관 특허 혼입 차단)."""
        d = doc("METHODS OF DOSING AND ADMINISTRATION OF ENGINEERED ISLET CELLS",
                "", source="kipris_foreign", source_type="patent")
        kept, dropped = filter_relevant([d], QUERIES)
        self.assertIn(d, dropped)

    def test_patent_with_title_strong_signal_kept(self):
        """제목에 강신호(fuel 등)가 있는 특허는 통과한다."""
        d = doc("FUEL CELL STACK DURABILITY APPARATUS",
                "", source="kipris", source_type="patent")
        kept, dropped = filter_relevant([d], QUERIES)
        self.assertIn(d, kept)

    def test_relevance_metadata_recorded(self):
        """doc.metadata 에 total/strong 매칭 수가 기록된다."""
        d = doc("연료전지 신뢰성 평가", "연료전지 시스템 신뢰성 평가")
        filter_relevant([d], QUERIES)
        self.assertIn("relevance", d.metadata)
        self.assertIn("relevance_strong", d.metadata)
        self.assertGreaterEqual(d.metadata["relevance_strong"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
