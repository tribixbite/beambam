"""tests/test_capture_params.py — `beambam capture-params` cloud-task capture.

No network: cloud_client is replaced with a fake exposing get_user_tasks /
get_task. Verifies the flatten (normalize_task_params) and the save behaviour.
"""
from __future__ import annotations

import argparse
import json
import types

from beambam.cli import cloud as CL


_TASK = {
    "id": 1012857366, "title": "8x2mm Oval & 8x1mm Slot Tips",
    "designId": 2191272, "modelId": "US60b31377cfcb8", "profileId": 700988943,
    "instanceId": 2906017, "deviceId": "00M09A000000000", "deviceModel": "X2D",
    "bedType": "Supertack Plate", "plateIndex": 1, "useAms": True,
    "amsMapping": [0], "amsMapping2": [{"ams_id": 0, "slot_id": 0}],
    "amsDetailMapping": [], "skipObjects": None, "jobType": 1,
    "mode": "cloud_slice", "weight": 6.43, "costTime": 1821, "repetitions": 1,
}
_DETAIL = {
    "job_id": 1012857366,
    "content": json.dumps({"info": {"name": "8x2mm Oval & 8x1mm Slot Tips",
                                     "printer": "00M09A000000000",
                                     "plate_idx": 1}}),
    "context": {
        "prefix": "makerworld/cache/2/US60b31377cfcb8/700988943/3mf/1/#F72323FF/REP1/",
        "configs": [{"name": "plate_1.json", "dir": "Metadata",
                     "url": "https://makerworld.bblmw.com/x"}],
        "materials": [{"color": "F72323FF", "material": "PETG-CF"}],
        "plate": {"index": 1, "name": ""},
    },
}


def test_normalize_task_params_flattens_task_and_detail():
    p = CL.normalize_task_params(_TASK, _DETAIL)
    assert p["task_id"] == 1012857366
    assert p["title"] == "8x2mm Oval & 8x1mm Slot Tips"
    assert p["device_id"] == "00M09A000000000"
    assert p["bed_type"] == "Supertack Plate"
    assert p["plate_index"] == 1
    assert p["use_ams"] is True
    assert p["mode"] == "cloud_slice"
    assert p["instance_id"] == 2906017
    assert p["prefix"].startswith("makerworld/cache/")
    assert p["materials"] == [{"color": "F72323FF", "material": "PETG-CF"}]
    assert p["info"]["plate_idx"] == 1
    assert p["raw_task"] is _TASK


def test_normalize_handles_missing_detail():
    p = CL.normalize_task_params(_TASK, None)
    assert p["task_id"] == 1012857366
    assert p["prefix"] is None and p["configs"] is None
    assert p["raw_detail_context"] is None


def _fake_cloud(monkeypatch, *, empty=False, tasks=(_TASK,), detail=_DETAIL):
    sess = types.SimpleNamespace(empty=empty)
    cli = types.SimpleNamespace(
        session=sess,
        get_user_tasks=lambda limit=1: list(tasks),
        get_task=lambda tid: detail)
    fake = types.SimpleNamespace(
        CloudClient=types.SimpleNamespace(load_or_anonymous=lambda: cli))
    monkeypatch.setitem(__import__("sys").modules, "cloud_client", fake)
    return cli


def test_capture_params_saves_file(tmp_path, monkeypatch, capsys):
    _fake_cloud(monkeypatch)
    out = tmp_path / "last_task.json"
    rc = CL.cmd_capture_params(argparse.Namespace(task_id=None, out=str(out)))
    assert rc == 0
    saved = json.loads(out.read_text())
    assert saved["task_id"] == 1012857366
    assert saved["bed_type"] == "Supertack Plate"
    assert "captured_at" in saved
    # summary echoed
    assert "8x2mm Oval" in capsys.readouterr().out


def test_capture_params_not_logged_in(tmp_path, monkeypatch):
    _fake_cloud(monkeypatch, empty=True)
    rc = CL.cmd_capture_params(argparse.Namespace(task_id=None,
                                                  out=str(tmp_path / "x.json")))
    assert rc == 1


def test_capture_params_specific_task_id(tmp_path, monkeypatch):
    other = {**_TASK, "id": 999, "title": "Other"}
    _fake_cloud(monkeypatch, tasks=(other, _TASK))
    out = tmp_path / "t.json"
    rc = CL.cmd_capture_params(argparse.Namespace(task_id="1012857366",
                                                  out=str(out)))
    assert rc == 0
    assert json.loads(out.read_text())["task_id"] == 1012857366
