"""tests/test_plate.py — `beambam plate {list,select,skip}`.

The list subcommand is exercised against the bundled rumi_frame.gcode.3mf
fixture. Select / skip are exercised with synthetic multi-plate 3MFs we
build on the fly (rumi has 1 plate, not enough to test plate-removal
without writing a fake one).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from beambam.plate import (
    PlateInfo,
    _drop_plate_xml,
    _plate_index_of_path,
    add_subparser,
    cmd_plate_list,
    cmd_plate_select,
    cmd_plate_skip,
    extract_plate,
    filter_3mf,
    plate_count,
    read_plates,
)


REPO_ROOT = HERE.parent
RUMI_FIXTURE = REPO_ROOT / "rumi_frame.gcode.3mf"


# ----- _plate_index_of_path ---------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("Metadata/plate_1.gcode",          1),
    ("Metadata/plate_42.gcode.md5",     42),
    ("Metadata/plate_2.json",           2),
    ("Metadata/plate_3.png",            3),
    ("Metadata/plate_3_small.png",      3),
    ("Metadata/plate_no_light_4.png",   4),
    ("Metadata/top_2.png",              2),
    ("Metadata/pick_1.png",             1),
    # Negative cases — these must NOT match.
    ("Metadata/slice_info.config",       None),
    ("Metadata/model_settings.config",   None),
    ("3D/3dmodel.model",                 None),
    ("",                                 None),
])
def test_plate_index_of_path(name, expected):
    assert _plate_index_of_path(name) == expected


# ----- _drop_plate_xml --------------------------------------------------


_SLICE_INFO_TWO_PLATES = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="weight" value="5.0"/>
    <metadata key="prediction" value="600"/>
    <object name="a.stl"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="weight" value="10.0"/>
    <metadata key="prediction" value="1200"/>
    <object name="b.stl"/>
  </plate>
</config>"""


def test_drop_plate_xml_keep_filters_correctly():
    out = _drop_plate_xml(_SLICE_INFO_TWO_PLATES, keep={1})
    root = ET.fromstring(out)
    indices = [
        int(m.get("value", "0"))
        for p in root.findall("plate")
        for m in p.findall("metadata") if m.get("key") == "index"
    ]
    assert indices == [1]


def test_drop_plate_xml_drop_filters_correctly():
    out = _drop_plate_xml(_SLICE_INFO_TWO_PLATES, drop={2})
    root = ET.fromstring(out)
    indices = [
        int(m.get("value", "0"))
        for p in root.findall("plate")
        for m in p.findall("metadata") if m.get("key") == "index"
    ]
    assert indices == [1]


def test_drop_plate_xml_requires_exactly_one_of_keep_drop():
    with pytest.raises(ValueError, match="exactly one"):
        _drop_plate_xml(_SLICE_INFO_TWO_PLATES)
    with pytest.raises(ValueError, match="exactly one"):
        _drop_plate_xml(_SLICE_INFO_TWO_PLATES, keep={1}, drop={2})


def test_drop_plate_xml_corrupt_input_passthrough():
    """Malformed XML should be returned untouched, not crash."""
    raw = b"<config><plate><corrupt"
    out = _drop_plate_xml(raw, keep={1})
    assert out == raw


def test_drop_plate_xml_empty_input():
    assert _drop_plate_xml(b"", keep={1}) == b""


# ----- read_plates against the bundled rumi fixture ---------------------


@pytest.mark.skipif(not RUMI_FIXTURE.exists(),
                    reason="rumi_frame.gcode.3mf fixture missing")
def test_read_plates_rumi():
    plates = read_plates(RUMI_FIXTURE)
    assert len(plates) == 1
    p = plates[0]
    assert p.index == 1
    assert p.weight_g > 0
    assert p.predicted_seconds > 0
    assert "rumi_frame.stl" in p.objects
    assert p.gcode_file == "Metadata/plate_1.gcode"
    assert "PLA" in " ".join(p.filaments)


def test_read_plates_predicted_human_format():
    """predicted_human renders short / long / zero correctly."""
    assert PlateInfo(index=1, predicted_seconds=0).predicted_human == "—"
    assert PlateInfo(index=1, predicted_seconds=45).predicted_human == "0m45s"
    assert PlateInfo(index=1, predicted_seconds=986).predicted_human == "16m26s"
    assert PlateInfo(index=1, predicted_seconds=4500).predicted_human == "1h15m"


# ----- synthetic multi-plate 3MF for filter_3mf tests -------------------


def _build_two_plate_3mf(path: Path) -> None:
    """Build a minimal valid-shape 3MF with two plates so we can test
    filter_3mf without depending on a real Bambu-sliced fixture."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", "<model>stub</model>")
        z.writestr("Metadata/slice_info.config", _SLICE_INFO_TWO_PLATES)
        z.writestr("Metadata/model_settings.config", b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <metadata key="gcode_file" value="Metadata/plate_2.gcode"/>
  </plate>
</config>""")
        # Per-plate assets — these MUST be stripped/kept by filter_3mf.
        z.writestr("Metadata/plate_1.gcode", "; plate 1 gcode\n")
        z.writestr("Metadata/plate_1.json", '{"plate":1}')
        z.writestr("Metadata/plate_2.gcode", "; plate 2 gcode\n")
        z.writestr("Metadata/plate_2.json", '{"plate":2}')
        # Non-per-plate asset that must always survive.
        z.writestr("Metadata/cut_information.xml", "<root/>")


def test_filter_3mf_select_keeps_only_target_plate(tmp_path):
    src = tmp_path / "src.3mf"
    dst = tmp_path / "dst.3mf"
    _build_two_plate_3mf(src)
    filter_3mf(src, dst, keep={1})

    with zipfile.ZipFile(dst) as z:
        names = set(z.namelist())
    assert "Metadata/plate_1.gcode" in names
    assert "Metadata/plate_1.json" in names
    assert "Metadata/plate_2.gcode" not in names
    assert "Metadata/plate_2.json" not in names
    # Slice_info must reflect: only plate 1.
    plates = read_plates(dst)
    assert [p.index for p in plates] == [1]


def test_filter_3mf_skip_drops_only_target_plate(tmp_path):
    src = tmp_path / "src.3mf"
    dst = tmp_path / "dst.3mf"
    _build_two_plate_3mf(src)
    filter_3mf(src, dst, drop={2})

    with zipfile.ZipFile(dst) as z:
        names = set(z.namelist())
    assert "Metadata/plate_1.gcode" in names
    assert "Metadata/plate_2.gcode" not in names
    plates = read_plates(dst)
    assert [p.index for p in plates] == [1]


def test_filter_3mf_preserves_non_plate_assets(tmp_path):
    src = tmp_path / "src.3mf"
    dst = tmp_path / "dst.3mf"
    _build_two_plate_3mf(src)
    filter_3mf(src, dst, keep={1})
    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
    # The 3D model and content-types-equivalent must survive.
    assert "3D/3dmodel.model" in names
    assert "Metadata/cut_information.xml" in names


def test_filter_3mf_requires_one_of_keep_drop(tmp_path):
    src = tmp_path / "src.3mf"
    _build_two_plate_3mf(src)
    with pytest.raises(ValueError, match="exactly one"):
        filter_3mf(src, tmp_path / "x.3mf")


# ----- CLI dispatch ------------------------------------------------------


def _args(**kw) -> argparse.Namespace:
    defaults = {"file": "", "plate": 1, "out": "", "force": False,
                "json": False}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


@pytest.mark.skipif(not RUMI_FIXTURE.exists(),
                    reason="rumi_frame.gcode.3mf fixture missing")
def test_cmd_plate_list_human(capsys):
    rc = cmd_plate_list(_args(file=str(RUMI_FIXTURE)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 plate(s)" in out
    assert "rumi_frame" in out


@pytest.mark.skipif(not RUMI_FIXTURE.exists(),
                    reason="rumi_frame.gcode.3mf fixture missing")
def test_cmd_plate_list_json(capsys):
    rc = cmd_plate_list(_args(file=str(RUMI_FIXTURE), json=True))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert parsed[0]["index"] == 1


def test_cmd_plate_list_missing_file_exits_1(capsys, tmp_path):
    rc = cmd_plate_list(_args(file=str(tmp_path / "nope.3mf")))
    assert rc == 1


def test_cmd_plate_skip_refuses_only_plate(tmp_path, capsys):
    src = tmp_path / "single.3mf"
    # Build a 1-plate 3MF.
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Metadata/slice_info.config", b"""<?xml version="1.0"?>
<config><plate><metadata key="index" value="1"/></plate></config>""")
    rc = cmd_plate_skip(_args(file=str(src), plate=1,
                               out=str(tmp_path / "x.3mf")))
    assert rc == 1
    assert "only plate" in capsys.readouterr().err


def test_cmd_plate_select_unknown_plate_exits_1(tmp_path, capsys):
    src = tmp_path / "src.3mf"
    _build_two_plate_3mf(src)
    rc = cmd_plate_select(_args(file=str(src), plate=99,
                                  out=str(tmp_path / "x.3mf")))
    assert rc == 1
    assert "not in" in capsys.readouterr().err


def test_cmd_plate_select_refuses_overwrite_without_force(tmp_path):
    src = tmp_path / "src.3mf"
    dst = tmp_path / "out.3mf"
    _build_two_plate_3mf(src)
    dst.write_bytes(b"existing")
    rc = cmd_plate_select(_args(file=str(src), plate=1, out=str(dst)))
    assert rc == 1
    assert dst.read_bytes() == b"existing"


def test_cmd_plate_select_with_force_overwrites(tmp_path):
    src = tmp_path / "src.3mf"
    dst = tmp_path / "out.3mf"
    _build_two_plate_3mf(src)
    dst.write_bytes(b"old")
    rc = cmd_plate_select(_args(file=str(src), plate=1, out=str(dst),
                                  force=True))
    assert rc == 0
    # File must now be a valid zip (not "old" anymore).
    assert zipfile.is_zipfile(dst)


def test_cmd_plate_select_creates_parent_dirs(tmp_path):
    src = tmp_path / "src.3mf"
    dst = tmp_path / "nested" / "deep" / "out.3mf"
    _build_two_plate_3mf(src)
    rc = cmd_plate_select(_args(file=str(src), plate=1, out=str(dst)))
    assert rc == 0
    assert dst.is_file()


# ----- subparser wiring smoke -------------------------------------------


def test_subparser_wires_three_actions():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    add_subparser(sub)
    # list
    ns = p.parse_args(["plate", "list", "x.3mf"])
    assert ns.fn is cmd_plate_list
    # select
    ns = p.parse_args(["plate", "select", "x.3mf", "2", "--out", "o.3mf"])
    assert ns.fn is cmd_plate_select
    assert ns.plate == 2
    assert ns.out == "o.3mf"
    # skip
    ns = p.parse_args(["plate", "skip", "x.3mf", "3", "--out", "o.3mf",
                       "--force"])
    assert ns.fn is cmd_plate_skip
    assert ns.force is True


def test_cmd_plate_select_onto_itself_is_a_clean_refusal(tmp_path, capsys):
    """--out naming the input (with --force) is refused with a message, not a
    ValueError traceback from filter_3mf."""
    src = tmp_path / "two.gcode.3mf"
    _build_two_plate_3mf(src)
    before = src.read_bytes()
    rc = cmd_plate_select(_args(file=str(src), plate=1, out=str(src), force=True))
    assert rc == 1
    assert "same file" in capsys.readouterr().err
    assert src.read_bytes() == before          # input untouched


# ----- extract_plate: single-plate extraction for the slicer ------------

_EXTRACT_MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
 <resources>
  <object id="2" type="model"><mesh/></object>
  <object id="4" type="model"><mesh/></object>
 </resources>
 <build p:UUID="aaaaaaaa-0000-0000-0000-000000000000">
  <item objectid="2" p:UUID="00000002-0000-0000-0000-000000000000" transform="1 0 0 0 1 0 0 0 1 50 60 0" printable="1"/>
  <item objectid="4" p:UUID="00000004-0000-0000-0000-000000000000" transform="1 0 0 0 1 0 0 0 1 120 60 0" printable="1"/>
 </build>
</model>"""

_EXTRACT_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="2"><metadata key="name" value="A.stl"/></object>
  <object id="4"><metadata key="name" value="B.stl"/></object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>
    <model_instance><metadata key="object_id" value="2"/><metadata key="instance_id" value="0"/></model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <metadata key="thumbnail_file" value="Metadata/plate_2.png"/>
    <model_instance><metadata key="object_id" value="4"/><metadata key="instance_id" value="0"/></model_instance>
  </plate>
  <assemble>
    <assemble_item object_id="2" instance_id="0" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
    <assemble_item object_id="4" instance_id="0" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
  </assemble>
</config>"""


def _build_two_plate_project(path: Path) -> None:
    """A minimal two-plate Bambu project (object 2 on plate 1, object 4 on plate 2) with a build and an
    assemble section — the shape extract_plate must reduce to one clean plate."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", _EXTRACT_MODEL)
        z.writestr("Metadata/model_settings.config", _EXTRACT_SETTINGS)
        z.writestr("Metadata/project_settings.config", "{}")
        z.writestr("Metadata/plate_1.png", b"p1")
        z.writestr("Metadata/plate_2.png", b"p2")
        z.writestr("Metadata/plate_1.json", b"{}")
        z.writestr("Metadata/plate_2.json", b"{}")


def _read(dst: Path, member: str) -> str:
    with zipfile.ZipFile(dst) as z:
        return z.read(member).decode("utf-8")


def test_plate_count_counts_plates(tmp_path):
    src = tmp_path / "p.3mf"
    _build_two_plate_project(src)
    assert plate_count(src) == 2
    # A bare mesh / non-project counts as one plate.
    stub = tmp_path / "s.3mf"
    with zipfile.ZipFile(stub, "w") as z:
        z.writestr("x", "y")
    assert plate_count(stub) == 1


def test_extract_plate_keeps_only_its_objects_and_strips_assemble(tmp_path):
    src = tmp_path / "p.3mf"
    dst = tmp_path / "out.3mf"
    _build_two_plate_project(src)
    info = extract_plate(src, dst, 2)
    assert info == {"objects": ["4"], "copies": {"4": 1}}
    model = _read(dst, "3D/3dmodel.model")
    assert 'objectid="4"' in model and 'objectid="2"' not in model
    ms = _read(dst, "Metadata/model_settings.config")
    assert "<assemble>" not in ms            # the section that aborts the slicer with code -6
    assert ms.count("<plate>") == 1          # one plate, renumbered
    assert 'value="1"' in ms                 # plater_id renumbered to 1
    assert 'id="2"' not in ms                # the other plate's object dropped
    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
    assert "Metadata/plate_1.png" in names   # plate 2's png renamed to _1
    assert "Metadata/plate_2.png" not in names


def test_extract_plate_quantity_duplicates_and_drops(tmp_path):
    src = tmp_path / "p.3mf"
    dst = tmp_path / "out.3mf"
    _build_two_plate_project(src)
    # On plate 1: print object 2 three times (offset copies). Object 2 is the only one on plate 1.
    info = extract_plate(src, dst, 1, {"2": [(0.0, 0.0), (30.0, 0.0), (0.0, 30.0)]})
    assert info["copies"] == {"2": 3}
    model = _read(dst, "3D/3dmodel.model")
    items = re.findall(r"<item\b[^>]*/>", model)
    assert len(items) == 3
    # Translations: 50,60 then 80,60 then 50,90 (tx at index 9, ty at index 10).
    tx = [it.split('transform="')[1].split('"')[0].split()[9:11] for it in items]
    assert tx[0] == ["50", "60"] and tx[1] == ["80", "60"] and tx[2] == ["50", "90"]
    # Fresh UUIDs on the copies (no duplicate p:UUID).
    uuids = re.findall(r'<item[^>]*p:UUID="([^"]*)"', model)
    assert len(uuids) == len(set(uuids)) == 3


def test_extract_plate_count_zero_drops_the_object(tmp_path):
    src = tmp_path / "p.3mf"
    _build_two_plate_project(src)
    # Plate 2 holds only object 4; dropping it leaves the plate empty -> ValueError.
    with pytest.raises(ValueError):
        extract_plate(src, tmp_path / "o.3mf", 2, {"4": []})


def test_extract_plate_unknown_plate_raises(tmp_path):
    src = tmp_path / "p.3mf"
    _build_two_plate_project(src)
    with pytest.raises(ValueError):
        extract_plate(src, tmp_path / "o.3mf", 9)
