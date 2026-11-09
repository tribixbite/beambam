#!/usr/bin/env python3
"""remix_3mf warns (F-23) when overrides are applied to an already-sliced
.gcode.3mf, because the embedded plate_*.gcode is NOT regenerated and a
printer fed that file runs the old G-code. Uses a hand-built minimal 3MF so
it runs without the local rumi_frame fixture."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remix_3mf  # noqa: E402

_MODEL_CONFIG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<config>\n'
    b'  <object id="2">\n'
    b'    <metadata key="name" value="widget"/>\n'
    b'  </object>\n'
    b'</config>\n'
)


def _make_3mf(path: Path, *, sliced: bool) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(remix_3mf.CONFIG_PATH, _MODEL_CONFIG)
        if sliced:
            zf.writestr("Metadata/plate_1.gcode", b"; G-code\nG28\n")


def test_warns_on_sliced_file(tmp_path, capsys) -> None:
    path = tmp_path / "sliced.gcode.3mf"
    _make_3mf(path, sliced=True)
    sys.argv = ["remix_3mf.py", str(path), "--extruder", "2"]
    assert remix_3mf.main() == 0
    err = capsys.readouterr().err
    assert "sliced .gcode.3mf" in err and "Re-slice" in err
    # The override was still written.
    with zipfile.ZipFile(path) as zf:
        assert b'key="extruder" value="2"' in zf.read(remix_3mf.CONFIG_PATH)


def test_no_warning_on_unsliced_file(tmp_path, capsys) -> None:
    path = tmp_path / "model.3mf"
    _make_3mf(path, sliced=False)
    sys.argv = ["remix_3mf.py", str(path), "--extruder", "2"]
    assert remix_3mf.main() == 0
    err = capsys.readouterr().err
    assert "sliced .gcode.3mf" not in err


def test_filament_alias(tmp_path) -> None:
    """--filament is an alias for --extruder (both write the 'extruder' key)."""
    path = tmp_path / "model.3mf"
    _make_3mf(path, sliced=False)
    sys.argv = ["remix_3mf.py", str(path), "--filament", "3"]
    assert remix_3mf.main() == 0
    with zipfile.ZipFile(path) as zf:
        assert b'key="extruder" value="3"' in zf.read(remix_3mf.CONFIG_PATH)
