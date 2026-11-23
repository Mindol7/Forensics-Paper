"""One writer at a time: the pipeline, the renumbering tool and the removal tool share a lock.

They all rewrite the same stores (manifests, profile index, profile tree).  A
monthly cron run that overlaps a manual ``python3 main.py -renumber`` would
leave numbers half rewritten, so the second process stops with a clear message
instead of starting.  The lock is an advisory ``flock`` on a file; it is
released by the operating system even if the process is killed.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parents[1] / "reports" / ".pipeline.lock"


class AlreadyRunningError(RuntimeError):
    """Another pipeline / renumber / removal process holds the lock."""


@contextmanager
def exclusive_run(what: str, path: Path | None = None) -> Iterator[None]:
    lock_path = path or LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = os.pread(descriptor, 200, 0).decode("utf-8", "replace").strip()
            except OSError:
                pass
            raise AlreadyRunningError(
                f"다른 작업이 실행 중이라 '{what}'을(를) 시작하지 않았습니다" + (f" (실행 중: {holder})" if holder else "")
                + ". 그 작업이 끝난 뒤 다시 실행하세요."
            ) from None
        os.ftruncate(descriptor, 0)
        os.pwrite(descriptor, f"{what} · pid {os.getpid()}".encode("utf-8"), 0)
        yield
    finally:
        os.close(descriptor)  # closing the descriptor releases the lock
