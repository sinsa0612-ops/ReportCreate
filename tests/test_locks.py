"""동시 실행 가드(slug_lock) 테스트.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.test_locks -v
"""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import locks


class SlugLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _lock_file(self, slug="demo") -> Path:
        return self.dir / f"{slug}.lock"

    def test_acquire_and_release(self):
        with locks.slug_lock("demo", lock_dir=self.dir):
            self.assertEqual(self._lock_file().read_text(encoding="utf-8"),
                             str(os.getpid()))
        self.assertFalse(self._lock_file().exists())  # 종료 시 해제

    def test_conflict_with_alive_process_raises(self):
        self._lock_file().write_text("99999", encoding="utf-8")
        with patch.object(locks, "_pid_alive", return_value=True):
            with self.assertRaises(RuntimeError):
                with locks.slug_lock("demo", lock_dir=self.dir):
                    pass
        # 실패해도 기존 락은 보존 (남의 락을 지우면 안 됨)
        self.assertTrue(self._lock_file().exists())

    def test_stale_lock_reclaimed(self):
        """죽은 프로세스의 락은 자동 회수하고 진행한다."""
        self._lock_file().write_text("99999", encoding="utf-8")
        with patch.object(locks, "_pid_alive", return_value=False):
            with locks.slug_lock("demo", lock_dir=self.dir):
                self.assertEqual(self._lock_file().read_text(encoding="utf-8"),
                                 str(os.getpid()))

    def test_reentrant_same_process(self):
        """같은 프로세스의 재진입(main --validate → 검증 루프)은 허용."""
        with locks.slug_lock("demo", lock_dir=self.dir):
            with locks.slug_lock("demo", lock_dir=self.dir):
                pass
            # 안쪽 블록이 끝나도 락은 바깥 보유자 소유로 유지
            self.assertTrue(self._lock_file().exists())
        self.assertFalse(self._lock_file().exists())

    def test_corrupt_lock_treated_as_stale(self):
        """PID 가 아닌 내용의 락 파일은 stale 로 간주하고 회수한다."""
        self._lock_file().write_text("내용깨짐", encoding="utf-8")
        with locks.slug_lock("demo", lock_dir=self.dir):
            pass
        self.assertFalse(self._lock_file().exists())

    def test_exception_inside_releases_lock(self):
        with self.assertRaises(ValueError):
            with locks.slug_lock("demo", lock_dir=self.dir):
                raise ValueError("작업 실패")
        self.assertFalse(self._lock_file().exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
