"""beambam.stl_normals: a binary STL's zero facet normals are computed before slicing (Bambu Studio refuses the
file otherwise: code -6, seen on the phone build)."""
from __future__ import annotations

import struct
from pathlib import Path

from beambam.stl_normals import FACET, is_binary_stl, with_normals

TRI = [(0, 0, 0), (10, 0, 0), (0, 10, 0)]           # in the z = 0 plane, counter-clockwise from +z


def _binary(path: Path, normal: tuple[float, float, float], count: int = 2) -> Path:
    body = b"".join(FACET.pack(*normal, *(c for v in TRI for c in v), 0) for _ in range(count))
    path.write_bytes(b"solid but binary".ljust(80, b" ") + struct.pack("<I", count) + body)
    return path


def test_zero_normals_are_computed_from_the_vertices(tmp_path):
    src = _binary(tmp_path / "zero.stl", (0.0, 0.0, 0.0))
    out = with_normals(src, tmp_path / "out")
    assert out != src and src.read_bytes()[84:96] == b"\0" * 12          # the user's file is untouched
    for i in range(2):
        assert FACET.unpack_from(out.read_bytes(), 84 + i * FACET.size)[:3] == (0.0, 0.0, 1.0)
    assert out.stat().st_size == src.stat().st_size and is_binary_stl(out)


def test_files_that_need_nothing_are_used_as_they_are(tmp_path):
    good = _binary(tmp_path / "good.stl", (0.0, 0.0, 1.0))
    assert with_normals(good, tmp_path / "out") == good
    ascii_stl = tmp_path / "a.stl"
    ascii_stl.write_text("solid a\nendsolid a\n")
    assert not is_binary_stl(ascii_stl) and with_normals(ascii_stl, tmp_path / "out") == ascii_stl
    short = tmp_path / "short.stl"
    short.write_bytes(_binary(tmp_path / "x.stl", (0.0, 0.0, 0.0)).read_bytes()[:-10])   # truncated: not ours to fix
    assert with_normals(short, tmp_path / "out") == short
    assert not (tmp_path / "out").exists()
