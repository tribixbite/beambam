"""Slices with a real Bambu Studio (whatever `slice_service.resolve_slicer` finds): the phone's source build,
or the official build `beambam slicer install` downloads. Opt-in (`BEAMBAM_REAL_SLICER=1`): CI's "slicer" job
runs it on Linux, macOS and Windows after `beambam slicer install`; on the phone it runs against bs-bionic."""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

import beambam.slice_service as ss
from beambam.slice_options import SliceOptions
import tests.test_http_print as _http

daemon = _http.daemon            # the daemon fixture, shared with the HTTP tests

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


def test_a_chosen_plate_with_quantity_slices(tmp_path, slicer):
    """extract_plate end to end: ask a single-plate project to print its object 3 times. The chosen plate is
    extracted, the object is re-placed on the bed and duplicated, and the result slices to ~3x the grams."""
    first = ss.slice_project(_stl(tmp_path / "cube.stl", [(0, 15)]), tmp_path / "a", name="cube.stl")
    _check(first, tmp_path / "a").close()
    project = tmp_path / "cube_project.3mf"
    shutil.copy(first["file"], project)
    model = zipfile.ZipFile(project).read("3D/3dmodel.model").decode("utf-8", "replace")
    oid = re.search(r'<item[^>]*objectid="(\d+)"', model).group(1)
    res = ss.slice_project(project, tmp_path / "b", name="cube_project.3mf", track_switch=True,
                           options=SliceOptions(plate=1, quantity={oid: ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))}))
    _check(res, tmp_path / "b").close()
    assert res["grams"] > first["grams"] * 2.0  # three copies, minus shared overhead


def test_the_daemon_slices_over_http(tmp_path, slicer, daemon):
    """What pokefit.party and beambam.boo do: /healthz says it can slice, POST /slice an STL, poll the job."""
    from tests.test_http_print import _req, _wait
    import socket
    import time
    port, _ = daemon
    t0 = time.time()
    while True:                       # the fixture gives up waiting after 15 s without saying so
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            assert time.time() - t0 < 60, f"the daemon never listened on {port}"
            time.sleep(0.2)
    code, health, _ = _req(port, "/healthz", method="GET")
    assert "slice" in health["capabilities"] and health["slicer"]["ready"], health
    stl = _stl(tmp_path / "cube.stl", [(0, 15)]).read_bytes()
    code, body, _ = _req(port, "/slice", stl, headers={"X-Filename": "cube.stl", "Origin": "https://pokefit.party",
                                                     "X-Slice-Options": '{"filament_types":["PETG"],"bed_type":"Textured PEI Plate"}'})
    assert code == 202, body
    job = _wait(port, body["poll"])
    assert job["state"] == "done", job
    assert job["grams"] > 0 and job["mesh"] and Path(job["file"]).name == "cube.gcode.3mf"


def test_a_mesh_slices_for_the_plate_asked_for(tmp_path, slicer):
    """The profiles' default plate (Cool Plate) refuses PETG (code -61); the plate asked for is used."""
    src = _stl(tmp_path / "cube.stl", [(0, 15)])
    refused = ss.slice_project(src, tmp_path / "a", name="cube.stl", options=SliceOptions(filament_types=("PETG",)))
    # The phone build's log names the plate ("Plate 1: Cool Plate does not support filament 1"); the official
    # Windows build says only the code.
    assert not refused["ok"] and "code -61" in refused["error"], refused
    res = ss.slice_project(src, tmp_path / "b", name="cube.stl",
                           options=SliceOptions(filament_types=("PETG",), bed_type="Textured PEI Plate"))
    _check(res, tmp_path / "b").close()


def test_a_binary_stl_with_zero_normals_slices(tmp_path, slicer):
    """Valid STL (normals left to the reader), refused by Bambu Studio as written (code -6 on the phone build):
    beambam fills the normals in before slicing."""
    import struct
    from beambam.stl_normals import FACET
    rows = []
    for x0, size in [(0, 15)]:
        v = _box(x0, size)
        rows += [FACET.pack(0.0, 0.0, 0.0, *v[a], *v[b], *v[c], 0) for a, b, c in _FACES]
    src = tmp_path / "zero_normals.stl"
    src.write_bytes(b"\0" * 80 + struct.pack("<I", len(rows)) + b"".join(rows))
    res = ss.slice_project(src, tmp_path / "out", name="zero_normals.stl")
    _check(res, tmp_path / "out").close()
