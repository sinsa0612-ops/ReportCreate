"""EIA 어댑터 단위 테스트 — 외부 네트워크·API 키 불필요 (전부 mock).

실행:
    .venv\\Scripts\\python.exe -m unittest tests.collectors.test_eia -v
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from src.collectors.eia import match_routes, eia_search, _build_content
from src.models import Document


# ------------------------------------------------------------------
# match_routes
# ------------------------------------------------------------------

class MatchRoutesTest(unittest.TestCase):
    def test_english_electricity(self):
        self.assertIn("electricity", match_routes("electricity generation"))

    def test_korean_renewable(self):
        routes = match_routes("태양광 풍력 재생에너지")
        self.assertIn("renewable-energy", routes)

    def test_hydrogen_has_no_route(self):
        """수소→petroleum 매핑은 제거됨 — 석유 개요 페이지가 수소 보고서에
        S급 노이즈로 투입되던 문제(평가 보고서 H2). 수소 쿼리는 매칭 없음."""
        self.assertEqual(match_routes("수소 hydrogen fuel cell"), [])

    def test_korean_nuclear(self):
        self.assertIn("nuclear-outages", match_routes("원자력 원전 smr"))

    def test_co2_emissions(self):
        self.assertIn("co2-emissions", match_routes("탄소 배출 co2 emission"))

    def test_unknown_query_returns_empty(self):
        """무매칭 쿼리는 빈 리스트 — total-energy 기본값 스텁 노이즈 제거."""
        self.assertEqual(match_routes("xyzzy undefined topic 999"), [])

    def test_no_duplicate_routes(self):
        """동일 경로가 중복 반환되지 않아야 한다."""
        routes = match_routes("petroleum oil crude 석유 원유")
        self.assertEqual(len(routes), len(set(routes)))

    def test_natural_gas(self):
        self.assertIn("natural-gas", match_routes("천연가스 natural gas LNG"))


# ------------------------------------------------------------------
# _build_content
# ------------------------------------------------------------------

class BuildContentTest(unittest.TestCase):
    def test_includes_description(self):
        meta = {"description": "Electricity Overview", "startPeriod": "2001", "endPeriod": "2024"}
        content = _build_content("electricity", meta)
        self.assertIn("Electricity Overview", content)

    def test_includes_date_range(self):
        meta = {"description": "X", "startPeriod": "2000", "endPeriod": "2024"}
        content = _build_content("electricity", meta)
        self.assertIn("2000", content)
        self.assertIn("2024", content)

    def test_includes_sub_routes(self):
        meta = {
            "description": "Y",
            "routes": [{"id": "sub1", "description": "Sub Dataset One"}],
        }
        content = _build_content("electricity", meta)
        self.assertIn("Sub Dataset One", content)

    def test_empty_meta_shows_fallback(self):
        content = _build_content("electricity", {})
        self.assertIn("EIA", content)


# ------------------------------------------------------------------
# eia_search — API 호출 전부 mock
# ------------------------------------------------------------------

MOCK_META = {
    "description": "Electricity Overview",
    "startPeriod": "2001-01",
    "endPeriod": "2024-11",
    "routes": [
        {"id": "retail-sales", "description": "Retail Sales"},
        {"id": "rto",          "description": "Regional Transmission"},
    ],
}


def _make_mock_response(json_data: dict, status: int = 200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = {"response": json_data}
    mock.raise_for_status = MagicMock()
    return mock


class EiaSearchTest(unittest.TestCase):
    def setUp(self):
        os.environ["EIA_API_KEY"] = "test-key-123"

    def tearDown(self):
        os.environ.pop("EIA_API_KEY", None)

    @patch("src.collectors.eia.httpx.get")
    def test_returns_document_list(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_META)
        docs = eia_search("electricity", limit=1)
        self.assertIsInstance(docs, list)
        self.assertTrue(len(docs) >= 1)

    @patch("src.collectors.eia.httpx.get")
    def test_document_fields(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_META)
        doc = eia_search("electricity", limit=1)[0]
        self.assertIsInstance(doc, Document)
        self.assertEqual(doc.source, "eia")
        self.assertEqual(doc.source_type, "stats")
        self.assertIn("eia.gov", doc.url)
        self.assertIn("Electricity", doc.title)

    @patch("src.collectors.eia.httpx.get")
    def test_published_from_end_period(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_META)
        doc = eia_search("electricity", limit=1)[0]
        self.assertEqual(doc.published, "2024-11")

    @patch("src.collectors.eia.httpx.get")
    def test_limit_respected(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_META)
        docs = eia_search("energy electricity coal", limit=2)
        self.assertLessEqual(len(docs), 2)

    @patch("src.collectors.eia.httpx.get")
    def test_api_failure_returns_document_with_fallback_content(self, mock_get):
        """API 호출 실패 시 빈 meta 폴백 — 예외 없이 Document 반환."""
        mock_get.side_effect = Exception("network error")
        docs = eia_search("electricity", limit=1)
        self.assertEqual(len(docs), 1)
        self.assertIn("EIA", docs[0].content)

    def test_missing_api_key_raises(self):
        os.environ.pop("EIA_API_KEY", None)
        with self.assertRaises(ValueError):
            eia_search("electricity")

    @patch("src.collectors.eia.httpx.get")
    def test_metadata_contains_route(self, mock_get):
        mock_get.return_value = _make_mock_response(MOCK_META)
        doc = eia_search("electricity", limit=1)[0]
        self.assertIn("route", doc.metadata)
        self.assertEqual(doc.metadata["route"], "electricity")


if __name__ == "__main__":
    unittest.main()
