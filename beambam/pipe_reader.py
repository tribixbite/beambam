"""beambam.pipe_reader — portable, stoppable reads from a subprocess pipe.

`select.select()` on a pipe only works on POSIX: on Windows it accepts sockets only and raises
`OSError: [WinError 10038]`, which killed `beambam camera`'s ffmpeg pump on every Windows run.
`PipeReader` instead reads in a daemon thread into a queue, so the consumer can wait with a
timeout (and check its stop events) on every platform.

Also here: `split_jpeg_frames`, the MJPEG (`-f image2pipe`) frame splitter the camera pump uses.
"""
from __future__ import annotations

import collections
import queue
import threading
from typing import IO

CHUNK = 65536


class PipeReader:
    """Read `stream` in a background thread; `get(timeout)` returns the next chunk, `b""` once
    at EOF, or None when nothing arrived within `timeout` seconds.

    Uses `read1` where the stream has it, so a chunk is whatever is available (up to `CHUNK`)
    rather than a full `CHUNK`: a plain `read(65536)` on a buffered pipe waits for 64 KB,
    which holds back camera frames.
    """

    def __init__(self, stream: IO[bytes], chunk: int = CHUNK, name: str = "pipe-reader") -> None:
        self._q: queue.Queue[bytes] = queue.Queue()
        self._stream = stream
        self._chunk = chunk
        self.eof = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        try:
            while True:
                data = read(self._chunk)
                if not data:
                    break
                self._q.put(data)
        except (OSError, ValueError):     # pipe closed under us (process killed)
            pass
        finally:
            self._q.put(b"")

    def get(self, timeout: float) -> bytes | None:
        if self.eof:
            return b""
        try:
            data = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if not data:
            self.eof = True
        return data


class TailReader:
    """Drain `stream` in a background thread, keeping only its last `keep` bytes (e.g. ffmpeg's
    stderr: unread, a full pipe would block the process)."""

    def __init__(self, stream: IO[bytes], keep: int = 4000, name: str = "pipe-tail") -> None:
        self._buf: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._keep = keep
        self._lock = threading.Lock()
        self._stream = stream
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        try:
            while True:
                data = read(CHUNK)
                if not data:
                    return
                with self._lock:
                    self._buf.append(data)
                    self._size += len(data)
                    while self._size - len(self._buf[0]) >= self._keep and len(self._buf) > 1:
                        self._size -= len(self._buf.popleft())
        except (OSError, ValueError):
            return

    def tail(self, n: int = 400, wait: float = 0.0) -> str:
        """The last `n` bytes read, decoded; `wait` seconds for the stream to finish first."""
        if wait:
            self._thread.join(wait)
        with self._lock:
            return b"".join(self._buf)[-n:].decode(errors="replace")


def split_jpeg_frames(buf: bytes) -> tuple[bytes | None, bytes]:
    """(latest complete JPEG in `buf`, unconsumed rest) for back-to-back JPEGs (MJPEG image2pipe).

    A frame is complete once the next frame's SOI marker (FFD8) has arrived; it must start with
    SOI and end with EOI (FFD9). The rest begins at the last SOI seen, so call again with more data.
    """
    latest = None
    while True:
        idx = buf.find(b"\xff\xd8", 1)
        if idx == -1:
            return latest, buf
        frame, buf = buf[:idx], buf[idx:]
        if frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"):
            latest = frame
