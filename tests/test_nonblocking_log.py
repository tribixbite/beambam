"""NonBlockingLog never blocks its caller, even when stderr is a full, unread pipe."""
from __future__ import annotations

import threading
import time

from beambam.pipe_reader import NonBlockingLog


class _StuckStream:
    """A stream whose write() blocks until released (a full pipe nobody reads)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lines: list[str] = []

    def write(self, s: str) -> int:
        self.release.wait()
        self.lines.append(s)
        return len(s)

    def flush(self) -> None:
        pass


def test_log_calls_return_while_stream_is_stuck() -> None:
    stream = _StuckStream()
    log = NonBlockingLog(stream, maxsize=8)   # type: ignore[arg-type]
    t0 = time.monotonic()
    for i in range(100):
        log("[camera] line", i, file=None)
    assert time.monotonic() - t0 < 0.5        # never waited on the stream
    assert log.dropped >= 100 - 8 - 1          # overflow dropped, not blocked
    stream.release.set()
    deadline = time.monotonic() + 2
    while not stream.lines and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream.lines[0] == "[camera] line 0\n"
