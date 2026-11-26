"""beambam.stl_normals — give a binary STL's facets the normals Bambu Studio needs.

The STL format lets a facet's normal be (0, 0, 0) ("compute it from the vertices"), and some exporters write
that. The phone's Bambu Studio build refuses such a binary STL outright ("The input model file to the slicer can
not be parsed", code -6) while it reads the same mesh as ASCII. `with_normals` writes a copy whose zero normals
are the facets' own (right-hand rule over the vertex order, unit length); a file whose normals are all set, an
ASCII STL, or anything that is not a well-formed binary STL is returned as it is.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

HEADER = 80
FACET = struct.Struct("<12fH")             # normal xyz, 3 vertices xyz, attribute byte count: 50 bytes


def is_binary_stl(path: Path) -> bool:
    """A binary STL: 80-byte header, a facet count, and exactly that many 50-byte facets (an ASCII STL can start
    with "solid" in a binary header too, so the size decides)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(HEADER + 4)
    except OSError:
        return False
    if len(head) < HEADER + 4:
        return False
    (n,) = struct.unpack_from("<I", head, HEADER)
    return size == HEADER + 4 + n * FACET.size


def _normal(v: tuple[float, ...]) -> tuple[float, float, float]:
    ax, ay, az = v[3] - v[0], v[4] - v[1], v[5] - v[2]
    bx, by, bz = v[6] - v[0], v[7] - v[1], v[8] - v[2]
    nx, ny, nz = ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (nx / length, ny / length, nz / length) if length > 0 else (0.0, 0.0, 0.0)


def with_normals(src: Path, out_dir: Path) -> Path:
    """`src`, or a copy in `out_dir` with every zero facet normal computed from its vertices (see module doc)."""
    if src.suffix.lower() != ".stl" or not is_binary_stl(src):
        return src
    data = src.read_bytes()
    (n,) = struct.unpack_from("<I", data, HEADER)
    fixed = bytearray(data)
    changed = 0
    for i in range(n):
        at = HEADER + 4 + i * FACET.size
        facet = FACET.unpack_from(data, at)
        if facet[0] == 0.0 and facet[1] == 0.0 and facet[2] == 0.0:
            struct.pack_into("<3f", fixed, at, *_normal(facet[3:12]))
            changed += 1
    if not changed:
        return src
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"normals_{src.name}"
    out.write_bytes(bytes(fixed))
    return out
