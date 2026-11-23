from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from Util.run_lock import AlreadyRunningError, exclusive_run


def _hold(path: str, ready, release) -> None:
    with exclusive_run("cron 실행", Path(path)):
        ready.set()
        release.wait(20)


def test_a_second_writer_is_refused_while_the_first_is_running(tmp_path: Path) -> None:
    lock = tmp_path / "reports" / ".pipeline.lock"
    context = mp.get_context("spawn")
    ready, release = context.Event(), context.Event()
    holder = context.Process(target=_hold, args=(str(lock), ready, release))
    holder.start()
    try:
        assert ready.wait(20)
        with pytest.raises(AlreadyRunningError, match="cron 실행"):
            with exclusive_run("논문 번호 정렬", lock):
                raise AssertionError("two writers at once")
    finally:
        release.set()
        holder.join(20)
    # Released when the holder ends (also if it is killed: the OS drops an flock with the process).
    with exclusive_run("논문 번호 정렬", lock):
        pass
