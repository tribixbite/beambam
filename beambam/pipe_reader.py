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


class NonBlockingLog:
    """A stderr logger that never blocks the caller.

    A parent that starts beambam with ``stderr=PIPE`` and only reads the pipe
    when the child exits (pokefit's camera proxy does) lets the OS pipe fill
    (4 KB on Windows); the next ``print(..., file=sys.stderr)`` then blocks
    forever. In `beambam camera` that write happened while holding the
    supervisor lock, so every HTTP request hung. Lines go into a bounded
    queue that a daemon thread writes out; when the queue is full a line is
    dropped (and counted) instead of blocking.

    Call it like ``print``: ``log("text", more, sep=" ")`` (``file=`` is
    accepted and ignored so call sites can be converted mechanically).
    """

    def __init__(self, stream: IO[str] | None = None, maxsize: int = 256) -> None:
        import sys
        self._stream = stream if stream is not None else sys.stderr
        self._q: queue.Queue[str] = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="nonblocking-log", daemon=True)
        self._thread.start()
        # The writer is a daemon thread, so lines queued just before exit (an
        # error printed before `return 2`) were lost. Give it a bounded moment
        # to finish at exit; bounded so a blocked stderr still cannot hang us.
        import atexit
        atexit.register(self.drain)

    def drain(self, timeout: float = 1.0) -> bool:
        """Wait up to `timeout` s for every queued line to be written.
        Returns True if the queue emptied in time."""
        import time
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._q.unfinished_tasks

    def __call__(self, *parts: object, sep: str = " ", end: str = "\n",
                 file: object = None, flush: bool = False) -> None:  # noqa: ARG002
        try:
            self._q.put_nowait(sep.join(str(p) for p in parts) + end)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            line = self._q.get()
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError):
                pass     # closed / broken stderr: nothing useful to do
            finally:
                self._q.task_done()
