"""pipeline.collect 라우팅·상한 테스트 — 수집기 호출은 전부 mock.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.test_pipeline -v
"""
import unittest
from unittest.mock import patch

from src import pipeline
from src.models import Document


def _doc(url: str, source: str = "tavily", content: str = "수소 hydrogen 본문") -> Document:
    return Document(title=url, url=url, content=content,
                    source=source, source_type="web")


def _collector_mocks(**returns):
    """pipeline 의 수집기 함수들을 빈 리스트(또는 지정 값)로 patch 하는 컨텍스트 목록."""
    names = ["tavily_search", "exa_search", "arxiv_search",
             "semantic_scholar_search", "kosis_search", "gas_safety_search",
             "eia_search", "kipris_search", "kipris_foreign_search",
             "nalib_search"]
    return [patch.object(pipeline, n,
                         return_value=returns.get(n, []), create=False)
            for n in names]


class IsKoreanTest(unittest.TestCase):
    def test_korean_detected(self):
        self.assertTrue(pipeline._is_korean("수소 정책 R&D"))

    def test_english_only(self):
        self.assertFalse(pipeline._is_korean("green hydrogen LCOE 2026"))


class CollectRoutingTest(unittest.TestCase):
    """쿼리 언어에 따라 맞는 소스만 호출되는지 검증."""

    def _run(self, queries):
        mocks = _collector_mocks()
        started = [m.start() for m in mocks]
        try:
            pipeline.collect(queries, per_query=3)
            names = ["tavily_search", "exa_search", "arxiv_search",
                     "semantic_scholar_search", "kosis_search",
                     "gas_safety_search", "eia_search", "kipris_search",
                     "kipris_foreign_search", "nalib_search"]
            return {n: m.call_count for n, m in zip(names, started)}
        finally:
            for m in mocks:
                m.stop()

    def test_korean_query_skips_english_sources(self):
        calls = self._run(["수소 에너지 정책"])
        self.assertEqual(calls["tavily_search"], 1)
        self.assertEqual(calls["kosis_search"], 1)
        self.assertEqual(calls["gas_safety_search"], 1)
        self.assertEqual(calls["kipris_search"], 1)
        self.assertEqual(calls["nalib_search"], 1)
        # 영어 특화 소스는 호출 안 함
        self.assertEqual(calls["exa_search"], 0)
        self.assertEqual(calls["arxiv_search"], 0)
        self.assertEqual(calls["semantic_scholar_search"], 0)
        self.assertEqual(calls["eia_search"], 0)
        self.assertEqual(calls["kipris_foreign_search"], 0)

    def test_english_query_skips_korean_sources(self):
        calls = self._run(["green hydrogen electrolyzer"])
        self.assertEqual(calls["tavily_search"], 1)
        self.assertEqual(calls["exa_search"], 1)
        self.assertEqual(calls["arxiv_search"], 1)
        self.assertEqual(calls["semantic_scholar_search"], 1)
        self.assertEqual(calls["eia_search"], 1)
        self.assertEqual(calls["kipris_foreign_search"], 1)
        # 국내 API 는 호출 안 함
        self.assertEqual(calls["kosis_search"], 0)
        self.assertEqual(calls["gas_safety_search"], 0)
        self.assertEqual(calls["kipris_search"], 0)
        self.assertEqual(calls["nalib_search"], 0)

    def test_mixed_queries_route_each(self):
        calls = self._run(["수소 정책", "green hydrogen"])
        self.assertEqual(calls["tavily_search"], 2)   # 양쪽 모두
        self.assertEqual(calls["kosis_search"], 1)    # 한국어 쿼리만
        self.assertEqual(calls["arxiv_search"], 1)    # 영어 쿼리만


class RunCapsTest(unittest.TestCase):
    """목록형 소스의 런 단위 상한 검증."""

    def test_gas_safety_capped_per_run(self):
        many = [_doc(f"odcloud://{i}", source="gas_safety",
                     content="수소 R&D 과제") for i in range(20)]
        mocks = _collector_mocks(gas_safety_search=many)
        for m in mocks:
            m.start()
        try:
            docs = pipeline.collect(["수소 연료전지"], per_query=5)
        finally:
            for m in mocks:
                m.stop()
        n_gas = sum(1 for d in docs if d.source == "gas_safety")
        self.assertEqual(n_gas, pipeline.RUN_CAPS["gas_safety"])

    def test_uncapped_source_not_limited(self):
        many = [_doc(f"http://w/{i}", source="tavily",
                     content="수소 연료전지 분석 내용") for i in range(15)]
        mocks = _collector_mocks(tavily_search=many)
        for m in mocks:
            m.start()
        try:
            docs = pipeline.collect(["수소 연료전지"], per_query=5)
        finally:
            for m in mocks:
                m.stop()
        self.assertEqual(sum(1 for d in docs if d.source == "tavily"), 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
