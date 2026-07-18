"""원문 저장 incremental 모드 테스트.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.collectors.test_fetch -v

gas_safety 소스는 content 가 수집 시점에 확보돼 httpx 를 타지 않으므로,
네트워크 없이 incremental 누적·skip 로직을 검증할 수 있다.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.collectors import fetch


def _raw(tmp: Path, docs: list[dict], name: str = "demo_2026-06-08.json") -> Path:
    p = tmp / name
    p.write_text(json.dumps({"documents": docs}, ensure_ascii=False), encoding="utf-8")
    return p


def _gas(url: str) -> dict:
    """gas_safety 문서(content 충분 → status ok, pre_fetched)."""
    return {"title": "수소 R&D 과제", "url": url, "source": "gas_safety",
            "source_type": "report",
            "content": "가스안전공사 수소 연료전지 R&D 과제 내용 메타데이터 " * 20}


class FetchIncrementalTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _manifest(self, out_dir: Path) -> list[dict]:
        return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

    def test_gas_safety_pre_fetched(self):
        raw = _raw(self.tmp, [_gas("odcloud://1")])
        out = fetch.fetch_sources(raw)
        man = self._manifest(out)
        self.assertEqual(len(man), 1)
        self.assertEqual(man[0]["kind"], "pre_fetched")
        self.assertEqual(man[0]["status"], "ok")
        self.assertEqual(man[0]["index"], 1)

    def test_default_out_dir_is_stem_sources(self):
        raw = _raw(self.tmp, [_gas("odcloud://1")])
        out = fetch.fetch_sources(raw)
        self.assertEqual(out.name, "demo_2026-06-08_sources")

    def test_incremental_skips_done_and_appends_with_continued_index(self):
        # 1차: U1 저장
        raw1 = _raw(self.tmp, [_gas("odcloud://1")])
        out = fetch.fetch_sources(raw1)
        # 2차: U1(기존)+U2(신규) 를 같은 폴더에 incremental
        raw2 = _raw(self.tmp, [_gas("odcloud://1"), _gas("odcloud://2")])
        fetch.fetch_sources(raw2, out_dir=out, incremental=True)
        man = self._manifest(out)
        self.assertEqual([m["url"] for m in man], ["odcloud://1", "odcloud://2"])
        self.assertEqual(man[1]["index"], 2)  # index 이어감(중복 방지)

    def test_custom_out_dir_with_empty_manifest_fetches_all(self):
        raw = _raw(self.tmp, [_gas("odcloud://1")])
        custom = self.tmp / "base_sources"
        fetch.fetch_sources(raw, out_dir=custom, incremental=True)
        self.assertEqual(len(self._manifest(custom)), 1)

    def test_non_incremental_overwrites_manifest(self):
        # incremental=False(기본)면 기존 manifest 무시하고 새로 작성
        raw1 = _raw(self.tmp, [_gas("odcloud://1")])
        out = fetch.fetch_sources(raw1)
        raw2 = _raw(self.tmp, [_gas("odcloud://2")])
        fetch.fetch_sources(raw2, out_dir=out)  # incremental=False
        man = self._manifest(out)
        self.assertEqual([m["url"] for m in man], ["odcloud://2"])  # U1 안 남음

    def test_only_urls_limits_fetch_targets(self):
        """only_urls 지정 시 그 집합의 문서만 저장하고 나머지는 manifest 에도 없다."""
        raw = _raw(self.tmp, [_gas("odcloud://1"), _gas("odcloud://2"),
                              _gas("odcloud://3")])
        out = fetch.fetch_sources(raw, only_urls={"odcloud://2"})
        man = self._manifest(out)
        self.assertEqual([m["url"] for m in man], ["odcloud://2"])

    def test_only_urls_none_fetches_all(self):
        """only_urls 미지정(기본)이면 전체 문서를 처리한다 (하위 호환)."""
        raw = _raw(self.tmp, [_gas("odcloud://1"), _gas("odcloud://2")])
        out = fetch.fetch_sources(raw)
        self.assertEqual(len(self._manifest(out)), 2)

    def test_only_urls_with_incremental_skips_done(self):
        """incremental + only_urls: 기존 처리분은 건너뛰고 지정 신규만 추가."""
        raw1 = _raw(self.tmp, [_gas("odcloud://1")])
        out = fetch.fetch_sources(raw1)
        raw2 = _raw(self.tmp, [_gas("odcloud://1"), _gas("odcloud://2"),
                               _gas("odcloud://3")])
        fetch.fetch_sources(raw2, out_dir=out, incremental=True,
                            only_urls={"odcloud://3"})
        man = self._manifest(out)
        self.assertEqual([m["url"] for m in man], ["odcloud://1", "odcloud://3"])


class BlockedPageTest(unittest.TestCase):
    """캡차/봇차단 페이지 검출 (21차 문제3)."""

    def test_captcha_page_detected(self):
        text = ("Are you a robot?\nPlease confirm you are a human by "
                "completing the captcha challenge below.\nReference number: abc123")
        self.assertTrue(fetch._is_blocked_page(text))

    def test_cloudflare_checking_detected(self):
        self.assertTrue(fetch._is_blocked_page(
            "Checking your browser before accessing example.com"))

    def test_normal_article_passes(self):
        text = ("수전해 기술의 LCOE는 2030년까지 USD 2/kg 이하로 하락할 전망이다. "
                "Robot-assisted manufacturing 사례도 있으나 차단 문구는 아니다.")
        self.assertFalse(fetch._is_blocked_page(text))

    def test_repeated_sizes_warning(self):
        """동일 크기 ok 문서 3건 이상이면 경고가 출력된다."""
        import io
        from contextlib import redirect_stdout
        manifest = [{"status": "ok", "chars": 2519} for _ in range(3)] + \
                   [{"status": "ok", "chars": 999}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            fetch._warn_repeated_sizes(manifest)
        self.assertIn("2,519", buf.getvalue())
        self.assertIn("의심", buf.getvalue())

    def test_repeated_sizes_no_warning_below_threshold(self):
        import io
        from contextlib import redirect_stdout
        manifest = [{"status": "ok", "chars": 2519} for _ in range(2)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            fetch._warn_repeated_sizes(manifest)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
