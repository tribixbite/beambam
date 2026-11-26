"""`beambam slicer install` and how the daemon finds a Bambu Studio off the phone (slicer_install, slicer_paths,
slice_service.resolve_slicer). The real download is exercised by CI's "slicer" job; these use local files."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import beambam.slicer_install as si
import beambam.slicer_paths as sp
import beambam.slice_service as ss

ASSETS = [{"name": n} for n in (
    "BambuStudio_ubu22-v02.09.02.62-20261124164959.AppImage",
    "BambuStudio_ubu24-v02.09.02.62-20261124164959.AppImage",
    "Bambu_Studio_mac-v02.09.02.62-20261124164959.dmg",
    "Bambu_Studio_win-v02.09.02.62-20261124164959.exe",
    "Bambu_Studio_win-v02.09.02.62-20261124164959.zip",
    "signed_dll.rar")]


def test_the_asset_for_each_platform():
    name = lambda *a: si.pick_asset(ASSETS, *a)["name"]  # noqa: E731
    assert name("Windows", "AMD64").endswith(".zip")                   # portable, not the installer
    assert name("Darwin", "arm64").endswith(".dmg")
    assert "ubu24" in name("Linux", "x86_64", (2, 39))
    assert "ubu22" in name("Linux", "x86_64", (2, 35))                 # Ubuntu 22.04's glibc
    with pytest.raises(si.SlicerInstallError, match="2.35 or newer"):
        si.pick_asset(ASSETS, "Linux", "x86_64", (2, 31))
    with pytest.raises(si.SlicerInstallError, match="no Bambu Studio for Linux on aarch64"):
        si.pick_asset(ASSETS, "Linux", "aarch64", (2, 39))             # never the x86 build
    with pytest.raises(si.SlicerInstallError, match="Windows only"):
        si.pick_asset(ASSETS, "Windows", "arm64")
    with pytest.raises(si.SlicerInstallError, match="no Bambu Studio download"):
        si.pick_asset([{"name": "notes.txt"}], "Darwin", "arm64")


def _file(tmp_path: Path, data: bytes) -> tuple[str, str]:
    f = tmp_path / "src.bin"
    f.write_bytes(data)
    return f.as_uri(), "sha256:" + hashlib.sha256(data).hexdigest()


def test_downloads_are_checked(tmp_path):
    url, digest = _file(tmp_path, b"x" * 3000)
    out = si.download(url, tmp_path / "a.bin", 3000, digest)
    assert out.read_bytes() == b"x" * 3000 and not (tmp_path / "a.bin.part").exists()
    with pytest.raises(si.SlicerInstallError, match="corrupt"):
        si.download(url, tmp_path / "b.bin", 3000, "sha256:" + "0" * 64)
    with pytest.raises(si.SlicerInstallError, match="incomplete"):
        si.download(url, tmp_path / "c.bin", 4000, digest)
    assert not list(tmp_path.glob("[bc].bin*"))                          # nothing half-written is left
    notes: list[str] = []
    si.download(url, tmp_path / "d.bin", None, None, notes.append)     # an older release: no digest listed
    assert any("no checksum" in n for n in notes)


def test_a_zip_cannot_write_outside_its_folder(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(si.SlicerInstallError, match="outside the folder"):
        si.safe_extract_zip(bad, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def _profiles(base: Path) -> Path:
    bbl = base / "resources" / "profiles" / "BBL"
    (bbl / "machine").mkdir(parents=True)
    (bbl / "process").mkdir()
    (bbl / "machine" / "fdm_machine_common.json").write_text(json.dumps({"name": "fdm_machine_common", "a": 1}))
    (bbl / "machine" / f"{si.X2D_MACHINE}.json").write_text(
        json.dumps({"name": si.X2D_MACHINE, "inherits": "fdm_machine_common", "b": 2}))
    return bbl


def test_locate_finds_each_layout(tmp_path):
    app = tmp_path / "linux" / "squashfs-root"                         # AppImage: AppRun, resources beside bin/
    (app / "bin").mkdir(parents=True)
    (app / "AppRun").write_text("#!/bin/sh\n")
    (app / "bin" / "bambu-studio").write_text("")
    bbl = _profiles(app)
    assert si.locate(tmp_path / "linux") == (app / "AppRun", bbl)
    assert sp.bbl_profiles_dir(app / "bin" / "bambu-studio") == bbl     # ../resources, for an extracted copy
    mac = tmp_path / "mac" / "BambuStudio.app" / "Contents"
    (mac / "MacOS").mkdir(parents=True)
    (mac / "MacOS" / "BambuStudio").write_text("")
    _profiles(mac)                                                     # Contents/resources
    exe, prof = si.locate(tmp_path / "mac")
    assert exe == mac / "MacOS" / "BambuStudio" and prof.parent.name == "profiles"
    with pytest.raises(si.SlicerInstallError, match="no Bambu Studio executable"):
        si.locate(tmp_path / "mac" / "BambuStudio.app" / "Contents" / "resources")


def _windows_zip(tmp_path: Path) -> Path:
    tree = tmp_path / "build" / "Bambu Studio"
    tree.mkdir(parents=True)
    (tree / "bambu-studio.exe").write_text("")
    _profiles(tree)
    archive = tmp_path / "Bambu_Studio_win-v9.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for f in tree.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(tmp_path / "build"))
    return archive


def test_install_unpacks_flattens_and_becomes_the_slicer(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAMBAM_HOME", str(tmp_path / "home"))
    for var in ("BAMBU_STUDIO", "X2D_BS_BIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sp, "REPO_ROOT", tmp_path / "no-checkout")      # no phone build in the way
    archive = _windows_zip(tmp_path)
    data = archive.read_bytes()
    asset = {"name": archive.name, "browser_download_url": archive.as_uri(), "size": len(data),
             "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(si, "release", lambda tag: {"tag_name": "v9", "assets": [asset]})
    monkeypatch.setattr(si, "pick_asset", lambda assets: assets[0])
    man = si.install("v9", progress=lambda _: None)
    exe, prof = Path(man["exe"]), Path(man["profiles"])
    assert exe.name == "bambu-studio.exe" and exe.is_file() and man["version"] == "v9"
    assert man["sha256"] == asset["digest"] and not list((tmp_path / "home" / "slicer").glob(".v9-*"))
    # The CLI loads presets from the flattened copies: written when the build lacks them.
    flat = json.loads((prof / "machine_full" / f"{si.X2D_MACHINE}.json").read_text())
    assert flat["a"] == 1 and flat["b"] == 2 and "inherits" not in flat
    assert sp.downloaded_slicer() == man and sp.bambu_studio_bin() == exe
    s = ss.resolve_slicer(tmp_path / "no-checkout")
    assert s is not None and s.exe == exe and s.profiles == prof and not s.phone and s.problem() == ""
    st = ss.slicer_status(tmp_path / "no-checkout")
    assert st == {"ready": True, "kind": "downloaded", "version": "v9", "problem": ""}
    # Again: already there, nothing downloaded.
    monkeypatch.setattr(si, "download", lambda *a, **k: pytest.fail("downloaded twice"))
    assert si.install("v9", progress=lambda _: None)["exe"] == str(exe)
    assert si.remove() and sp.downloaded_slicer() is None


def test_no_slicer_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAMBAM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sp, "_standard_installs", lambda: [])
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    for var in ("BAMBU_STUDIO", "X2D_BS_BIN"):
        monkeypatch.delenv(var, raising=False)
    assert ss.resolve_slicer(tmp_path) is None
    res = ss.slice_project(tmp_path / "x.stl", tmp_path / "out", root=tmp_path)
    assert not res["ok"] and "beambam slicer install" in res["error"]
    assert ss.slicer_status(tmp_path)["ready"] is False


def test_an_installed_copy_without_flattened_profiles_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAMBAM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sp, "REPO_ROOT", tmp_path)
    exe = tmp_path / "opt" / "bambu-studio"
    exe.parent.mkdir()
    exe.write_text("")
    _profiles(exe.parent)
    monkeypatch.setenv("BAMBU_STUDIO", str(exe))
    s = ss.resolve_slicer(tmp_path)
    assert s is not None and not s.phone and "machine_full" in s.problem()
    res = ss.slice_project(tmp_path / "x.stl", tmp_path / "out", root=tmp_path)
    assert not res["ok"] and "beambam.profile_flatten" in res["error"]


def test_load_problem_names_the_missing_library(tmp_path):
    log = tmp_path / "slicer.log"
    log.write_text("bambu-studio: error while loading shared libraries: libwebkit2gtk-4.1.so.0: cannot open\n")
    assert "libwebkit2gtk-4.1.so.0" in ss.load_problem(log)
    assert ss.load_problem(tmp_path / "none.log") == ""
