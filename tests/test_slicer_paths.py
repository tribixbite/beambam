"""beambam.slicer_paths resolution + x2d_slice multi-part template normalisation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from beambam import slicer_paths as sp


def test_explicit_env_wins_and_is_not_guessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "bs.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("BAMBU_STUDIO", str(exe))
    assert sp.bambu_studio_bin() == exe
    monkeypatch.setenv("BAMBU_STUDIO", str(tmp_path / "missing.exe"))
    assert sp.bambu_studio_bin() is None          # a wrong explicit path is reported, not replaced


def test_standard_install_and_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("BAMBU_STUDIO", "X2D_BS_BIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sp, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    exe = tmp_path / "Bambu Studio" / "bambu-studio.exe"
    (exe.parent / "resources" / "profiles" / "BBL").mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sp, "_standard_installs", lambda: [exe])
    assert sp.bambu_studio_bin() == exe
    assert not sp.is_bionic_build(exe)
    assert sp.bbl_profiles_dir(exe) == exe.parent / "resources" / "profiles" / "BBL"


def test_run_bambu_cli_stops_a_process_that_hangs_after_result(tmp_path: Path) -> None:
    # A stand-in CLI: writes a complete result.json into its CWD, then hangs.
    script = tmp_path / "fake_bs.py"
    script.write_text(
        "import json, time\n"
        "json.dump({'return_code': 0, 'error_string': 'Success.'}, open('result.json', 'w'))\n"
        "time.sleep(60)\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = sp.run_bambu_cli([sys.executable, str(script)], out, exit_grace=1.0, timeout=30)
    assert rc == 0 and (out / "result.json").is_file()


def test_graft_reduces_a_multi_part_template() -> None:
    import x2d_slice as xs
    wrapper = (b'<model><resources><object id="3" type="model"><components>'
               b'<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 15"/>'
               b'<component p:path="/3D/Objects/object_1.model" objectid="2"/>'
               b'</components></object></resources></model>')
    out = xs._single_component_wrapper(wrapper).decode()
    assert out.count("<component ") == 1 and 'objectid="1"' in out and "transform" not in out

    settings = (b'<config><object id="3"><metadata face_count="900"/>'
                b'<part id="1"><metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 15.8 0 0 0 1"/>'
                b'<metadata key="source_offset_z" value="15.8"/><mesh_stat face_count="800"/></part>'
                b'<part id="2"><metadata key="name" value="flat base"/><mesh_stat face_count="100"/></part>'
                b'</object><plate/></config>')
    out = xs._single_part_settings(settings, 5312).decode()
    assert out.count("<part ") == 1 and "flat base" not in out
    assert 'value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"' in out and 'source_offset_z" value="0"' in out
    assert out.count('face_count="5312"') == 2
