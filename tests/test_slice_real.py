"""Slices with a real Bambu Studio (whatever `slice_service.resolve_slicer` finds): the phone's source build,
or the official build `beambam slicer install` downloads. Opt-in (`BEAMBAM_REAL_SLICER=1`): CI's "slicer" job
runs it on Linux, macOS and Windows after `beambam slicer install`; on the phone it runs against bs-bionic."""
from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import pytest

import beambam.slice_service as ss
from beambam.slice_options import SliceOptions

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(os.environ.get("BEAMBAM_REAL_SLICER") != "1",
                                 reason="set BEAMBAM_REAL_SLICER=1 to slice with a real Bambu Studio")]

# Two boxes 20 mm apart: two objects, so a two-filament print has something on each nozzle.
_FACES = ((0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 4, 5), (0, 5, 1),
          (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0))


def _box(x0: float, size: float) -> list[tuple[float, float, float]]:
    return [(x0, 0, 0), (x0 + size, 0, 0), (x0 + size, size, 0), (x0, size, 0),
            (x0, 0, size), (x0 + size, 0, size), (x0 + size, size, size), (x0, size, size)]


def _stl(path: Path, boxes: list[tuple[float, float]]) -> Path:
    lines = ["solid boxes"]
    for x0, size in boxes:
        v = _box(x0, size)
        for a, b, c in _FACES:
            lines += ["facet normal 0 0 0", " outer loop",
                      *(f"  vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)), " endloop", "endfacet"]
    lines.append("endsolid boxes")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


@pytest.fixture(scope="module")
def slicer() -> ss.Slicer:
    s = ss.resolve_slicer()
    if s is None:
        pytest.fail(ss.NO_SLICER)
    return s


def _check(res: dict, out_dir: Path) -> zipfile.ZipFile:
    log = out_dir / "slicer.log"
    assert res["ok"], f"{res.get('error')}\n--- slicer.log tail ---\n" + (
        log.read_text(errors="replace")[-3000:] if log.is_file() else "(no log)")
    assert res["seconds"] > 0 and res["grams"] > 0 and res["plates"] == 1
    z = zipfile.ZipFile(res["file"])
    assert "Metadata/plate_1.gcode" in z.namelist()
    return z


def test_a_mesh_slices_for_the_x2d(tmp_path, slicer):
    src = _stl(tmp_path / "cube.stl", [(0, 15)])
    res = ss.slice_project(src, tmp_path / "out", name="cube.stl", options=SliceOptions(layer_height=0.2))
    with _check(res, tmp_path / "out") as z:
        gcode = z.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
    assert res["mesh"] and res["machine_profile"] == "Bambu Lab X2D 0.4 nozzle.json"
    assert "; layer_height = 0.2" in gcode


def test_two_filaments_on_chosen_nozzles(tmp_path, slicer):
    src = _stl(tmp_path / "pair.stl", [(0, 12), (30, 12)])
    opts = SliceOptions(filament_types=("PLA", "PETG"), nozzles=(2, 1))
    res = ss.slice_project(src, tmp_path / "out", name="pair.stl", options=opts)
    with _check(res, tmp_path / "out") as z:
        info = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
    assert "PLA" in info


def test_the_sliced_file_slices_again_as_a_project(tmp_path, slicer):
    first = ss.slice_project(_stl(tmp_path / "cube.stl", [(0, 15)]), tmp_path / "a", name="cube.stl")
    _check(first, tmp_path / "a").close()
    project = tmp_path / "cube_project.3mf"
    shutil.copy(first["file"], project)
    res = ss.slice_project(project, tmp_path / "b", name="cube_project.3mf", track_switch=True)
    _check(res, tmp_path / "b").close()
    assert not res["mesh"] and res["machine_profile"] == "Bambu Lab X2D 0.4 nozzle.json"
