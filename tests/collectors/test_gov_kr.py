"""gov_kr 어댑터 테스트 — gas_safety 강신호 매칭 (네트워크 전부 mock).

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.collectors.test_gov_kr -v
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from src.collectors import gov_kr


def _mock_response(items: list[dict]):
    mock = MagicMock()
    mock.json.return_value = {"data": items}
    mock.raise_for_status = MagicMock()
    return mock


PROJECTS = [
    {"사업명": "수소 충전소 안전 기준 개발", "분야": "수소 안전", "수행기관": "가스안전공사"},
    {"사업명": "수소 배관 모니터링", "분야": "수소 인프라", "수행기관": "KIER"},
]


class GasSafetyStrongTermTest(unittest.TestCase):
    """범용어만으로는 매칭되지 않아야 한다 — 태양전지 보고서에 수소 R&D 과제가
    S급으로 침투하던 노이즈의 회귀 테스트 (평가 보고서 H1)."""

    def setUp(self):
        os.environ["DATA_GO_KR_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("DATA_GO_KR_KEY", None)

    def test_generic_only_query_returns_empty_without_http(self):
        """'기술'·'동향' 같은 범용어뿐인 쿼리는 HTTP 호출 없이 빈 결과."""
        with patch.object(gov_kr.httpx, "get") as mock_get:
            out = gov_kr.gas_safety_search("기술 동향 시장 분석")
        self.assertEqual(out, [])
        mock_get.assert_not_called()

    def test_unrelated_strong_term_no_match(self):
        """강신호가 있어도 과제와 무관하면(태양전지) 매칭 0건."""
        with patch.object(gov_kr.httpx, "get",
                          return_value=_mock_response(PROJECTS)):
            out = gov_kr.gas_safety_search("페로브스카이트 태양전지 기술")
        self.assertEqual(out, [])

    def test_relevant_strong_term_matches(self):
        """주제 강신호(수소)가 과제와 겹치면 정상 수집."""
        with patch.object(gov_kr.httpx, "get",
                          return_value=_mock_response(PROJECTS)):
            out = gov_kr.gas_safety_search("수소 안전 기준")
        self.assertEqual(len(out), 2)
        self.assertTrue(all(d.source == "gas_safety" for d in out))

    def test_generic_terms_excluded_from_matching(self):
        """'기술' 범용어는 매칭 어절에서 빠진다 — '수소'만으로 판정."""
        with patch.object(gov_kr.httpx, "get",
                          return_value=_mock_response(
                              [{"사업명": "신재생 기술 로드맵", "분야": "정책",
                                "수행기관": "X"}])):
            out = gov_kr.gas_safety_search("수소 기술")
        self.assertEqual(out, [])  # '기술'만 겹치고 '수소'는 없음 → 제외

    def test_missing_key_raises(self):
        os.environ.pop("DATA_GO_KR_KEY", None)
        with self.assertRaises(ValueError):
            gov_kr.gas_safety_search("수소")


if __name__ == "__main__":
    unittest.main(verbosity=2)
