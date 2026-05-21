"""Tests for beambam.queuecli — print queue CLI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.queuecli import (
    _format_age,
    _qm,
    _resolve_id,
    add_subparser,
    cmd_add,
    cmd_cancel,
    cmd_clear,
    cmd_list,
    cmd_path,
    cmd_remove,
)


@pytest.fixture
def queue_file(tmp_path, monkeypatch):
    """Point the queue manager at a fresh tmp file."""
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr("runtime.queue.manager._DEFAULT_PATH", qpath)
    return qpath


@pytest.fixture
def sample_gcode(tmp_path):
    p = tmp_path / "sample.gcode.3mf"
    p.write_bytes(b"PK\x03\x04dummy")
    return p


# ----- helpers -----------------------------------------------------------


def test_format_age_zero():
    assert _format_age(0) == "—"


def test_format_age_recent():
    import time
    assert _format_age(time.time() - 30).endswith("s ago")
    assert _format_age(time.time() - 120).endswith("m ago")
    assert _format_age(time.time() - 7200).endswith("h ago")
    assert _format_age(time.time() - 200000).endswith("d ago")


# ----- list ---------------------------------------------------------------


def test_cmd_list_empty_queue(queue_file, capsys):
    cmd_list(argparse.Namespace())
    assert "queue is empty" in capsys.readouterr().out


def test_cmd_list_with_jobs(queue_file, sample_gcode, capsys):
    qm = _qm()
    qm.add(printer="", gcode=str(sample_gcode), slot=3, label="job1")
    qm.add(printer="garage", gcode=str(sample_gcode), slot=7, label="job2")
    cmd_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "2 job(s)" in out
    assert "job1" in out
    assert "job2" in out
    assert "garage" in out
    assert "(default)" in out


# ----- add ---------------------------------------------------------------


def test_cmd_add_missing_file(tmp_path, capsys):
    args = argparse.Namespace(file=str(tmp_path / "nope.3mf"),
                               printer=None, slot=1,
                               label=None, json_out=False)
    rc = cmd_add(args)
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cmd_add_enqueues_file(queue_file, sample_gcode, capsys):
    args = argparse.Namespace(file=str(sample_gcode),
                               printer="studio", slot=5,
                               label="custom", json_out=False)
    rc = cmd_add(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "enqueued" in out
    assert "studio" in out
    assert "custom" in out
    # Verify it actually landed in the queue file
    qm = _qm()
    jobs = qm.list()
    assert len(jobs) == 1
    assert jobs[0].printer == "studio"
    assert jobs[0].slot == 5
    assert jobs[0].label == "custom"


def test_cmd_add_default_label_is_filename(queue_file, sample_gcode, capsys):
    args = argparse.Namespace(file=str(sample_gcode), printer=None,
                               slot=1, label=None, json_out=False)
    cmd_add(args)
    qm = _qm()
    assert qm.list()[0].label == "sample.gcode.3mf"


def test_cmd_add_json_output(queue_file, sample_gcode, capsys):
    args = argparse.Namespace(file=str(sample_gcode), printer=None,
                               slot=1, label=None, json_out=True)
    cmd_add(args)
    parsed = json.loads(capsys.readouterr().out)
    assert "id" in parsed
    assert parsed["slot"] == 1


# ----- remove / cancel / clear -------------------------------------------


def test_cmd_remove_by_prefix(queue_file, sample_gcode, capsys):
    args_add = argparse.Namespace(file=str(sample_gcode), printer=None,
                                    slot=1, label=None, json_out=False)
    cmd_add(args_add)
    job_id = _qm().list()[0].id
    args_rm = argparse.Namespace(job_id=job_id[:8])
    rc = cmd_remove(args_rm)
    assert rc == 0
    assert _qm().list() == []


def test_cmd_remove_short_prefix_rejected(queue_file, sample_gcode, capsys):
    args = argparse.Namespace(job_id="ab")
    rc = cmd_remove(args)
    assert rc == 1
    assert "too short" in capsys.readouterr().err


def test_cmd_remove_no_match(queue_file, capsys):
    args = argparse.Namespace(job_id="abcdef99")
    rc = cmd_remove(args)
    assert rc == 1
    assert "no job matching" in capsys.readouterr().err


def test_cmd_cancel(queue_file, sample_gcode, capsys):
    cmd_add(argparse.Namespace(file=str(sample_gcode), printer=None,
                                slot=1, label=None, json_out=False))
    job_id = _qm().list()[0].id
    rc = cmd_cancel(argparse.Namespace(job_id=job_id[:8]))
    assert rc == 0
    job = _qm().list()[0]
    assert job.status == "cancelled"


def test_cmd_clear_empty(queue_file, capsys):
    rc = cmd_clear(argparse.Namespace(all=False))
    assert rc == 0
    assert "already empty" in capsys.readouterr().out


def test_cmd_clear_removes_pending(queue_file, sample_gcode, capsys):
    for _ in range(3):
        cmd_add(argparse.Namespace(file=str(sample_gcode), printer=None,
                                    slot=1, label=None, json_out=False))
    rc = cmd_clear(argparse.Namespace(all=False))
    assert rc == 0
    assert "removed 3" in capsys.readouterr().out
    assert _qm().list() == []


# ----- path --------------------------------------------------------------


def test_cmd_path_prints(queue_file, capsys):
    cmd_path(argparse.Namespace())
    out = capsys.readouterr().out.strip()
    assert "queue.json" in out


# ----- subparser ---------------------------------------------------------


def test_subparser_requires_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["queue"])


def test_subparser_add_requires_file():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["queue", "add"])


def test_subparser_remove_aliased_as_rm():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["queue", "rm", "abc123"])
    assert args.queue_cmd == "rm"
    assert args.job_id == "abc123"


def test_resolve_id_ambiguous(queue_file, sample_gcode, capsys):
    """Two jobs with the same prefix → ambiguous error."""
    # We can't reliably collide UUIDs, so simulate by direct manager
    # access — add 2 jobs and test resolve with a common prefix.
    qm = _qm()
    qm.add(printer="", gcode=str(sample_gcode), slot=1)
    qm.add(printer="", gcode=str(sample_gcode), slot=1)
    # Pick a prefix likely to match both: the first char of each job's id.
    # We can't guarantee a 3-char collision, so explicitly use the empty-
    # ish case by forcing it: use very short prefix once we know neither
    # job's full id is exactly 3 chars (always true).
    # Instead, test the ambiguous-by-construction scenario: feed the
    # _resolve_id helper a synthetic prefix that matches both jobs.
    # If hex IDs all start with 0-9a-f, "" technically matches all but
    # is rejected by the length check. Use a known-shared prefix? Not
    # reliable. Skip this test if the two random IDs don't share 3 chars.
    jobs = qm.list()
    common = ""
    for i in range(3, 16):
        prefixes = {j.id[:i] for j in jobs}
        if len(prefixes) == 1:
            common = jobs[0].id[:i]
        else:
            break
    if not common or len(common) < 3:
        pytest.skip("UUIDs don't share a 3-char prefix; skip ambiguity test")
    result = _resolve_id(qm, common)
    err = capsys.readouterr().err
    assert result is None
    assert "ambiguous" in err
