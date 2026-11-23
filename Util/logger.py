"""Small, thread-safe terminal progress logger.

Collectors deliberately depend on this module instead of configuring Python's
global logging state.  That keeps CLI output immediate (``flush=True``), makes
parallel worker messages readable, and lets tests inject an in-memory stream.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    component: str
    message: str
    current: int | None = None
    total: int | None = None
    elapsed_seconds: float | None = None
    counters: Mapping[str, int] | None = None


class ProgressLogger:
    """Render progress events as one immediately flushed terminal line."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._stream = stream or sys.stdout
        self._clock = clock
        self._now = now
        self._started_at = clock()
        self._lock = threading.Lock()

    def emit(
        self,
        component: str,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        counters: Mapping[str, int] | None = None,
    ) -> ProgressEvent:
        if current is not None and current < 0:
            raise ValueError("current must be non-negative")
        if total is not None and total < 0:
            raise ValueError("total must be non-negative")
        if current is not None and total is not None and current > total:
            raise ValueError("current cannot exceed total")

        elapsed = max(0.0, self._clock() - self._started_at)
        event = ProgressEvent(
            component=str(component),
            message=str(message),
            current=current,
            total=total,
            elapsed_seconds=elapsed,
            counters=dict(counters) if counters else None,
        )
        timestamp = self._now().strftime("%Y-%m-%d %H:%M:%S")
        segments = [f"[{timestamp}]", f"[{event.component}]", event.message]
        if current is not None:
            progress = str(current)
            if total is not None:
                progress = f"{current}/{total}"
                if total:
                    progress += f" ({current / total:.1%})"
            segments.append(f"progress={progress}")
        if counters:
            segments.extend(f"{key}={value}" for key, value in sorted(counters.items()))
        segments.append(f"elapsed={elapsed:.1f}s")
        with self._lock:
            print(" ".join(segments), file=self._stream, flush=True)
        return event

    def metadata(
        self,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        counters: Mapping[str, int] | None = None,
    ) -> ProgressEvent:
        return self.emit(
            "KCI-METADATA",
            message,
            current=current,
            total=total,
            counters=counters,
        )

    def pdf(
        self,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        counters: Mapping[str, int] | None = None,
    ) -> ProgressEvent:
        return self.emit(
            "KCI-PDF",
            message,
            current=current,
            total=total,
            counters=counters,
        )


_DEFAULT_LOGGER = ProgressLogger()


def log(step: str, message: str) -> None:
    """Compatibility-friendly two-argument log function used by collectors."""

    _DEFAULT_LOGGER.emit(step, message)


def log_progress(
    component: str,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
    counters: Mapping[str, int] | None = None,
) -> ProgressEvent:
    return _DEFAULT_LOGGER.emit(
        component,
        message,
        current=current,
        total=total,
        counters=counters,
    )
