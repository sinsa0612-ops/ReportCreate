"""동시 실행 가드 — slug 단위 pidfile lock.

같은 slug 의 파이프라인/검증 루프가 동시에 돌면 raw JSON·드래프트·manifest 를
서로 덮어쓴다. output/locks/{slug}.lock 에 PID 를 기록해 두 번째 실행을 차단한다.

- 락 보유 프로세스가 죽어 있으면(stale) 자동 회수한다.
- 같은 프로세스의 재진입(main --validate 가 내부에서 검증 루프 호출)은 허용하며,
  이때 락 해제는 바깥쪽 보유자가 한다.
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path("output/locks")


def _pid_alive(pid: int) -> bool:
    """해당 PID 프로세스가 살아 있는지 (Windows/POSIX 모두 지원)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def slug_lock(slug: str, lock_dir: Path = LOCK_DIR):
    """slug 락을 잡고 본문 실행, 종료 시 해제. 이미 실행 중이면 RuntimeError."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"{slug}.lock"

    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = -1
        if pid == os.getpid():
            yield          # 같은 프로세스의 재진입 — 바깥 보유자가 해제
            return
        if _pid_alive(pid):
            raise RuntimeError(
                f"'{slug}' 작업이 이미 실행 중입니다 (PID {pid}). "
                f"중복 실행은 출력 파일을 서로 덮어씁니다. 끝나기를 기다리거나, "
                f"멈춘 프로세스라면 {lock} 파일을 삭제한 뒤 다시 실행하세요.")
        lock.unlink()      # stale lock 회수

    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
