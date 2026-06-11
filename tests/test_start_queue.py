"""tests/test_start_queue.py — `beambam start` print-next-in-queue + the
shared cloud project_file builder + QueueManager one-shot dispatch helpers.

No printer / cloud / network required — `cmd_cloud_print` is mocked so the
test verifies *routing* (the right job's .3mf is handed to cloud-print and
the queue state transitions correctly), not an actual print.
"""
from __future__ import annotations

import argparse
import types

import pytest

from beambam.cli import cloud as CL
from beambam.cli import control as CT
from runtime.queue import manager as QM
from runtime.queue.manager import QueueManager


# --- build_cloud_project_file ------------------------------------------------

def test_build_project_file_shape_and_determinism():
    up = {"url": "https://oss/x.3mf", "md5": "deadbeef",
          "remote_name": "Widget.gcode.3mf"}
    b1 = CL.build_cloud_project_file("SER123", up, now=1700000000, slot=5)
    b2 = CL.build_cloud_project_file("SER123", up, now=1700000000, slot=5)
    assert b1 == b2                                   # deterministic for fixed now
    assert b1["command"] == "project_file"
    assert b1["dev_id"] == "SER123"
    assert b1["url"] == up["url"] and b1["md5"] == up["md5"]
    assert b1["subtask_name"] == "Widget.gcode"       # .3mf stripped, .gcode kept
    assert b1["job_type"] == 1                         # cloud
    assert b1["sequence_id"] == "1700000000"
    assert b1["job_id"] == 1700000000 * 10
    # AMS slot 5 → ams_id 1, slot_id 1
    assert b1["ams_mapping"] == [5]
    assert b1["ams_mapping2"] == [{"ams_id": 1, "slot_id": 1}]


def test_build_project_file_no_ams():
    up = {"url": "u", "md5": "m", "remote_name": "a.3mf"}
    b = CL.build_cloud_project_file("S", up, now=1, use_ams=False)
    assert b["use_ams"] is False
    assert b["ams_mapping"] == [] and b["ams_mapping2"] == []
    assert b["subtask_name"] == "a.gcode"             # bare .3mf → .gcode


# --- QueueManager.start_next / mark_failed -----------------------------------

def _qm(tmp_path):
    qm = QueueManager(dispatch_cb=lambda _j: True, path=tmp_path / "queue.json")
    return qm


def test_start_next_marks_running_and_is_fifo(tmp_path):
    qm = _qm(tmp_path)
    a = qm.add(printer="", gcode="/x/a.3mf", label="A")
    b = qm.add(printer="", gcode="/x/b.3mf", label="B")
    job = qm.start_next("")
    assert job is not None and job.id == a.id          # FIFO head
    assert qm.get(a.id).status == "running"
    # a second start_next is refused while one is running
    assert qm.start_next("") is None
    # b stays pending
    assert qm.get(b.id).status == "pending"


def test_start_next_empty_returns_none(tmp_path):
    qm = _qm(tmp_path)
    assert qm.start_next("") is None


def test_mark_failed(tmp_path):
    qm = _qm(tmp_path)
    a = qm.add(printer="", gcode="/x/a.3mf")
    qm.start_next("")
    qm.mark_failed(a.id, "boom")
    j = qm.get(a.id)
    assert j.status == "failed" and j.error == "boom" and j.finished > 0


# --- _start_next_in_queue routing -------------------------------------------

@pytest.fixture
def patched_serial(monkeypatch):
    monkeypatch.setattr(
        CT._config.Creds, "resolve",
        staticmethod(lambda args: types.SimpleNamespace(serial="TESTSER")))


def test_start_next_in_queue_empty_falls_through(tmp_path, monkeypatch,
                                                 patched_serial):
    monkeypatch.setattr(QM, "_DEFAULT_PATH", tmp_path / "queue.json")
    # empty queue → None so cmd_start falls through to print-again
    assert CT._start_next_in_queue(argparse.Namespace()) is None


def test_start_next_in_queue_dispatches_to_cloud_print(tmp_path, monkeypatch,
                                                       patched_serial):
    monkeypatch.setattr(QM, "_DEFAULT_PATH", tmp_path / "queue.json")
    three_mf = tmp_path / "model.gcode.3mf"
    three_mf.write_bytes(b"PK\x03\x04 fake 3mf")
    # enqueue one job via a manager pointed at the same path
    QueueManager(dispatch_cb=lambda _j: True,
                 path=tmp_path / "queue.json").add(
        printer="", gcode=str(three_mf), slot=3, label="Model")

    captured = {}
    def fake_cloud_print(ns):
        captured["ns"] = ns
        return 0
    monkeypatch.setattr("beambam.cli.cloud.cmd_cloud_print", fake_cloud_print)

    rc = CT._start_next_in_queue(argparse.Namespace())
    assert rc == 0
    ns = captured["ns"]
    assert ns.file == str(three_mf)
    assert ns.serial == "TESTSER"
    assert ns.slot == 3
    assert ns.dry_run is False
    # cloud-print succeeded → job removed from the queue (it's printing now;
    # leaving it "running" would be demoted to pending on reload → re-dispatch)
    remaining = QueueManager(dispatch_cb=lambda _j: True,
                             path=tmp_path / "queue.json").list()
    assert remaining == []


def test_start_next_in_queue_missing_file_marks_failed(tmp_path, monkeypatch,
                                                       patched_serial):
    monkeypatch.setattr(QM, "_DEFAULT_PATH", tmp_path / "queue.json")
    QueueManager(dispatch_cb=lambda _j: True,
                 path=tmp_path / "queue.json").add(
        printer="", gcode=str(tmp_path / "gone.3mf"), label="Gone")
    rc = CT._start_next_in_queue(argparse.Namespace())
    assert rc == 1
    job = QueueManager(dispatch_cb=lambda _j: True,
                       path=tmp_path / "queue.json").list()[0]
    assert job.status == "failed"


def test_start_next_in_queue_cloud_print_failure_marks_failed(
        tmp_path, monkeypatch, patched_serial):
    monkeypatch.setattr(QM, "_DEFAULT_PATH", tmp_path / "queue.json")
    three_mf = tmp_path / "m.gcode.3mf"
    three_mf.write_bytes(b"x")
    QueueManager(dispatch_cb=lambda _j: True,
                 path=tmp_path / "queue.json").add(
        printer="", gcode=str(three_mf), label="M")
    monkeypatch.setattr("beambam.cli.cloud.cmd_cloud_print", lambda ns: 1)
    rc = CT._start_next_in_queue(argparse.Namespace())
    assert rc == 1
    job = QueueManager(dispatch_cb=lambda _j: True,
                       path=tmp_path / "queue.json").list()[0]
    assert job.status == "failed" and "rc=1" in job.error
