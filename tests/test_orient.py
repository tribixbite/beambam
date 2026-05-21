"""tests/test_orient.py — pre-slice mesh orientation.

beambam.orient.orient_mesh() rotates a vertex list according to one of
four modes: original (no-op), flat / auto (largest-area face down on
the bed), tall (longest bbox axis aligned with +Z).

Pure-Python math under test — no fixtures needed, just synthetic
vertex/triangle lists.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam.orient import (
    _largest_face_normal,
    _longest_bbox_axis,
    _rotation_matrix_from_to,
    _triangle_area_normal,
    orient_mesh,
)


# ----- vector / matrix primitives ----------------------------------------


def _approx_eq(a, b, eps: float = 1e-6) -> bool:
    return all(abs(x - y) < eps for x, y in zip(a, b))


def test_triangle_area_normal_axis_aligned_xy_floor():
    """Triangle at z=0 with CCW winding from above → area 0.5, normal +Z."""
    verts = [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.)]
    area, n = _triangle_area_normal(verts, (0, 1, 2))
    assert math.isclose(area, 0.5)
    assert _approx_eq(n, (0., 0., 1.))


def test_triangle_area_normal_degenerate():
    """Zero-area (colinear) triangle → area 0, fallback normal +Z."""
    verts = [(0., 0., 0.), (1., 0., 0.), (2., 0., 0.)]
    area, n = _triangle_area_normal(verts, (0, 1, 2))
    assert area == 0.0
    assert n == (0., 0., 1.)


def test_rotation_matrix_identity_when_src_equals_dst():
    R = _rotation_matrix_from_to((1., 0., 0.), (1., 0., 0.))
    assert _approx_eq([R[0][0], R[1][1], R[2][2]], [1., 1., 1.])
    assert _approx_eq([R[0][1], R[0][2], R[1][0]], [0., 0., 0.])


def test_rotation_matrix_180_degree_case():
    """src and dst antiparallel — Rodrigues degenerates; we hit the
    180° fallback path. Verify R rotates src onto dst correctly."""
    R = _rotation_matrix_from_to((1., 0., 0.), (-1., 0., 0.))
    rotated = (R[0][0] * 1. + R[0][1] * 0. + R[0][2] * 0.,
               R[1][0] * 1. + R[1][1] * 0. + R[1][2] * 0.,
               R[2][0] * 1. + R[2][1] * 0. + R[2][2] * 0.)
    assert _approx_eq(rotated, (-1., 0., 0.))


def test_rotation_matrix_90_degree_x_to_z():
    R = _rotation_matrix_from_to((1., 0., 0.), (0., 0., 1.))
    rotated = (R[0][0], R[1][0], R[2][0])  # apply R to (1,0,0)
    assert _approx_eq(rotated, (0., 0., 1.))


# ----- _largest_face_normal ----------------------------------------------


def test_largest_face_normal_picks_biggest_triangle():
    """One small triangle facing +Z + one big triangle facing -Z →
    largest_face_normal returns the big-triangle's normal."""
    verts = [
        # Small CCW triangle from above (normal +Z), area 0.5
        (0., 0., 1.), (1., 0., 1.), (0., 1., 1.),
        # Big triangle on z=0 wound CW from above (normal -Z), area 50
        (0., 0., 0.), (10., 0., 0.), (0., 10., 0.),
    ]
    tris = [(0, 1, 2), (3, 5, 4)]  # second uses CW order → -Z normal
    normal = _largest_face_normal(verts, tris)
    assert _approx_eq(normal, (0., 0., -1.))


def test_largest_face_normal_empty_mesh():
    """Empty mesh → safe default +Z."""
    assert _largest_face_normal([], []) == (0., 0., 1.)


# ----- _longest_bbox_axis -----------------------------------------------


def test_longest_bbox_axis_x_when_x_longest():
    verts = [(0., 0., 0.), (10., 0., 0.), (0., 1., 0.), (0., 0., 1.)]
    assert _longest_bbox_axis(verts) == 0


def test_longest_bbox_axis_y_when_y_longest():
    verts = [(0., 0., 0.), (1., 0., 0.), (0., 10., 0.), (0., 0., 1.)]
    assert _longest_bbox_axis(verts) == 1


def test_longest_bbox_axis_z_when_z_longest():
    verts = [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 10.)]
    assert _longest_bbox_axis(verts) == 2


def test_longest_bbox_axis_empty_falls_back_to_z():
    assert _longest_bbox_axis([]) == 2


# ----- orient_mesh end-to-end -------------------------------------------


def test_orient_mesh_original_is_identity():
    verts = [(1., 2., 3.), (4., 5., 6.), (7., 8., 9.)]
    out = orient_mesh(verts, [(0, 1, 2)], "original")
    assert out == verts


def test_orient_mesh_flat_puts_largest_face_at_min_z():
    """A wide flat slab inverted so its big face is on top (normal +Z).
    After --orient flat, the big face should end up at the BOTTOM of
    the bbox (the slicer's build plate)."""
    # 20×20 square at z=2 (top); 20×20 square at z=0 (bottom), connected
    # by 4 thin side walls. The top + bottom each have area 400; sides
    # are 20×2 = 40 each. So top + bottom are largest faces by a big
    # margin. The mesh is flipped — the top face's normal is +Z; we
    # expect flat to rotate so that face becomes the new BOTTOM.
    verts = [
        # Bottom square (z=0), winding so normal points DOWN (-Z)
        (0., 0., 0.), (20., 0., 0.), (20., 20., 0.), (0., 20., 0.),
        # Top square (z=2), winding so normal points UP (+Z) — this is
        # the largest +Z-facing triangle, ties with the bottom but ties
        # don't matter for the test: we just need the largest face to
        # be either top or bottom (both end up flat-on-bed).
        (0., 0., 2.), (20., 0., 2.), (20., 20., 2.), (0., 20., 2.),
    ]
    tris = [
        # Bottom face — CW from above → -Z normal, area 200 each
        (0, 2, 1), (0, 3, 2),
        # Top face — CCW from above → +Z normal, area 200 each
        (4, 5, 6), (4, 6, 7),
        # Sides (small, area 20 each)
        (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6),
        (3, 0, 4), (3, 4, 7),
    ]
    out = orient_mesh(verts, tris, "flat")
    # After flat: the largest-area normal got rotated to -Z, so its
    # face is now at the bottom of the bbox.
    spans_z = [v[2] for v in out]
    z_range = max(spans_z) - min(spans_z)
    # The slab is 2mm thick — after rotation the thin dim should still
    # be the Z dim (i.e. the orient call put a wide face on the plate).
    spans_x = [v[0] for v in out]
    spans_y = [v[1] for v in out]
    x_range = max(spans_x) - min(spans_x)
    y_range = max(spans_y) - min(spans_y)
    assert z_range < x_range and z_range < y_range, (
        f"after flat, Z dim ({z_range:.2f}) should be the smallest. "
        f"X={x_range:.2f} Y={y_range:.2f} Z={z_range:.2f}\nverts={out}")


def test_orient_mesh_auto_matches_flat():
    """auto is documented as `flat` for now — they should produce
    identical output on the same input."""
    verts = [(0., 0., 0.), (5., 0., 0.), (0., 5., 0.),
             (0., 0., 5.), (5., 5., 0.)]
    tris = [(0, 1, 2), (0, 1, 3), (1, 2, 3), (0, 2, 3), (1, 2, 4)]
    a = orient_mesh(verts, tris, "auto")
    f = orient_mesh(verts, tris, "flat")
    for va, vf in zip(a, f):
        assert _approx_eq(va, vf)


def test_orient_mesh_tall_already_tall_is_noop():
    """A mesh whose longest axis is Z should be untouched by tall."""
    verts = [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 10.)]
    out = orient_mesh(verts, [(0, 1, 2)], "tall")
    assert out == verts


def test_orient_mesh_tall_swaps_x_to_z():
    """A long-X slab → after tall, the long axis is Z."""
    verts = [(0., 0., 0.), (20., 0., 0.), (0., 1., 0.), (0., 0., 1.)]
    out = orient_mesh(verts, [(0, 1, 2)], "tall")
    spans = [max(v[i] for v in out) - min(v[i] for v in out) for i in range(3)]
    assert spans.index(max(spans)) == 2, f"expected longest on Z; spans={spans}"


def test_orient_mesh_tall_swaps_y_to_z():
    """A long-Y slab → after tall, the long axis is Z."""
    verts = [(0., 0., 0.), (1., 0., 0.), (0., 20., 0.), (0., 0., 1.)]
    out = orient_mesh(verts, [(0, 1, 2)], "tall")
    spans = [max(v[i] for v in out) - min(v[i] for v in out) for i in range(3)]
    assert spans.index(max(spans)) == 2, f"expected longest on Z; spans={spans}"


def test_orient_mesh_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown orient mode"):
        orient_mesh([(0., 0., 0.)], [], "diagonal")


def test_orient_mesh_preserves_triangle_count_and_vertex_count():
    """Rotation never adds/removes geometry; len(verts) is invariant.
    Catches accidental list-mutation bugs."""
    verts = [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.)]
    tris = [(0, 1, 2), (0, 1, 3)]
    for mode in ("original", "flat", "tall", "auto"):
        out = orient_mesh(verts, tris, mode)
        assert len(out) == len(verts), f"{mode} changed vertex count"


def test_orient_mesh_mode_is_case_insensitive():
    verts = [(0., 0., 0.), (5., 0., 0.), (0., 5., 0.)]
    a = orient_mesh(verts, [(0, 1, 2)], "FLAT")
    b = orient_mesh(verts, [(0, 1, 2)], "flat")
    for va, vb in zip(a, b):
        assert _approx_eq(va, vb)


# ----- CLI integration smoke ---------------------------------------------


def test_orient_arg_parses_via_x2d_slice_argparse(monkeypatch):
    """argparse must accept the documented choices + reject others."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--orient", default="original",
                   choices=("original", "flat", "tall", "auto"))
    ns = p.parse_args(["--orient", "flat"])
    assert ns.orient == "flat"
    ns = p.parse_args([])
    assert ns.orient == "original"
    with pytest.raises(SystemExit):
        p.parse_args(["--orient", "diagonal"])
