"""마스터 라이브러리 페이지 캐시 테스트.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.collectors.test_library -v

`_extract_pages`(pymupdf 호출)를 mock 하므로 실제 PDF/네트워크 없이
캐시 히트·무효화·페이지 선발 로직을 검증한다.
"""
import os
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.collectors import library


class CachedPagesTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.lib = Path(self._tmp.name)
        self.cache = self.lib / library.CACHE_DIRNAME
        self.pdf = self.lib / "iea" / "report.pdf"
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b"%PDF-1.4 fake")

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_read_extracts_and_writes_cache(self):
        with patch.object(library, "_extract_pages", return_value=["p1", "p2"]) as m:
            pages = library._cached_pages(self.pdf, self.cache, self.lib)
        self.assertEqual(pages, ["p1", "p2"])
        m.assert_called_once()
        # 캐시 파일 생성 + 상대경로 기반 키
        cache_file = self.cache / "iea_report.pdf.json"
        self.assertTrue(cache_file.exists())
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(data["pages"], ["p1", "p2"])

    def test_second_read_uses_cache_no_reextraction(self):
        with patch.object(library, "_extract_pages", return_value=["p1", "p2"]) as m:
            library._cached_pages(self.pdf, self.cache, self.lib)
            again = library._cached_pages(self.pdf, self.cache, self.lib)
        self.assertEqual(again, ["p1", "p2"])
        m.assert_called_once()  # 2회 호출했지만 추출은 1회 (캐시 히트)

    def test_mtime_change_invalidates_cache(self):
        with patch.object(library, "_extract_pages",
                          side_effect=[["old"], ["new"]]) as m:
            library._cached_pages(self.pdf, self.cache, self.lib)
            # PDF 수정 시각을 미래로 변경 → 캐시 무효
            st = self.pdf.stat()
            os.utime(self.pdf, (st.st_atime + 100, st.st_mtime + 100))
            pages = library._cached_pages(self.pdf, self.cache, self.lib)
        self.assertEqual(pages, ["new"])
        self.assertEqual(m.call_count, 2)

    def test_corrupt_cache_triggers_reextraction(self):
        cache_file = self.cache / "iea_report.pdf.json"
        self.cache.mkdir(parents=True)
        cache_file.write_text("{broken json", encoding="utf-8")
        with patch.object(library, "_extract_pages", return_value=["ok"]) as m:
            pages = library._cached_pages(self.pdf, self.cache, self.lib)
        self.assertEqual(pages, ["ok"])
        m.assert_called_once()

    def test_different_folder_same_name_no_collision(self):
        other = self.lib / "irena" / "report.pdf"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"%PDF other")
        with patch.object(library, "_extract_pages",
                          side_effect=[["iea"], ["irena"]]):
            a = library._cached_pages(self.pdf, self.cache, self.lib)
            b = library._cached_pages(other, self.cache, self.lib)
        self.assertEqual(a, ["iea"])
        self.assertEqual(b, ["irena"])  # 서로 다른 캐시 키


class SelectPagesTextTest(unittest.TestCase):

    def test_empty_pages(self):
        self.assertEqual(library._select_pages_text([], {"x"}), "")

    def test_no_terms_returns_all(self):
        self.assertEqual(library._select_pages_text(["a", "b"], set()), "a\nb")

    def test_short_doc_returns_all(self):
        pages = ["a", "b", "c"]  # <= ALWAYS_PAGES + 3
        self.assertEqual(library._select_pages_text(pages, {"x"}), "a\nb\nc")

    def test_long_doc_selects_scored_pages(self):
        # 앞 5p 항상 + 키워드 있는 페이지 선발
        pages = [f"page{i}" for i in range(10)]
        pages[8] = "관련 키워드 hydrogen 포함"
        out = library._select_pages_text(pages, {"hydrogen"})
        self.assertIn("hydrogen", out)
        self.assertIn("page0", out)  # executive summary


class LoadLibraryCacheIntegrationTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.lib = Path(self._tmp.name)
        (self.lib / "doc.pdf").write_bytes(b"%PDF fake")

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_library_caches_across_calls(self):
        with patch.object(library, "_extract_pages",
                          return_value=["hydrogen page"]) as m:
            d1 = library.load_library(self.lib, queries=["hydrogen"])
            d2 = library.load_library(self.lib, queries=["hydrogen"])
        self.assertEqual(len(d1), 1)
        self.assertEqual(len(d2), 1)
        m.assert_called_once()  # 두 번째 load 는 캐시 사용

    def test_cache_dir_contents_not_loaded_as_docs(self):
        with patch.object(library, "_extract_pages", return_value=["x"]):
            docs = library.load_library(self.lib, queries=["x"])
        # .cache/ 에 json 이 생겼어도 문서로 잡히지 않음 (pdf 1건만)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].source, "library")


if __name__ == "__main__":
    unittest.main(verbosity=2)
