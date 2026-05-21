"""Tests for beambam.cloud_data — history + whoami CLIs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.cloud_data import (
    add_subparser,
    cmd_history,
    cmd_whoami,
    fetch_history,
    fetch_whoami,
    format_history,
    format_whoami,
)


# ----- formatters ---------------------------------------------------------


def test_format_history_empty_explains():
    text = format_history([])
    assert "no recent cloud tasks" in text
    assert "region" in text


def test_format_history_with_tasks():
    tasks = [
        {"id": 962344529, "title": "Eevee print",
         "designId": 1501027, "status": "FINISH", "weight": 33.67},
        {"id": 962344530, "subtaskName": "P1S preset",
         "designId": 0, "status": "RUNNING", "weight": 0},
    ]
    text = format_history(tasks)
    assert "2 recent task" in text
    assert "Eevee print" in text
    assert "FINISH" in text
    assert "33.7g" in text
    # Untitled fallback
    text2 = format_history([{"id": 1, "status": "X"}])
    assert "<untitled>" in text2


def test_format_whoami_basic():
    profile = {
        "uid": 2000000001, "name": "will", "handle": "tribixbite",
        "account": "x@y.z", "avatar": "https://...",
        "fanCount": 5, "followCount": 2,
    }
    text = format_whoami(profile)
    assert "uid:        2000000001" in text
    assert "name:       will" in text
    assert "@tribixbite" in text
    assert "followers:  5" in text


def test_format_whoami_with_optional_bio():
    profile = {"uid": 1, "name": "a", "handle": "b", "account": "c",
               "fanCount": 0, "followCount": 0, "bio": "I print stuff"}
    text = format_whoami(profile)
    assert "I print stuff" in text


# ----- subparser ----------------------------------------------------------


def test_subparser_registers_both_commands():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    add_subparser(sub)
    # history defaults
    h = p.parse_args(["history"])
    assert h.cmd == "history" and h.limit == 20 and h.json_out is False
    h2 = p.parse_args(["history", "--limit", "5", "--json"])
    assert h2.limit == 5 and h2.json_out is True
    # whoami defaults
    w = p.parse_args(["whoami"])
    assert w.cmd == "whoami" and w.json_out is False


# ----- cmd_history --------------------------------------------------------


def test_cmd_history_calls_fetch_with_limit(capsys):
    args = argparse.Namespace(limit=5, json_out=False)
    with patch("beambam.cloud_data.fetch_history") as fetch:
        fetch.return_value = [{"id": 1, "title": "t",
                               "status": "F", "weight": 1}]
        rc = cmd_history(args)
    assert rc == 0
    fetch.assert_called_once_with(limit=5)
    assert "recent task" in capsys.readouterr().out


def test_cmd_history_clamps_limit_to_100(capsys):
    args = argparse.Namespace(limit=500, json_out=False)
    with patch("beambam.cloud_data.fetch_history") as fetch:
        fetch.return_value = []
        cmd_history(args)
    fetch.assert_called_once_with(limit=100)


def test_cmd_history_json_output(capsys):
    args = argparse.Namespace(limit=5, json_out=True)
    with patch("beambam.cloud_data.fetch_history",
                return_value=[{"id": 1}]):
        cmd_history(args)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["id"] == 1


def test_cmd_history_handles_cloud_error(capsys):
    args = argparse.Namespace(limit=5, json_out=False)
    with patch("beambam.cloud_data.fetch_history",
                side_effect=RuntimeError("not logged in")):
        rc = cmd_history(args)
    assert rc == 1
    assert "history failed" in capsys.readouterr().err


# ----- cmd_whoami ---------------------------------------------------------


def test_cmd_whoami_prints_profile(capsys):
    args = argparse.Namespace(json_out=False)
    profile = {"uid": 1, "name": "a", "handle": "b", "account": "c",
               "fanCount": 0, "followCount": 0}
    with patch("beambam.cloud_data.fetch_whoami", return_value=profile):
        rc = cmd_whoami(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "uid:" in out and "name:" in out


def test_cmd_whoami_json_output(capsys):
    args = argparse.Namespace(json_out=True)
    profile = {"uid": 7, "name": "x", "handle": "y", "account": "z",
               "fanCount": 0, "followCount": 0}
    with patch("beambam.cloud_data.fetch_whoami", return_value=profile):
        cmd_whoami(args)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["uid"] == 7


def test_cmd_whoami_handles_auth_error(capsys):
    args = argparse.Namespace(json_out=False)
    with patch("beambam.cloud_data.fetch_whoami",
                side_effect=RuntimeError("not logged in")):
        rc = cmd_whoami(args)
    assert rc == 1
    assert "whoami failed" in capsys.readouterr().err


# ----- live ---------------------------------------------------------------


@pytest.mark.live
def test_live_whoami_returns_user_id(live_printer):
    """Real Bambu Cloud round-trip — fetch_whoami must include a uid."""
    profile = fetch_whoami()
    assert "uid" in profile
    assert profile["uid"]
