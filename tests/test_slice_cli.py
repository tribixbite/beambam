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


def test_cmd_slice_forwards_resize_and_copies_flags():
    """New flags (2026-05-21) — --scale-pct / --mm / --copies — must
    reach x2d_slice.main's argv when set."""
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"),
        template=Path("t.3mf"), plate=0, scale=1.0,
        scale_pct=50.0, mm=120.0, copies=4,
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
    assert "--scale-pct" in argv and "50.0" in argv
    assert "--mm" in argv and "120.0" in argv
    assert "--copies" in argv and "4" in argv


def test_cmd_slice_omits_new_flags_at_defaults():
    """copies=1 + scale_pct=None + mm=None: NO flag in forwarded argv."""
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"),
        template=Path("t.3mf"), plate=0, scale=1.0,
        scale_pct=None, mm=None, copies=1,
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
    assert "--scale-pct" not in argv
    assert "--mm" not in argv
    assert "--copies" not in argv


def test_subparser_accepts_multicolor_flags():
    """--colors / --color-by-region (the full x2d_slice option set) parse."""
    p = _parser()
    args = p.parse_args([
        "slice", "in.stl", "-o", "out.gcode.3mf",
        "--colors", "Gold,Red,Blue",
        "--color-by-region", "regions.json",
        "--orient", "flat",
    ])
    assert args.colors == "Gold,Red,Blue"
    assert args.color_by_region == "regions.json"
    assert args.orient == "flat"


def test_cmd_slice_forwards_multicolor_flags():
    """--colors / --color-by-region must reach x2d_slice.main's argv."""
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"), template=Path("t.3mf"),
        plate=0, scale=1.0, scale_pct=None, mm=None, copies=1,
        color=None, colors="Gold,Red", color_by_region="r.json",
        bed=None, keep_graft=False, orient="original",
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
    assert "--colors" in argv and "Gold,Red" in argv
    assert "--color-by-region" in argv and "r.json" in argv


def test_cmd_slice_omits_multicolor_when_unset():
    args = argparse.Namespace(
        stl=Path("m.stl"), out=Path("m.3mf"), template=Path("t.3mf"),
        plate=0, scale=1.0, scale_pct=None, mm=None, copies=1,
        color=None, colors=None, color_by_region=None,
        bed=None, keep_graft=False, orient="original",
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
    assert "--colors" not in argv
    assert "--color-by-region" not in argv


def test_slice_print_help_lists_full_slice_options():
    """`slice-print --help` must expose the multi-color + orient slice
    options (parity with `beambam slice` / x2d_slice.py)."""
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "beambam.cli", "slice-print", "--help"],
        capture_output=True, text=True, timeout=20,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert r.returncode == 0, r.stderr
    for flag in ("--colors", "--color-by-region", "--orient"):
        assert flag in r.stdout, f"{flag} missing from `slice-print --help`"


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
