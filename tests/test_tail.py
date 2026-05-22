"""Tests for `beambam tail` — the push-based event-stream CLI.

`_TailDispatcher` is a pure diff engine extracted from `cmd_tail` so we
can drive it with synthetic state pushes without spinning up MQTT,
threads, or signal handlers. `_tail_print` is also pure (formatter only).

The real-printer path is exercised manually with a live X2D in the
commit message — these tests cover the diff and format correctness."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: F401

from beambam.cli.info import _TailDispatcher, _tail_print

def test_state_transitions_emit_observed_then_arrows():
    """The first push reports `observed X`; subsequent transitions
    report `X -> Y` so logs are unambiguous about direction."""
    d = _TailDispatcher()
    e1 = d.events_for({"print": {"gcode_state": "IDLE"}})
    e2 = d.events_for({"print": {"gcode_state": "PREPARE"}})
    e3 = d.events_for({"print": {"gcode_state": "RUNNING"}})
    msgs = [m for _, m, _ in (e1 + e2 + e3)]
    assert "observed IDLE" in msgs
    assert "IDLE -> PREPARE" in msgs
    assert "PREPARE -> RUNNING" in msgs


def test_state_transitions_to_failed_use_fail_level():
    d = _TailDispatcher()
    d.events_for({"print": {"gcode_state": "RUNNING"}})
    out = d.events_for({"print": {"gcode_state": "FAILED"}})
    assert len(out) == 1
    cat, msg, level = out[0]
    assert cat == "state" and "FAILED" in msg and level == "fail"


def test_state_unchanged_emits_nothing():
    """Repeated pushes of the same state should not emit any event."""
    d = _TailDispatcher()
    d.events_for({"print": {"gcode_state": "RUNNING"}})
    assert d.events_for({"print": {"gcode_state": "RUNNING"}}) == []


def test_progress_milestones_fire_on_10pct_buckets():
    """Crossing each 10 % bucket fires exactly one progress line."""
    d = _TailDispatcher()
    d.events_for({"print": {"mc_percent": 0}})    # init prev
    e1 = d.events_for({"print": {"mc_percent": 5}})  # same bucket
    e2 = d.events_for({"print": {"mc_percent": 13}})  # crossed 10
    e3 = d.events_for({"print": {"mc_percent": 27}})  # crossed 20
    e4 = d.events_for({"print": {"mc_percent": 100}})  # finished
    progress = [m for evs in (e1, e2, e3, e4) for cat, m, _ in evs
                if cat == "progress"]
    assert e1 == []
    assert progress == ["13%", "27%", "100% — print finished"]


def test_progress_finished_level_is_ok():
    """The 100 % milestone is an `ok` (✓ not ·) event to distinguish
    print completion from mid-print progress."""
    d = _TailDispatcher()
    d.events_for({"print": {"mc_percent": 50}})
    out = d.events_for({"print": {"mc_percent": 100}})
    cats = {(c, lvl) for c, _, lvl in out}
    assert ("progress", "ok") in cats


def test_no_progress_flag_suppresses_progress():
    d = _TailDispatcher(no_progress=True)
    d.events_for({"print": {"mc_percent": 0}})
    out = d.events_for({"print": {"mc_percent": 50}})
    assert all(cat != "progress" for cat, _, _ in out)


def test_hms_add_uses_fail_level_with_decoded_message():
    """A new HMS code in the state surfaces as a fail event with the
    code in canonical form and the decoded human description."""
    d = _TailDispatcher()
    d.events_for({"print": {"hms": []}})
    out = d.events_for({"print": {"hms": [
        {"a": 0x0500, "b": 0x0300, "c": 0x0002, "d": 0x0002}
    ]}})
    assert len(out) == 1
    cat, msg, level = out[0]
    assert cat == "hms" and level == "fail"
    assert "0500_0300_0002_0002" in msg
    assert "Chamber fan" in msg  # from HMS_DESCRIPTIONS lookup


def test_hms_clear_uses_ok_level():
    d = _TailDispatcher()
    d.events_for({"print": {"hms": [
        {"a": 0x0500, "b": 0x0300, "c": 0x0002, "d": 0x0002}
    ]}})
    out = d.events_for({"print": {"hms": []}})
    assert len(out) == 1
    cat, msg, level = out[0]
    assert cat == "hms" and level == "ok" and "cleared" in msg


def test_hms_overlapping_add_and_clear_in_one_push():
    """If a push swaps one active code for another, dispatcher reports
    both events — the new one as fail, the gone one as ok."""
    d = _TailDispatcher()
    d.events_for({"print": {"hms": [
        {"a": 0x0500, "b": 0x0300, "c": 0x0002, "d": 0x0002}
    ]}})
    out = d.events_for({"print": {"hms": [
        {"a": 0x1000, "b": 0xC001, "c": 0x0000, "d": 0x0000}
    ]}})
    levels = {lvl for _, _, lvl in out}
    assert levels == {"fail", "ok"}, out


def test_no_hms_flag_suppresses_both_add_and_clear():
    d = _TailDispatcher(no_hms=True)
    d.events_for({"print": {"hms": []}})
    out = d.events_for({"print": {"hms": [{"a": 1, "b": 2, "c": 3, "d": 4}]}})
    assert all(cat != "hms" for cat, _, _ in out)


def test_layer_changes_default_off():
    d = _TailDispatcher()
    d.events_for({"print": {"layer_num": 5, "total_layer_num": 100}})
    out = d.events_for({"print": {"layer_num": 6, "total_layer_num": 100}})
    assert all(cat != "layer" for cat, _, _ in out)


def test_layer_changes_emit_under_every_state():
    d = _TailDispatcher(every_state=True)
    d.events_for({"print": {"layer_num": 5, "total_layer_num": 100}})
    out = d.events_for({"print": {"layer_num": 6, "total_layer_num": 100}})
    assert ("layer", "L6/100", "info") in out


def test_hms_int_attr_code_form_is_split_into_four_groups():
    """The X2D firmware reports HMS as `{attr: int, code: int}` where
    each int is 32 bits encoding two 16-bit hex groups. The dispatcher
    must split each into high/low halves so the resulting
    `AAAA_BBBB_CCCC_DDDD` form matches HMS_DESCRIPTIONS keys + Bambu
    error-page URLs. Caught by the live test against the real printer
    — without this split, the lookup against `0702_0000_0002_0025`
    would miss because the dispatcher would emit `117575680_131109`."""
    d = _TailDispatcher()
    d.events_for({"print": {"hms": []}})
    out = d.events_for({"print": {"hms": [
        # attr=0x07020000 (117_575_680), code=0x00020025 (131_109)
        {"attr": 0x07020000, "code": 0x00020025}
    ]}})
    assert len(out) == 1
    cat, msg, _ = out[0]
    assert cat == "hms"
    assert "0702_0000_0002_0025" in msg


# --- _tail_print formatter -------------------------------------------

def test_tail_print_human_format_includes_icon_and_category():
    buf = io.StringIO()
    with redirect_stdout(buf):
        _tail_print([("state", "IDLE -> RUNNING", "info"),
                     ("hms", "0000_0000_0000_0000: dummy", "fail")],
                    as_json=False)
    out = buf.getvalue()
    assert "·" in out and "state" in out and "IDLE -> RUNNING" in out
    assert "✗" in out and "hms" in out


def test_tail_print_json_format_is_ndjson_with_schema():
    """--json output must be one JSON object per line with
    {ts, category, level, message}."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        _tail_print([("state", "IDLE -> RUNNING", "info"),
                     ("hms", "code cleared", "ok")],
                    as_json=True)
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 2
    for ln in lines:
        obj = json.loads(ln)
        assert set(obj.keys()) == {"ts", "category", "level", "message"}
        assert isinstance(obj["ts"], (int, float))


def test_tail_print_empty_input_writes_nothing():
    buf = io.StringIO()
    with redirect_stdout(buf):
        _tail_print([], as_json=False)
        _tail_print([], as_json=True)
    assert buf.getvalue() == ""
