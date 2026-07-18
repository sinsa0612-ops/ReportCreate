"""보고서 투입 자료 선별(select_for_report) 테스트.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.processors.test_select -v
"""
import unittest

from src.processors.select import (
    select_for_report, selection_score, content_bonus, count_groups,
    doc_angles, GROUP_CAPS, FULLTEXT_BONUS, ANGLE_MIN,
)


def _doc(url: str, source: str = "tavily", score: float = 60.0,
         content: str = "x" * 1000) -> dict:
    return {"title": url, "url": url, "content": content,
            "source": source, "trust_score": score}


def _stub(url: str, source: str, score: float = 94.0) -> dict:
    """목록형 메타데이터 스텁 — 본문 200자 수준."""
    return _doc(url, source=source, score=score, content="메타데이터 " * 20)


class ContentBonusTest(unittest.TestCase):
    def test_stub_penalized(self):
        self.assertEqual(content_bonus({"content": "짧음"}), -8)

    def test_rich_rewarded(self):
        self.assertEqual(content_bonus({"content": "x" * 2500}), 8)

    def test_missing_content_penalized(self):
        self.assertEqual(content_bonus({}), -8)


class SelectForReportTest(unittest.TestCase):
    def test_patent_quota_capped(self):
        """특허(목록형)는 점수가 높아도 쿼터(5건)까지만 들어간다."""
        patents = [_stub(f"p{i}", "kipris_foreign", 95) for i in range(20)]
        papers = [_doc(f"a{i}", "exa", 75) for i in range(20)]
        out = select_for_report(patents + papers, 20)
        n_patent = sum(1 for d in out if d["source"] == "kipris_foreign")
        self.assertEqual(n_patent, GROUP_CAPS["patent"])
        self.assertEqual(len(out), 20)  # 남은 슬롯은 논문으로 채움

    def test_stats_and_listing_quota(self):
        eias = [_stub(f"e{i}", "eia", 95) for i in range(10)]
        gas = [_stub(f"g{i}", "gas_safety", 94) for i in range(10)]
        papers = [_doc(f"a{i}", "arxiv", 75) for i in range(30)]
        out = select_for_report(eias + gas + papers, 30)
        self.assertEqual(sum(1 for d in out if d["source"] == "eia"),
                         GROUP_CAPS["stats"])
        self.assertEqual(sum(1 for d in out if d["source"] == "gas_safety"),
                         GROUP_CAPS["listing"])

    def test_rich_paper_beats_stub_of_similar_grade(self):
        """원문 풍부한 A급(75+8=83)이 스텁 S급(88-8=80)보다 앞선다."""
        stub = _stub("s1", "gas_safety", 88)
        paper = _doc("a1", "exa", 75, content="x" * 2500)
        out = select_for_report([stub, paper], 1)
        self.assertEqual(out[0]["url"], "a1")

    def test_deterministic(self):
        """같은 입력 → 같은 결과 (outline 인덱스 안정성의 전제)."""
        docs = ([_stub(f"p{i}", "kipris") for i in range(8)]
                + [_doc(f"w{i}", "tavily", 60 + i) for i in range(10)])
        out1 = [d["url"] for d in select_for_report(list(docs), 10)]
        out2 = [d["url"] for d in select_for_report(list(docs), 10)]
        self.assertEqual(out1, out2)

    def test_max_docs_respected(self):
        docs = [_doc(f"w{i}") for i in range(50)]
        self.assertEqual(len(select_for_report(docs, 40)), 40)

    def test_non_listing_sources_uncapped(self):
        """일반 소스(tavily·exa 등)는 쿼터 없이 점수순으로 채운다."""
        docs = [_doc(f"w{i}", "tavily") for i in range(30)]
        self.assertEqual(len(select_for_report(docs, 25)), 25)

    def test_selection_score_combines(self):
        d = _doc("u", score=70, content="x" * 900)
        self.assertEqual(selection_score(d), 74)  # 70 + 4


class FulltextBonusTest(unittest.TestCase):
    """18차 A6 — 원문 파일이 확보된 문서는 선별 점수 +6."""

    def test_score_includes_fulltext_bonus(self):
        d = _doc("u", score=70, content="x" * 900)
        self.assertEqual(selection_score(d, fulltext_urls={"u"}), 74 + FULLTEXT_BONUS)
        self.assertEqual(selection_score(d, fulltext_urls={"other"}), 74)

    def test_fulltext_doc_ranked_first(self):
        a = _doc("a", score=70)
        b = _doc("b", score=70)
        out = select_for_report([a, b], 1, fulltext_urls={"b"})
        self.assertEqual(out[0]["url"], "b")


class CumulativeQuotaTest(unittest.TestCase):
    """18차 — 배치 간 그룹 쿼터 누적(검증 재수집 쿼터 리셋 수정)."""

    def test_count_groups(self):
        docs = [_stub("p1", "kipris"), _stub("e1", "eia"), _doc("w1")]
        self.assertEqual(count_groups(docs), {"patent": 1, "stats": 1})

    def test_initial_counts_block_exhausted_group(self):
        """기본 배치가 특허 쿼터를 소진했으면 추가 배치 특허는 0건."""
        base = [_stub(f"p{i}", "kipris", 95) for i in range(GROUP_CAPS["patent"])]
        more = [_stub(f"q{i}", "kipris_foreign", 95) for i in range(5)]
        out = select_for_report(more, 12, initial_counts=count_groups(base))
        self.assertEqual(out, [])

    def test_initial_counts_partial_remaining(self):
        """쿼터 일부만 소진됐으면 남은 만큼만 들어간다."""
        base = [_stub(f"p{i}", "kipris", 95) for i in range(3)]
        more = ([_stub(f"q{i}", "kipris_foreign", 95) for i in range(5)]
                + [_doc("a1", "exa", 75)])
        out = select_for_report(more, 12, initial_counts=count_groups(base))
        n_patent = sum(1 for d in out if d["source"] == "kipris_foreign")
        self.assertEqual(n_patent, GROUP_CAPS["patent"] - 3)
        self.assertIn("a1", [d["url"] for d in out])  # 일반 소스는 영향 없음


def _market_doc(url: str, score: float = 55.0) -> dict:
    return _doc(url, "tavily", score,
                content="그린수소 LCOE 분석과 시장 규모 전망. " * 30)


def _demo_doc(url: str, score: float = 60.0) -> dict:
    return _doc(url, "tavily", score,
                content="실증 파일럿 프로젝트 운전 결과 보고. " * 30)


class DocAnglesTest(unittest.TestCase):
    """19차 — 문서 각도 판별(키워드 매칭)."""

    def test_market_keywords(self):
        self.assertIn("market", doc_angles(_market_doc("m")))

    def test_demo_keywords(self):
        self.assertIn("demo", doc_angles(_demo_doc("d")))

    def test_no_angle(self):
        self.assertEqual(doc_angles(_doc("g")), set())

    def test_ascii_word_boundary(self):
        """'usd' 가 'used' 에 오매칭되지 않는다."""
        d = _doc("x", content="this method is used widely in research papers")
        self.assertNotIn("market", doc_angles(d))


class AngleBalanceTest(unittest.TestCase):
    """19차 — 각도(시장·정책·비교·실증)별 최소 슬롯 보장."""

    def test_starved_angle_promoted(self):
        """시장 각도 자료가 점수에 밀려도 ANGLE_MIN 건은 들어온다."""
        generic = [_doc(f"g{i}", "exa", 90) for i in range(12)]
        market = [_market_doc(f"m{i}") for i in range(4)]
        out = select_for_report(generic + market, 12)
        n_market = sum(1 for d in out if d["url"].startswith("m"))
        self.assertEqual(n_market, ANGLE_MIN)
        self.assertEqual(len(out), 12)          # 총원 유지(교체, 추가 아님)

    def test_no_swap_when_angle_satisfied(self):
        """각도가 이미 충족되면 일반 점수순 결과와 동일하다."""
        docs = ([_market_doc(f"m{i}", 90) for i in range(ANGLE_MIN)]
                + [_doc(f"g{i}", "exa", 80) for i in range(7)])
        balanced = select_for_report(list(docs), 10)
        plain = select_for_report(list(docs), 10, angle_min=0)
        self.assertEqual([d["url"] for d in balanced],
                         [d["url"] for d in plain])

    def test_guardian_not_removed(self):
        """다른 각도의 최소선을 지키는 문서는 교체 피해자가 되지 않는다."""
        generic = [_doc(f"g{i}", "exa", 90) for i in range(7)]
        demos = [_demo_doc(f"d{i}", 60) for i in range(ANGLE_MIN)]  # 최저점 수호자
        market = [_market_doc(f"m{i}", 50) for i in range(ANGLE_MIN)]
        out = select_for_report(generic + demos + market, 10)
        urls = [d["url"] for d in out]
        for i in range(ANGLE_MIN):              # 실증 수호자 전원 보존
            self.assertIn(f"d{i}", urls)
        n_market = sum(1 for u in urls if u.startswith("m"))
        self.assertEqual(n_market, ANGLE_MIN)   # 시장도 충족(일반 문서가 빠짐)

    def test_under_capacity_appends_without_removal(self):
        """선발 인원이 max_docs 미만이면 교체 없이 그냥 추가된다."""
        docs = [_doc("g1", "exa", 90), _market_doc("m1")]
        out = select_for_report(docs, 10)
        self.assertEqual(len(out), 2)

    def test_angle_min_zero_disables(self):
        generic = [_doc(f"g{i}", "exa", 90) for i in range(12)]
        market = [_market_doc(f"m{i}") for i in range(4)]
        out = select_for_report(generic + market, 12, angle_min=0)
        self.assertEqual(sum(1 for d in out if d["url"].startswith("m")), 0)

    def test_deterministic_with_balancing(self):
        docs = ([_doc(f"g{i}", "exa", 90) for i in range(12)]
                + [_market_doc(f"m{i}") for i in range(4)]
                + [_demo_doc(f"d{i}") for i in range(2)])
        out1 = [d["url"] for d in select_for_report(list(docs), 12)]
        out2 = [d["url"] for d in select_for_report(list(docs), 12)]
        self.assertEqual(out1, out2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
