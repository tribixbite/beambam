"""tests/test_pipe_reader.py — beambam.pipe_reader: portable pipe reads for the camera pump.

The camera's ffmpeg pump used `select.select()` on ffmpeg's stdout, which raises WinError 10038 on
Windows (select takes sockets only). These run a real subprocess pipe on every platform.
"""
from __future__ import annotations

import subprocess
import sys
import time

from beambam.pipe_reader import PipeReader, TailReader, split_jpeg_frames


def _proc(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_reader_times_out_then_delivers_partial_chunks_and_eof() -> None:
    # Writes 10 bytes, waits, writes 5 more: a plain read(65536) would block until EOF.
    p = _proc("import sys,time; o=sys.stdout.buffer; o.write(b'0123456789'); o.flush(); "
              "time.sleep(1.0); o.write(b'abcde'); o.flush()")
    assert p.stdout is not None
    r = PipeReader(p.stdout)
    t = time.time()
    first = None
    while first is None and time.time() - t < 10:
        first = r.get(timeout=0.2)
    assert first == b"0123456789"           # the available bytes, not a full chunk
    assert time.time() - t < 0.9            # before the second write
    got = b""
    while True:
        c = r.get(timeout=5)
        assert c is not None
        if not c:
            break
        got += c
    assert got == b"abcde"
    assert r.get(timeout=0.1) == b""        # EOF stays EOF
    p.wait(5)


def test_get_returns_none_while_the_pipe_is_quiet() -> None:
    p = _proc("import time; time.sleep(2)")
    assert p.stdout is not None
    r = PipeReader(p.stdout)
    t = time.time()
    assert r.get(timeout=0.3) is None
    assert time.time() - t < 1.5
    p.kill()
    p.wait(5)


def test_tail_reader_drains_and_keeps_the_end() -> None:
    # 200 KB on stderr: undrained, it would fill the pipe and block the child.
    p = _proc("import sys; sys.stderr.buffer.write(b'x' * 200000 + b'THE END'); sys.stderr.flush()")
    assert p.stderr is not None
    tail = TailReader(p.stderr, keep=1000)
    assert p.wait(10) == 0
    assert tail.tail(7, wait=5) == "THE END"


def test_split_jpeg_frames_keeps_the_latest_complete_frame() -> None:
    a = b"\xff\xd8" + b"A" * 10 + b"\xff\xd9"
    b = b"\xff\xd8" + b"B" * 10 + b"\xff\xd9"
    frame, rest = split_jpeg_frames(a)                  # no next SOI yet: not known complete
    assert frame is None and rest == a
    frame, rest = split_jpeg_frames(a + b + b"\xff\xd8CC")
    assert frame == b and rest == b"\xff\xd8CC"
    frame, rest = split_jpeg_frames(b"\xff\xd8broken" + b"\xff\xd8x")   # no EOI: dropped
    assert frame is None and rest == b"\xff\xd8x"
