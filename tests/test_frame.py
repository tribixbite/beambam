"""Tests for beambam.frame — frame-STL preset wrapper."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.frame import FRAME_PRESETS, FramePreset, add_subparser, cmd_frame


def test_builtin_presets_have_text():
    """Every preset must have at least a non-empty `text` field —
    otherwise the deboss step produces an empty STL."""
    assert len(FRAME_PRESETS) >= 4
    for name, preset in FRAME_PRESETS.items():
        assert isinstance(preset, FramePreset), name
        assert preset.text and preset.text.strip(), name


def test_known_presets():
    for name in ("mira", "rumi", "zoey", "huntrx"):
        assert name in FRAME_PRESETS, f"missing preset: {name}"


def test_huntrx_text_includes_slash():
    """The huntrx preset spells 'HUNTR/X' literally."""
    assert FRAME_PRESETS["huntrx"].text == "HUNTR/X"


def test_subparser_registers_frame_command():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    add_subparser(sub)
    args = p.parse_args(["frame", "--preset", "mira", "--out", "/tmp/x.stl"])
    assert args.preset == "mira"
    assert args.out == "/tmp/x.stl"


def test_cmd_frame_rejects_missing_text(capsys):
    """Neither --preset nor --text → exit code 2 + stderr message."""
    ns = argparse.Namespace(preset=None, text=None, top_text=None,
                            out="/tmp/x.stl", deboss_depth=0.6, height=1.2)
    rc = cmd_frame(ns)
    assert rc == 2
    captured = capsys.readouterr()
    assert "--preset" in captured.err or "--text" in captured.err


def test_subparser_rejects_both_preset_and_text():
    """argparse mutual exclusion fires before cmd_frame even runs."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["frame", "--preset", "mira", "--text", "X",
                      "--out", "/tmp/x.stl"])
