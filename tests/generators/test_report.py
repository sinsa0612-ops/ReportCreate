"""보고서 자동 생성 테스트 — claude -p 서브프로세스 래퍼 검증.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.generators.test_report -v

모든 테스트는 mock 기반 — 실제 claude CLI 를 호출하지 않는다.
RAW_DIR/REPORT_DIR 모듈 상수를 임시 디렉터리로 교체해 파일시스템을 격리한다.
"""
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from src.generators import report


def _write_raw(raw_dir: Path, slug: str,
               date: str = "2026-06-07") -> Path:
    """테스트용 output/raw/{slug}_{date}.json 한 건 생성하고 경로 반환."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{slug}_{date}.json"
    payload = {
        "queries": ["연료전지 신뢰성 평가"],
        "collected_at": f"{date}T00:00:00",
        "count": 1,
        "documents": [
            {
                "title": "연료전지 신뢰성 평가",
                "url": "http://example/1",
                "content": "연료전지 시스템 신뢰성 평가 연구 내용.",
                "source": "kosis",
                "source_type": "stats",
                "trust_grade": "S",
                "trust_score": 92.0,
                "metadata": {},
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class GenerateReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.raw_dir = tmp / "raw"
        self.report_dir = tmp / "reports"
        self._p_raw = patch.object(report, "RAW_DIR", self.raw_dir)
        self._p_rep = patch.object(report, "REPORT_DIR", self.report_dir)
        self._p_raw.start()
        self._p_rep.start()

    def tearDown(self):
        self._p_raw.stop()
        self._p_rep.stop()
        self._tmp.cleanup()

    # AC-01 ─ claude CLI 가 PATH 에 없으면 RuntimeError
    def test_missing_claude_raises_runtimeerror(self):
        _write_raw(self.raw_dir, "fuel")
        with patch.object(report.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                report.generate_report("fuel")

    # AC-02 ─ raw JSON 이 없으면 FileNotFoundError
    def test_missing_raw_raises_filenotfound(self):
        with patch.object(report.shutil, "which", return_value="claude"):
            with self.assertRaises(FileNotFoundError):
                report.generate_report("nonexistent-slug")

    # AC-03 ─ dry_run=True 면 subprocess 를 호출하지 않는다
    def test_dry_run_skips_subprocess(self):
        _write_raw(self.raw_dir, "fuel")
        with patch.object(report.shutil, "which", return_value="claude"), \
             patch.object(report.subprocess, "run") as mock_run:
            result = report.generate_report("fuel", dry_run=True)
            mock_run.assert_not_called()
        self.assertIsInstance(result, Path)

    # AC-04 ─ 정상 실행 시 .md 파일이 생성되고 stdout 내용이 기록된다
    def test_success_writes_markdown(self):
        _write_raw(self.raw_dir, "fuel")

        def fake_run(*args, **kwargs):
            # generate_report는 stdout=파일객체로 subprocess를 호출하므로
            # 실제 파일 쓰기를 흉내낸다.
            out_f = kwargs.get("stdout")
            if out_f is not None:
                out_f.write("# 보고서\n\n본문 내용")
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            return m

        with patch.object(report.shutil, "which", return_value="claude"), \
             patch.object(report.subprocess, "run", side_effect=fake_run):
            out = report.generate_report("fuel")
        self.assertTrue(out.exists())
        self.assertIn("보고서", out.read_text(encoding="utf-8"))

    # AC-05 ─ exit code != 0 이면 RuntimeError
    def test_nonzero_exit_raises_runtimeerror(self):
        _write_raw(self.raw_dir, "fuel")
        fake = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch.object(report.shutil, "which", return_value="claude"), \
             patch.object(report.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError):
                report.generate_report("fuel")

    # AC-06 ─ stdout 이 공백뿐이면 RuntimeError
    def test_empty_stdout_raises_runtimeerror(self):
        _write_raw(self.raw_dir, "fuel")
        fake = MagicMock(returncode=0, stdout="   \n", stderr="")
        with patch.object(report.shutil, "which", return_value="claude"), \
             patch.object(report.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError):
                report.generate_report("fuel")

    # AC-07 ─ 타임아웃은 TimeoutExpired 그대로 전파
    def test_timeout_propagates(self):
        _write_raw(self.raw_dir, "fuel")
        with patch.object(report.shutil, "which", return_value="claude"), \
             patch.object(report.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(
                              cmd="claude", timeout=1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                report.generate_report("fuel")


class FindRawTest(unittest.TestCase):
    """slug 로 가장 최근 raw JSON 을 고르는 보조 로직 검증."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.raw_dir = Path(self._tmp.name) / "raw"
        self._p_raw = patch.object(report, "RAW_DIR", self.raw_dir)
        self._p_raw.start()

    def tearDown(self):
        self._p_raw.stop()
        self._tmp.cleanup()

    def test_picks_latest_by_filename(self):
        """동일 slug 의 날짜별 파일 중 최신 날짜를 고른다."""
        _write_raw(self.raw_dir, "fuel", date="2026-06-05")
        newest = _write_raw(self.raw_dir, "fuel", date="2026-06-07")
        chosen = report.find_raw("fuel")
        self.assertEqual(chosen, newest)


class FindSourcesDirFallbackTest(unittest.TestCase):
    """검증본(_v{n}.json)은 전용 _sources 폴더가 없으면 base _sources 로 폴백한다."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_direct_sources_dir(self):
        (self.d / "demo_2026-06-08_sources").mkdir()
        raw = self.d / "demo_2026-06-08.json"
        self.assertEqual(report._find_sources_dir(raw).name, "demo_2026-06-08_sources")

    def test_versioned_falls_back_to_base(self):
        # _v2 전용 폴더 없음 → base _sources 로 폴백
        (self.d / "demo_2026-06-08_sources").mkdir()
        raw = self.d / "demo_2026-06-08_v2.json"
        self.assertEqual(report._find_sources_dir(raw).name, "demo_2026-06-08_sources")

    def test_versioned_prefers_own_sources_if_present(self):
        (self.d / "demo_2026-06-08_v2_sources").mkdir()
        (self.d / "demo_2026-06-08_sources").mkdir()
        raw = self.d / "demo_2026-06-08_v2.json"
        self.assertEqual(report._find_sources_dir(raw).name, "demo_2026-06-08_v2_sources")

    def test_none_when_no_sources_dir(self):
        raw = self.d / "demo_2026-06-08.json"
        self.assertIsNone(report._find_sources_dir(raw))


class ExtractQuantSentencesTest(unittest.TestCase):
    """18차 A5 — 개요용 '수치 미리보기' 추출(프로그래밍, LLM 미사용)."""

    def test_picks_quant_sentences_only(self):
        text = ("이 문단은 서론으로 수치가 없는 일반 설명입니다. "
                "전해조 효율은 74.4 % 수준으로 보고되었습니다. "
                "마무리 문장도 수치가 없습니다.")
        out = report.extract_quant_sentences(text)
        self.assertIn("74.4", out)
        self.assertNotIn("서론", out)

    def test_empty_when_no_quant(self):
        self.assertEqual(
            report.extract_quant_sentences("수치가 전혀 없는 텍스트입니다."), "")

    def test_respects_max_chars(self):
        text = "비용은 100 달러입니다. " * 50
        out = report.extract_quant_sentences(text, max_sentences=10, max_chars=100)
        self.assertLessEqual(len(out), 110)   # 구분자 여유 포함


if __name__ == "__main__":
    unittest.main(verbosity=2)
