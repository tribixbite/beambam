"""preflight_3mf: every plate_N.gcode must have its plate_N.gcode.md5 sidecar.

The md5 sidecar is what the printer's file-verify compares against the
uploaded gcode; a missing one is a certain err 84033544, so preflight
should catch it before the upload (it used to build the sidecar map and
never look at it).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preflight_3mf


def _minimal_3mf(path: Path, *, with_md5: bool) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr("Metadata/project_settings.config",
                   '{"printer_model": "Bambu Lab X2D", "filament_type": ["PLA"]}')
        z.writestr("Metadata/plate_1.gcode", "G28\n")
        if with_md5:
            z.writestr("Metadata/plate_1.gcode.md5", "0" * 32)
    return path


def _codes(result: object) -> set[str]:
    return {f.code for f in result.findings}          # type: ignore[attr-defined]


def test_missing_md5_sidecar_is_an_error(tmp_path: Path) -> None:
    res = preflight_3mf.validate(_minimal_3mf(tmp_path / "no_md5.gcode.3mf", with_md5=False))
    assert "missing-plate-md5" in _codes(res)


def test_present_md5_sidecar_passes_that_check(tmp_path: Path) -> None:
    res = preflight_3mf.validate(_minimal_3mf(tmp_path / "ok.gcode.3mf", with_md5=True))
    assert "missing-plate-md5" not in _codes(res)
