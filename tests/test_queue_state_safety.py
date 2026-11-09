"""Regression tests for the print queue's state handling.

Audit findings covered:

* P0 — any message without ``gcode_state`` (a delta report, a command
  reply) counted as idle: it marked the running job done and dispatched
  the next job mid-print.
* P2 — the daemon ran queue dispatch (FTPS upload + request_state) on
  paho's network thread, so request_state always timed out.
* P3 — the CLI and the daemon raced on queue.json: the daemon rewrote
  from a stale in-memory copy and a CLI load flipped running jobs back
  to pending.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam.cli.daemon import PrinterStateFanout  # noqa: E402
from runtime.queue import manager as QM  # noqa: E402
from runtime.queue.manager import (  # noqa: E402
    JobFileError,
    QueueManager,
    _is_printer_idle,
    validate_job_file,
)


def _report(gcode_state: str, **extra) -> dict:
    return {"print": {"command": "push_status", "gcode_state": gcode_state,
                      **extra}}


class _Recorder:
    def __init__(self) -> None:
        self.jobs: list = []

    def __call__(self, job) -> bool:
        self.jobs.append(job)
        return True


# --- idle judgement ------------------------------------------------------

@pytest.mark.parametrize("msg", [
    {},
    {"print": {"nozzle_temper": 212.0}},                       # delta
    {"print": {"command": "pause", "result": "success"}},      # reply
    {"system": {"command": "ledctrl", "result": "success"}},   # reply
    {"print": {"gcode_state": ""}},
])
def test_message_without_gcode_state_is_never_idle(msg):
    assert _is_printer_idle(msg) is False


def test_delta_does_not_complete_running_job_or_dispatch(tmp_path):
    rec = _Recorder()
    qm = QueueManager(dispatch_cb=rec, path=tmp_path / "q.json")
    a = qm.add(printer="", gcode="/x/a.gcode.3mf")
    b = qm.add(printer="", gcode="/x/b.gcode.3mf")
    qm.on_state("", _report("IDLE"))
    assert [j.id for j in rec.jobs] == [a.id]
    qm.on_state("", _report("RUNNING", mc_percent=40))
    # Mid-print deltas and replies: nothing may change.
    for msg in ({"print": {"mc_percent": 41}},
                {"print": {"command": "ledctrl", "result": "success"}},
                {"info": {"command": "get_version"}}):
        qm.on_state("", msg)
    assert qm.get(a.id).status == "running"
    assert qm.get(b.id).status == "pending"
    assert len(rec.jobs) == 1


def test_stale_finish_after_dispatch_does_not_complete_job(tmp_path):
    """Right after dispatch the printer still reports FINISH for the
    previous print; that must not complete the job just dispatched."""
    rec = _Recorder()
    qm = QueueManager(dispatch_cb=rec, path=tmp_path / "q.json")
    a = qm.add(printer="", gcode="/x/a.gcode.3mf")
    qm.add(printer="", gcode="/x/b.gcode.3mf")
    qm.on_state("", _report("FINISH", mc_percent=100))
    qm.on_state("", _report("FINISH", mc_percent=100))
    assert qm.get(a.id).status == "running"
    assert len(rec.jobs) == 1
    qm.on_state("", _report("PREPARE"))
    qm.on_state("", _report("FINISH", mc_percent=100))
    assert qm.get(a.id).status == "done"
    assert len(rec.jobs) == 2


def test_failed_print_marks_job_failed(tmp_path):
    qm = QueueManager(dispatch_cb=_Recorder(), path=tmp_path / "q.json")
    a = qm.add(printer="", gcode="/x/a.gcode.3mf")
    qm.on_state("", _report("IDLE"))
    qm.on_state("", _report("RUNNING", mc_percent=5))
    qm.on_state("", _report("FAILED", mc_percent=5))
    job = qm.get(a.id)
    assert job.status == "failed" and "FAILED" in job.error


def test_job_never_started_fails_after_grace(tmp_path, monkeypatch):
    qm = QueueManager(dispatch_cb=_Recorder(), path=tmp_path / "q.json")
    a = qm.add(printer="", gcode="/x/a.gcode.3mf")
    qm.on_state("", _report("IDLE"))
    monkeypatch.setattr(QM, "_START_GRACE_S", 0.0)
    time.sleep(0.01)
    qm.on_state("", _report("IDLE"))
    job = qm.get(a.id)
    assert job.status == "failed" and "after dispatch" in job.error


# --- background dispatch ---------------------------------------------------

def test_background_dispatch_returns_before_callback_finishes(tmp_path):
    release = threading.Event()
    entered = threading.Event()

    def slow_cb(_job) -> bool:
        entered.set()
        release.wait(5)
        return True

    qm = QueueManager(dispatch_cb=slow_cb, path=tmp_path / "q.json",
                      background_dispatch=True)
    a = qm.add(printer="", gcode="/x/a.gcode.3mf")
    t0 = time.monotonic()
    qm.on_state("", _report("IDLE"))
    assert time.monotonic() - t0 < 1.0          # did not block the caller
    assert entered.wait(5)
    assert qm.get(a.id).status == "running"
    # An idle report while dispatch is in flight changes nothing.
    qm.on_state("", _report("IDLE"))
    assert qm.get(a.id).status == "running"
    release.set()
    assert qm.wait_idle(5)
    assert qm.get(a.id).status == "running"


def test_background_dispatch_failure_marks_failed(tmp_path):
    def boom(_job) -> bool:
        raise RuntimeError("ftps refused")

    qm = QueueManager(dispatch_cb=boom, path=tmp_path / "q.json",
                      background_dispatch=True)
    a = qm.add(printer="", gcode="/x/a.gcode.3mf")
    qm.on_state("", _report("IDLE"))
    assert qm.wait_idle(5)
    job = qm.get(a.id)
    assert job.status == "failed" and "ftps refused" in job.error


# --- file races --------------------------------------------------------------

def test_second_manager_keeps_running_jobs_running(tmp_path):
    path = tmp_path / "q.json"
    daemon = QueueManager(dispatch_cb=_Recorder(), path=path)
    a = daemon.add(printer="", gcode="/x/a.gcode.3mf")
    daemon.on_state("", _report("IDLE"))
    cli = QueueManager(dispatch_cb=lambda _j: False, path=path)
    assert cli.get(a.id).status == "running"
    # ... and the daemon still sees it running after the CLI's load.
    assert daemon.get(a.id).status == "running"


def test_edits_from_two_managers_are_not_lost(tmp_path):
    path = tmp_path / "q.json"
    daemon = QueueManager(dispatch_cb=_Recorder(), path=path)
    cli = QueueManager(dispatch_cb=lambda _j: False, path=path)
    a = daemon.add(printer="", gcode="/x/a.gcode.3mf")
    b = cli.add(printer="", gcode="/x/b.gcode.3mf")
    # The daemon dispatches from its (formerly stale) copy: b must survive.
    daemon.on_state("", _report("IDLE"))
    ids = {j.id for j in cli.list()}
    assert ids == {a.id, b.id}
    assert cli.cancel(b.id)
    assert daemon.get(b.id).status == "cancelled"


def test_concurrent_adds_from_threads_and_managers(tmp_path):
    path = tmp_path / "q.json"
    managers = [QueueManager(dispatch_cb=lambda _j: False, path=path)
                for _ in range(4)]

    def worker(m: QueueManager, n: int) -> None:
        for i in range(10):
            m.add(printer="", gcode=f"/x/{n}_{i}.gcode.3mf")

    threads = [threading.Thread(target=worker, args=(m, n))
               for n, m in enumerate(managers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(managers[0].list()) == 40
    assert not path.with_name("q.json.lock").exists()


def test_stale_lock_file_is_broken(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    lock = path.with_name("q.json.lock")
    lock.write_text("99999\n")
    old = time.time() - 3600
    import os
    os.utime(lock, (old, old))
    qm = QueueManager(dispatch_cb=lambda _j: False, path=path)
    qm.add(printer="", gcode="/x/a.gcode.3mf")
    assert len(qm.list()) == 1


# --- slot convention (0-based global slot) --------------------------------------

def test_dispatched_slot_is_the_zero_based_global_slot(tmp_path):
    """The queue's slot goes to start_print/_validate_ams_slot unchanged,
    which take 0..15: slot 0 must reach the dispatcher as 0 (it used to
    be entered as 1..16 and printed from the next tray)."""
    rec = _Recorder()
    qm = QueueManager(dispatch_cb=rec, path=tmp_path / "q.json")
    qm.add(printer="", gcode="/x/a.gcode.3mf", slot=0)
    qm.on_state("", _report("IDLE"))
    assert rec.jobs[0].slot == 0
    with pytest.raises(ValueError):
        qm.add(printer="", gcode="/x/a.gcode.3mf", slot=16)


def test_legacy_one_based_slots_are_migrated(tmp_path):
    import json as _json
    path = tmp_path / "q.json"
    path.write_text(_json.dumps({"jobs": [
        {"id": "a", "printer": "", "gcode": "/x/a.gcode.3mf", "slot": 1,
         "status": "pending"},
        {"id": "b", "printer": "", "gcode": "/x/b.gcode.3mf", "slot": 16,
         "status": "pending"},
    ]}))
    qm = QueueManager(dispatch_cb=_Recorder(), path=path)
    assert [j.slot for j in qm.list()] == [0, 15]
    # Rewritten records carry slot_base and are not converted again.
    qm.add(printer="", gcode="/x/c.gcode.3mf", slot=4)
    stored = _json.loads(path.read_text())["jobs"]
    assert [(j["slot"], j["slot_base"]) for j in stored] == \
        [(0, 0), (15, 0), (4, 0)]
    assert [j.slot for j in QueueManager(dispatch_cb=_Recorder(),
                                         path=path).list()] == [0, 15, 4]


def test_queue_cli_slot_is_zero_based(tmp_path, monkeypatch):
    import argparse
    from beambam.queuecli import add_subparser
    p = argparse.ArgumentParser()
    add_subparser(p.add_subparsers(dest="cmd"))
    assert p.parse_args(["queue", "add", "f.gcode.3mf"]).slot == 0
    assert p.parse_args(["queue", "add", "f.gcode.3mf", "--slot", "15"]).slot == 15
    with pytest.raises(SystemExit):
        p.parse_args(["queue", "add", "f.gcode.3mf", "--slot", "16"])


# --- job file validation ------------------------------------------------------

def test_validate_job_file_accepts_existing_gcode_3mf(tmp_path):
    f = tmp_path / "part.gcode.3mf"
    f.write_bytes(b"PK\x03\x04")
    assert validate_job_file(f) == f.resolve()


@pytest.mark.parametrize("name", ["credentials", "part.3mf", "part.gcode",
                                  "notes.txt"])
def test_validate_job_file_rejects_other_files(tmp_path, name):
    f = tmp_path / name
    f.write_text("secret")
    with pytest.raises(JobFileError):
        validate_job_file(f)


def test_queue_cli_add_refuses_non_gcode_3mf(tmp_path, monkeypatch, capsys):
    import argparse
    from beambam.queuecli import cmd_add
    monkeypatch.setattr(QM, "_DEFAULT_PATH", tmp_path / "q.json")
    secret = tmp_path / "credentials"
    secret.write_text("[printer]\ncode=12345678\n")
    rc = cmd_add(argparse.Namespace(file=str(secret), printer=None, slot=1,
                                    label=None, json_out=False))
    assert rc == 2
    assert "gcode.3mf" in capsys.readouterr().err
    assert QueueManager(dispatch_cb=lambda _j: False,
                        path=tmp_path / "q.json").list() == []


def test_validate_job_file_rejects_missing_and_dirs(tmp_path):
    with pytest.raises(JobFileError):
        validate_job_file(tmp_path / "gone.gcode.3mf")
    d = tmp_path / "dir.gcode.3mf"
    d.mkdir()
    with pytest.raises(JobFileError):
        validate_job_file(d)


# --- daemon fan-out -------------------------------------------------------------

class _Consumer:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    def on_state(self, printer: str, state: dict | None) -> None:
        assert state is not None
        self.seen.append(state)


class _Hub:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, state) -> None:
        self.published.append(state)


def test_fanout_hands_consumers_the_merged_state():
    consumer, hub = _Consumer(), _Hub()
    fo = PrinterStateFanout("lab", hub=hub, consumers=(consumer, None))
    fo(_report("RUNNING", mc_percent=10, nozzle_temper=210.0))
    fo({"print": {"mc_percent": 11}})                  # delta
    fo({"print": {"command": "pause", "result": "success"}})  # reply
    last = consumer.seen[-1]["print"]
    assert last["gcode_state"] == "RUNNING"
    assert last["mc_percent"] == 11
    assert last["nozzle_temper"] == 210.0
    assert hub.published[-1] == consumer.seen[-1]
    assert fo.snapshot == consumer.seen[-1]
    # Snapshots are independent copies (HTTP threads read them).
    assert consumer.seen[0]["print"]["mc_percent"] == 10


def test_fanout_delta_mid_print_does_not_dispatch(tmp_path):
    rec = _Recorder()
    qm = QueueManager(dispatch_cb=rec, path=tmp_path / "q.json")
    qm.add(printer="lab", gcode="/x/a.gcode.3mf")
    fo = PrinterStateFanout("lab", consumers=(qm,))
    fo(_report("RUNNING", mc_percent=30))     # someone else's print
    fo({"print": {"mc_percent": 31}})
    fo({"print": {"command": "ledctrl", "result": "success"}})
    assert rec.jobs == []


def test_fanout_consumer_exception_does_not_stop_others(capsys):
    class Bad:
        def on_state(self, printer, state):
            raise RuntimeError("bad consumer")

    good = _Consumer()
    fo = PrinterStateFanout("", consumers=(Bad(), good))
    fo(_report("IDLE"))
    assert len(good.seen) == 1
    assert "bad consumer" in capsys.readouterr().err
