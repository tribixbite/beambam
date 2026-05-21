"""tests/test_cmd_ams_and_misc.py — AMS + cmd_printers + cmd_files.

Covers three more clusters of untested cmd_* handlers:

1. **AMS commands** (LAN, publish-based, same shape as
   test_cmd_lan_control.py):
     cmd_ams_load    — tray_id math (ams_id * 4 + slot_id) is the
                       "global slot" indexing the firmware uses;
                       worth pinning down with explicit cases.
     cmd_ams_unload  — uses target=255 sentinel.

2. **`cmd_printers`** — pure I/O: read INI, emit JSON. No network. Tested
   with a fake $HOME containing a credentials file.

3. **`cmd_files`** — FTPS file listing. We mock `FileTunnelClient` to
   avoid touching real FTPS; verify the JSON-emission branch.
"""
from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


# ----- AMS handler fixtures (shared with test_cmd_lan_control pattern) ----


class _Cap:
    def __init__(self, *args, **kwargs): self.pubs: list[dict] = []
    def connect(self, *a, **kw): pass
    def disconnect(self): pass
    def publish(self, payload, qos=1, **kw): self.pubs.append(payload)


@pytest.fixture
def captured(monkeypatch):
    import x2d_bridge

    bucket: list[dict] = []

    class _Cli(_Cap):
        def publish(self, payload, qos=1, **kw):
            bucket.append(payload)

    monkeypatch.setattr(x2d_bridge, "X2DClient", _Cli)
    return bucket


def _args(**kw) -> argparse.Namespace:
    defaults = dict(
        ip="192.168.0.42", code="abcdef12", serial="00P9AJ000000000",
        printer=None, config=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ===== AMS unload =========================================================


def test_ams_unload_uses_255_sentinel_for_target_and_slot(captured):
    """Per DeviceManager.cpp:1537 — target=255 + slot=255 is the unload
    sentinel; firmware interprets it as 'eject whatever is loaded'."""
    import x2d_bridge

    args = _args(ams=0, curr_temp=210, tar_temp=210)
    rc = x2d_bridge.cmd_ams_unload(args)
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "ams_change_filament"
    assert body["target"] == 255
    assert body["slot_id"] == 255
    assert body["ams_id"] == 0
    assert body["curr_temp"] == 210
    assert body["tar_temp"] == 210


# ===== AMS load — the tricky tray_id math ================================


@pytest.mark.parametrize("ams,slot,expected_target", [
    # The handler computes: tray_id = ams_id*4 + slot_id;
    #                       target = ams_id if tray_id == 0 else tray_id
    # That branch matters because slot 0 on AMS 0 has tray_id == 0,
    # which collides with the unload sentinel pattern; the handler
    # uses ams_id (which is 0) as the target there to disambiguate.
    (0, 0, 0),    # tray_id=0 → target=ams_id=0
    (0, 1, 1),    # tray_id=1
    (0, 3, 3),    # tray_id=3 (slot 3 on AMS 0)
    (1, 0, 4),    # tray_id=4 (slot 0 on AMS 1)
    (2, 2, 10),   # tray_id=10
    (3, 3, 15),   # tray_id=15 (last slot on AMS 3)
])
def test_ams_load_computes_correct_tray_id(captured, ams, slot, expected_target):
    """Lock in the tray_id arithmetic — getting this wrong loads the
    wrong filament."""
    import x2d_bridge

    args = _args(ams=ams, slot=slot, curr_temp=215, tar_temp=215)
    rc = x2d_bridge.cmd_ams_load(args)
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "ams_change_filament"
    assert body["target"] == expected_target
    assert body["ams_id"] == ams
    assert body["slot_id"] == slot


# ===== cmd_printers =======================================================


def test_cmd_printers_lists_default_section(monkeypatch, tmp_path, capsys):
    """`[printer]` (no name) is emitted with name = ''."""
    import x2d_bridge

    creds = tmp_path / "credentials"
    creds.write_text(
        "[printer]\n"
        "ip = 192.168.1.42\n"
        "code = abcdef12\n"
        "serial = 00P9AJ000000000\n"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path.parent))
    # The handler reads `Path.home() / ".x2d" / "credentials"`; make it work
    # by pointing $HOME at the parent of a `.x2d/credentials` we set up.
    home = tmp_path.parent
    (home / ".x2d").mkdir(exist_ok=True)
    (home / ".x2d" / "credentials").write_text(creds.read_text())

    rc = x2d_bridge.cmd_printers(_args())
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "printers" in data
    assert len(data["printers"]) == 1
    p0 = data["printers"][0]
    assert p0["name"] == ""
    assert p0["ip"] == "192.168.1.42"
    assert p0["serial"] == "00P9AJ000000000"


def test_cmd_printers_lists_named_sections(monkeypatch, tmp_path, capsys):
    """`[printer:studio]` + `[printer:lab]` are emitted with their names."""
    import x2d_bridge

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path.parent))
    home = tmp_path.parent
    (home / ".x2d").mkdir(exist_ok=True)
    (home / ".x2d" / "credentials").write_text(
        "[printer:studio]\nip = 10.0.0.1\nserial = AAA\n\n"
        "[printer:lab]\nip = 10.0.0.2\nserial = BBB\n"
    )

    rc = x2d_bridge.cmd_printers(_args())
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    names = sorted(p["name"] for p in data["printers"])
    assert names == ["lab", "studio"]


def test_cmd_printers_empty_when_no_creds_file(monkeypatch, tmp_path, capsys):
    """No file → no exception, just an empty list."""
    import x2d_bridge

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # tmp_path has no .x2d/ — the handler must not crash on missing file.
    rc = x2d_bridge.cmd_printers(_args())
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"printers": []}


def test_cmd_printers_ignores_non_printer_sections(monkeypatch, tmp_path, capsys):
    """Foreign sections like `[other]` must not appear in the output."""
    import x2d_bridge

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path.parent))
    home = tmp_path.parent
    (home / ".x2d").mkdir(exist_ok=True)
    (home / ".x2d" / "credentials").write_text(
        "[printer]\nip = 1.1.1.1\nserial = A\n\n"
        "[other]\nstuff = ignored\n"
    )

    rc = x2d_bridge.cmd_printers(_args())
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["printers"]) == 1
    assert data["printers"][0]["ip"] == "1.1.1.1"


# ===== cmd_files (FTPS path) ==============================================


class _FakeFile:
    def __init__(self, name, path, size, is_dir=False, time=""):
        self.name, self.path, self.size = name, path, size
        self.is_dir, self.time = is_dir, time


class _FakeTunnel:
    """Stand-in for FileTunnelClient. The handler uses it as a context
    manager: `with FileTunnelClient(...) as cli: cli.list_files(kind)`."""

    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

    def list_files(self, kind: str):
        return [
            _FakeFile("a.gcode.3mf", "/sdcard/a.gcode.3mf", 1234),
            _FakeFile("dir1", "/sdcard/dir1", 0, is_dir=True),
        ]


def test_cmd_files_emits_json(monkeypatch, capsys):
    """`--json` flag emits a parseable list with name/path/size/is_dir."""
    import x2d_bridge

    # Inject a fake `runtime.network_shim.file_tunnel` module so the
    # import inside cmd_files resolves to our stub.
    fake_mod = types.ModuleType("runtime.network_shim.file_tunnel")
    fake_mod.FileTunnelClient = _FakeTunnel
    class _FTErr(Exception): pass
    fake_mod.FileTunnelError = _FTErr
    monkeypatch.setitem(sys.modules, "runtime.network_shim.file_tunnel", fake_mod)

    args = _args(kind="sdcard", json=True)
    rc = x2d_bridge.cmd_files(args)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list) and len(parsed) == 2
    assert parsed[0]["name"] == "a.gcode.3mf"
    assert parsed[0]["size"] == 1234
    assert parsed[1]["is_dir"] is True


def test_cmd_files_sys_exits_on_missing_module(monkeypatch):
    """If file_tunnel can't be imported (deleted, broken install), the
    handler must sys.exit with a useful error — NOT a raw ImportError."""
    import x2d_bridge

    # Inject a broken module that raises ImportError on attribute access.
    # `sys.modules[name] = None` makes `from <name> import X` raise
    # ImportError without us providing a real module.
    monkeypatch.setitem(sys.modules, "runtime.network_shim.file_tunnel",
                         None)
    with pytest.raises(SystemExit) as exc:
        x2d_bridge.cmd_files(_args(kind="sdcard", json=True))
    # SystemExit's str() should mention the missing module.
    assert "file_tunnel" in str(exc.value)
