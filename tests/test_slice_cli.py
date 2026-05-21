"""Tests for the `beambam slice` subcommand (beambam.slice)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.slice import add_subparser, cmd_slice


def _parser():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    return p


def test_subparser_requires_stl_and_out():
    p = _parser()
    with pytest.raises(SystemExit):
        p.parse_args(["slice"])                          # missing stl + out


def test_subparser_requires_out():
    p = _parser()
    with pytest.raises(SystemExit):
        p.parse_args(["slice", "x.stl"])                 # missing --out


def test_subparser_minimal():
    p = _parser()
    args = p.parse_args(["slice", "x.stl", "-o", "x.gcode.3mf"])
    assert args.stl == Path("x.stl")
    assert args.out == Path("x.gcode.3mf")
    assert args.plate == 0
    assert args.scale == 1.0
    assert args.color is None
    assert args.bed is None
    assert args.keep_graft is False


def test_subparser_all_options():
    p = _parser()
    args = p.parse_args([
        "slice", "in.stl", "-o", "out.gcode.3mf",
        "--template", "tpl.gcode.3mf",
        "--plate", "2",
        "--scale", "0.5",
        "--color", "Gold",
        "--bed", "supertack",
        "--keep-graft",
    ])
    assert args.template == Path("tpl.gcode.3mf")
    assert args.plate == 2
    assert args.scale == 0.5
    assert args.color == "Gold"
    assert args.bed == "supertack"
    assert args.keep_graft is True


def test_cmd_slice_delegates_to_x2d_slice_main():
    """cmd_slice should rebuild argv and call x2d_slice.main()."""
    args = argparse.Namespace(
        stl=Path("model.stl"),
        out=Path("model.gcode.3mf"),
        template=Path("tpl.gcode.3mf"),
        plate=0,
        scale=1.0,
        color=None,
        bed=None,
        keep_graft=False,
    )
    saved_argv = sys.argv[:]
    with patch("x2d_slice.main", return_value=0) as main_fn:
        rc = cmd_slice(args)
    sys.argv = saved_argv
    assert rc == 0
    main_fn.assert_called_once()


def test_cmd_slice_passes_optional_flags():
    """Optional flags should be propagated through to x2d_slice's argv."""
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"),
        template=Path("t.3mf"), plate=1, scale=2.0,
        color="#FF00FF", bed="supertack", keep_graft=True,
    )
    captured_argv: list[list[str]] = []

    def fake_main():
        captured_argv.append(sys.argv[:])
        return 0

    saved = sys.argv[:]
    with patch("x2d_slice.main", side_effect=fake_main):
        cmd_slice(args)
    sys.argv = saved

    argv = captured_argv[0]
    assert "--color" in argv and "#FF00FF" in argv
    assert "--bed" in argv and "supertack" in argv
    assert "--keep-graft" in argv
    assert "--scale" in argv and "2.0" in argv
    assert "--plate" in argv and "1" in argv


def test_cmd_slice_omits_optional_flags_when_unset():
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"),
        template=Path("t.3mf"), plate=0, scale=1.0,
        color=None, bed=None, keep_graft=False,
    )
    captured_argv: list[list[str]] = []

    def fake_main():
        captured_argv.append(sys.argv[:])
        return 0

    saved = sys.argv[:]
    with patch("x2d_slice.main", side_effect=fake_main):
        cmd_slice(args)
    sys.argv = saved

    argv = captured_argv[0]
    assert "--color" not in argv
    assert "--bed" not in argv
    assert "--keep-graft" not in argv


def test_cmd_slice_restores_argv_on_error():
    """Even if x2d_slice.main raises, sys.argv must be restored."""
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"),
        template=Path("t.3mf"), plate=0, scale=1.0,
        color=None, bed=None, keep_graft=False,
    )
    original_argv = sys.argv[:]
    with patch("x2d_slice.main", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            cmd_slice(args)
    assert sys.argv == original_argv
