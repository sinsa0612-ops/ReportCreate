"""국회도서관 API 어댑터 단위 테스트 — 외부 네트워크·API 키 불필요 (전부 mock).

실행:
    .venv\\Scripts\\python.exe -m unittest tests.collectors.test_nalib -v
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from src.collectors.gov_kr import nalib_search, _nalib_detail_fields
from src.models import Document


# ── 공용 XML 픽스처 ──────────────────────────────────────────────────────────

def _xml(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>' + body).encode("utf-8")


BASIC_XML_ONE = _xml("""
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL_CODE</resultMsg></header>
  <total>42</total>
  <record>
    <item><value>KINX2024001234</value><name>controlno</name></item>
    <item><value>수소연료전지 기술 현황 /홍길동,이순신</value>
          <name>자료명/저자사항</name></item>
    <item><value>서울:에너지경제연구원,2024</value><name>출판사항</name></item>
  </record>
</response>""")

DETAIL_XML_WITH_SOURCE = _xml("""
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL_CODE</resultMsg></header>
  <item><value>수소연료전지 기술 현황 /홍길동,이순신</value>
        <name>자료명/저자사항</name></item>
  <item><value>서울:에너지경제연구원,2024</value><name>출판사항</name></item>
  <item><value>연구보고서</value><name>자료유형</name></item>
  <item><value>수소에너지,연료전지,SOFC</value><name>주제명</name></item>
  <item><value>https://www.prism.go.kr/homepage/researchsearch/brm/retrieveBrm.do?detail_id=1234-2024</value>
        <name>출처</name></item>
  <item><value>Y</value><name>공개</name></item>
  <item><value>인터넷자료</value><name>DB</name></item>
</response>""")

DETAIL_XML_NO_SOURCE = _xml("""
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL_CODE</resultMsg></header>
  <item><value>인터넷자료</value><name>DB</name></item>
  <item><value>Y</value><name>공개</name></item>
</response>""")

BASIC_XML_EMPTY = _xml("""
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL_CODE</resultMsg></header>
  <total>0</total>
</response>""")

BASIC_XML_ERROR = _xml("""
<response>
  <header><resultCode>30</resultCode><resultMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</resultMsg></header>
</response>""")

BASIC_XML_TWO = _xml("""
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL_CODE</resultMsg></header>
  <total>2</total>
  <record>
    <item><value>CN0001</value><name>controlno</name></item>
    <item><value>태양광 발전 기술 /저자A</value>
          <name>자료명/저자사항</name></item>
    <item><value>서울:기관A,2022</value><name>출판사항</name></item>
  </record>
  <record>
    <item><value>CN0002</value><name>controlno</name></item>
    <item><value>풍력 발전 동향 /저자B</value>
          <name>자료명/저자사항</name></item>
    <item><value>서울:기관B,2025</value><name>출판사항</name></item>
  </record>
</response>""")


def _mock_response(content: bytes, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.content = content
    m.raise_for_status = MagicMock()
    return m


# ── _nalib_detail_fields ─────────────────────────────────────────────────────

class NalibDetailFieldsTest(unittest.TestCase):

    @patch("src.collectors.gov_kr.httpx.get")
    def test_returns_field_dict(self, mock_get):
        mock_get.return_value = _mock_response(DETAIL_XML_WITH_SOURCE)
        fields = _nalib_detail_fields("KEY", "KINX2024001234")
        self.assertIn("자료명/저자사항", fields)
        self.assertIn("공개", fields)
        self.assertEqual(fields["공개"], "Y")

    @patch("src.collectors.gov_kr.httpx.get")
    def test_pops_source_url(self, mock_get):
        """출처 필드는 별도로 팝되어 반환 dict에서 제거된다."""
        mock_get.return_value = _mock_response(DETAIL_XML_WITH_SOURCE)
        fields = _nalib_detail_fields("KEY", "KINX2024001234")
        # _nalib_detail_fields는 출처를 pop하지 않음 — nalib_search에서 pop
        self.assertIn("출처", fields)

    @patch("src.collectors.gov_kr.httpx.get")
    def test_http_error_returns_empty(self, mock_get):
        mock_get.side_effect = Exception("connection timeout")
        fields = _nalib_detail_fields("KEY", "BAD")
        self.assertEqual(fields, {})


# ── nalib_search 기본 동작 ───────────────────────────────────────────────────

class NalibSearchBasicTest(unittest.TestCase):

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_returns_document_list(self, mock_get):
        """1건 결과 + detail 성공 → Document 1건 반환."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),         # basic search
            _mock_response(DETAIL_XML_WITH_SOURCE), # detail
        ]
        docs = nalib_search("수소 연료전지", limit=5)
        self.assertEqual(len(docs), 1)

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_document_fields(self, mock_get):
        """Document의 source, source_type, title, published 확인."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),
            _mock_response(DETAIL_XML_WITH_SOURCE),
        ]
        doc = nalib_search("수소", limit=5)[0]
        self.assertEqual(doc.source, "nalib")
        self.assertEqual(doc.source_type, "report")
        self.assertIn("수소연료전지", doc.title)
        self.assertEqual(doc.published, "2024-01-01")

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_source_url_used_when_present(self, mock_get):
        """detail 의 출처 URL이 Document.url로 사용된다."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),
            _mock_response(DETAIL_XML_WITH_SOURCE),
        ]
        doc = nalib_search("수소", limit=5)[0]
        self.assertIn("prism.go.kr", doc.url)

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_catalog_url_fallback_when_no_source(self, mock_get):
        """detail 에 출처가 없으면 디지털도서관 목록 URL로 폴백."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),
            _mock_response(DETAIL_XML_NO_SOURCE),
        ]
        doc = nalib_search("수소", limit=5)[0]
        self.assertIn("nanet.go.kr", doc.url)
        self.assertIn("KINX2024001234", doc.url)

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_catalog_url_fallback_when_detail_fails(self, mock_get):
        """detail HTTP 오류 시에도 Document가 반환되고 목록 URL 사용."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),
            Exception("timeout"),
        ]
        docs = nalib_search("수소", limit=5)
        self.assertEqual(len(docs), 1)
        self.assertIn("nanet.go.kr", docs[0].url)

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_empty_result_returns_empty_list(self, mock_get):
        mock_get.return_value = _mock_response(BASIC_XML_EMPTY)
        docs = nalib_search("존재하지않는쿼리xyz", limit=5)
        self.assertEqual(docs, [])

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_error_result_code_returns_empty_list(self, mock_get):
        mock_get.return_value = _mock_response(BASIC_XML_ERROR)
        docs = nalib_search("수소", limit=5)
        self.assertEqual(docs, [])

    def test_missing_key_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "DATA_GO_KR_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                nalib_search("수소")


# ── nalib_search 복수 결과 ───────────────────────────────────────────────────

class NalibSearchMultipleTest(unittest.TestCase):

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_two_records(self, mock_get):
        """2건 basic 결과 → detail 2회 호출 → Document 2건."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_TWO),
            _mock_response(DETAIL_XML_NO_SOURCE),  # CN0001 detail
            _mock_response(DETAIL_XML_NO_SOURCE),  # CN0002 detail
        ]
        docs = nalib_search("재생에너지", limit=5)
        self.assertEqual(len(docs), 2)
        self.assertEqual(mock_get.call_count, 3)  # basic 1 + detail 2

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_published_year_parsed(self, mock_get):
        """출판사항에서 연도 파싱 확인."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_TWO),
            _mock_response(DETAIL_XML_NO_SOURCE),
            _mock_response(DETAIL_XML_NO_SOURCE),
        ]
        docs = nalib_search("에너지", limit=5)
        self.assertEqual(docs[0].published, "2022-01-01")
        self.assertEqual(docs[1].published, "2025-01-01")

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_controlno_in_metadata(self, mock_get):
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),
            _mock_response(DETAIL_XML_WITH_SOURCE),
        ]
        doc = nalib_search("수소", limit=5)[0]
        self.assertEqual(doc.metadata["controlno"], "KINX2024001234")

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_content_includes_detail_fields(self, mock_get):
        """detail 필드(주제명 등)가 content에 포함된다."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_ONE),
            _mock_response(DETAIL_XML_WITH_SOURCE),
        ]
        doc = nalib_search("수소", limit=5)[0]
        self.assertIn("주제명", doc.content)
        self.assertIn("수소에너지", doc.content)

    @patch.dict(os.environ, {"DATA_GO_KR_KEY": "TESTKEY"})
    @patch("src.collectors.gov_kr.httpx.get")
    def test_limit_caps_displaylines(self, mock_get):
        """limit > 10 은 10으로 클램핑 (API 최대값)."""
        mock_get.side_effect = [
            _mock_response(BASIC_XML_EMPTY),
        ]
        nalib_search("수소", limit=20)
        call_params = mock_get.call_args[1]["params"]
        self.assertEqual(call_params["displaylines"], "10")


if __name__ == "__main__":
    unittest.main()
