"""beambam.orient — pre-slice mesh orientation.

Rotate an STL's vertices before slicing so the print sits the way the
user wants on the build plate. Four modes:

  original   no-op (default)
  flat       rotate so the largest-area face sits flat on the plate
             (its normal points down at -Z). Best for prints with a
             clear "base" face.
  tall       rotate so the longest axis of the bounding box aligns
             with +Z. Good for thin, vertical models that imported sideways.
  auto       alias for `flat` — heuristic baseline; we may expand
             this later (e.g. minimise support volume).

Pure-Python math: no numpy dependency so this works in `uvx beambam`
without optional extras. Vertex lists are small enough that the O(N)
matrix-multiply is fine in Python.

The transform preserves chirality (no reflections) so face normals
stay consistent with the slicer's winding-order expectations.
"""
from __future__ import annotations

import math
from typing import Iterable


# A 3-vector is just a tuple — we don't introduce numpy here.
_Vec3 = tuple[float, float, float]


# ----- vector math --------------------------------------------------------


def _sub(a: _Vec3, b: _Vec3) -> _Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: _Vec3, b: _Vec3) -> _Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a: _Vec3, b: _Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: _Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _scale(a: _Vec3, k: float) -> _Vec3:
    return (a[0] * k, a[1] * k, a[2] * k)


def _unit(a: _Vec3) -> _Vec3:
    n = _norm(a)
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


# ----- triangle properties -----------------------------------------------


def _triangle_area_normal(verts: list[_Vec3],
                          tri: tuple[int, int, int]) -> tuple[float, _Vec3]:
    """Return (area, unit-normal) for the triangle.

    The normal direction follows the right-hand rule given the triangle's
    vertex order; for a watertight mesh with consistent winding this
    points outward."""
    a, b, c = verts[tri[0]], verts[tri[1]], verts[tri[2]]
    n_raw = _cross(_sub(b, a), _sub(c, a))
    mag = _norm(n_raw)
    if mag < 1e-12:
        return 0.0, (0.0, 0.0, 1.0)
    return 0.5 * mag, _scale(n_raw, 1.0 / mag)


# ----- rotation construction ---------------------------------------------


def _rotation_matrix_from_to(src: _Vec3, dst: _Vec3) -> list[list[float]]:
    """Return a 3×3 rotation matrix that maps unit vector `src` onto `dst`.

    Uses Rodrigues' rotation formula. Degenerate cases:
      * src == dst → identity
      * src == -dst → 180° rotation around any axis perpendicular to src;
        we pick the basis axis least parallel to src for numerical stability.
    """
    a = _unit(src)
    b = _unit(dst)
    cos_t = max(-1.0, min(1.0, _dot(a, b)))
    if cos_t > 1.0 - 1e-9:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if cos_t < -1.0 + 1e-9:
        # 180° rotation — pick axis least parallel to a.
        candidates = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
        candidates.sort(key=lambda v: abs(_dot(v, a)))
        axis = _unit(_cross(a, candidates[0]))
        # R = I + 2 [k]× [k]× = 2 k kᵀ - I    (since 180° → sin=0, cos=-1)
        k = axis
        return [
            [2 * k[0] * k[0] - 1, 2 * k[0] * k[1],     2 * k[0] * k[2]],
            [2 * k[1] * k[0],     2 * k[1] * k[1] - 1, 2 * k[1] * k[2]],
            [2 * k[2] * k[0],     2 * k[2] * k[1],     2 * k[2] * k[2] - 1],
        ]
    axis = _unit(_cross(a, b))
    sin_t = math.sqrt(max(0.0, 1.0 - cos_t * cos_t))
    x, y, z = axis
    one_minus_c = 1.0 - cos_t
    return [
        [cos_t + x * x * one_minus_c,
         x * y * one_minus_c - z * sin_t,
         x * z * one_minus_c + y * sin_t],
        [y * x * one_minus_c + z * sin_t,
         cos_t + y * y * one_minus_c,
         y * z * one_minus_c - x * sin_t],
        [z * x * one_minus_c - y * sin_t,
         z * y * one_minus_c + x * sin_t,
         cos_t + z * z * one_minus_c],
    ]


def _apply_matrix(R: list[list[float]],
                  verts: Iterable[_Vec3]) -> list[_Vec3]:
    """Apply 3×3 rotation `R` to each vertex, return a new list."""
    out: list[_Vec3] = []
    for v in verts:
        out.append((
            R[0][0] * v[0] + R[0][1] * v[1] + R[0][2] * v[2],
            R[1][0] * v[0] + R[1][1] * v[1] + R[1][2] * v[2],
            R[2][0] * v[0] + R[2][1] * v[1] + R[2][2] * v[2],
        ))
    return out


# ----- orientation policies ----------------------------------------------


def _largest_face_normal(verts: list[_Vec3],
                          tris: list[tuple[int, int, int]]) -> _Vec3:
    """Return the unit outward normal of the largest-area triangle.

    For meshes whose "base" is composed of many small triangles rather
    than one big one, this heuristic can pick a wall. We accept that
    limitation — the user can fall back to `--orient original` and
    rotate manually in CAD."""
    if not tris:
        return (0.0, 0.0, 1.0)
    best_area = -1.0
    best_normal: _Vec3 = (0.0, 0.0, 1.0)
    for t in tris:
        area, normal = _triangle_area_normal(verts, t)
        if area > best_area:
            best_area = area
            best_normal = normal
    return best_normal


def _longest_bbox_axis(verts: list[_Vec3]) -> int:
    """Return 0/1/2 for X/Y/Z, whichever bbox span is longest."""
    if not verts:
        return 2
    mins = [min(v[i] for v in verts) for i in range(3)]
    maxs = [max(v[i] for v in verts) for i in range(3)]
    spans = [maxs[i] - mins[i] for i in range(3)]
    return spans.index(max(spans))


def orient_mesh(verts: list[_Vec3],
                tris: list[tuple[int, int, int]],
                mode: str) -> list[_Vec3]:
    """Return a new vertex list rotated according to `mode`.

    Triangle indices are unchanged — we never permute vertex order, so
    winding/normal direction is preserved by construction."""
    mode = (mode or "original").lower()
    if mode == "original":
        return list(verts)
    if mode in ("flat", "auto"):
        normal = _largest_face_normal(verts, tris)
        # Rotate the largest face's normal to point DOWN (-Z) so that
        # face becomes the bottom — the slicer's build plate is +Z up.
        R = _rotation_matrix_from_to(normal, (0.0, 0.0, -1.0))
        return _apply_matrix(R, verts)
    if mode == "tall":
        axis = _longest_bbox_axis(verts)
        if axis == 2:
            return list(verts)  # already tall
        # Map the longest axis to Z. Use basis-axis rotation: if the
        # longest is X, swap X↔Z; if Y, swap Y↔Z.
        src = (1.0, 0.0, 0.0) if axis == 0 else (0.0, 1.0, 0.0)
        R = _rotation_matrix_from_to(src, (0.0, 0.0, 1.0))
        return _apply_matrix(R, verts)
    raise ValueError(
        f"unknown orient mode {mode!r}; expected auto/flat/tall/original")
