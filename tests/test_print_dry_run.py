"""tests/test_print_dry_run.py — `beambam print --dry-run` early-exit.

The --dry-run path of cmd_print MUST short-circuit BEFORE Creds.resolve()
runs (so users without a `~/.x2d/credentials` file can still sanity-check
a sliced .3mf), then refuse to proceed if the predicted purge waste
exceeds --max-flush-g.

What we pin down:
  * dry_run=True with low flush → exit 0, no network surface touched
  * dry_run=True with high flush → exit 2 + stderr "REFUSED" message
  * --max-flush-g threshold is honored
  * Creds.resolve is NEVER called when --dry-run is set (so the
    handler works on a workstation that isn't on the printer's LAN)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _ns(**kw) -> argparse.Namespace:
    """argparse.Namespace mirroring the `print` subparser's defaults."""
    defaults = dict(
        ip="192.168.0.42", code="abcdef12", serial="00P9AJ000000000",
        printer=None, config=None,
        file="/dev/null",       # overridden by tests
        remote=None, slot=0,
        no_upload=False, no_ams=False, no_bed_level=False,
        bed_type=None, bed_temp=None, force=False,
        flow_cali=False, timelapse=False, vib_cali=False,
        dry_run=True, max_flush_g=10.0,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ----- analyze stub -----------------------------------------------------


def _stub_analyze(flush_g: float):
    """Return a fake `(analyze_3mf, format_report)` pair the dry-run path
    can pull via `from beambam.analyze import analyze_3mf, format_report`.

    We monkeypatch the *module-level* attributes so the local `from … import`
    inside cmd_print resolves to our stubs."""
    fake_report = SimpleNamespace(
        totals={"flush_volume_g": flush_g, "flush_volume_mm": flush_g * 100},
        # The full Report has many more attrs but the dry-run path only
        # touches .totals and passes the object to format_report.
    )
    def _analyze(_path): return fake_report
    def _format(_r): return f"[stub format_report — flush_g={flush_g}]"
    return _analyze, _format


# ----- happy path: low flush passes ------------------------------------


def test_dry_run_low_flush_returns_0(monkeypatch, capsys, tmp_path):
    """0.2 g of flush vs the default 10 g cap should pass with exit 0."""
    from beambam.cli.lan import cmd_print
    from beambam.config import Creds
    from beambam import analyze as analyze_mod

    fake_3mf = tmp_path / "tiny.gcode.3mf"
    fake_3mf.write_text("")  # doesn't matter — analyze is stubbed

    analyze, fmt = _stub_analyze(flush_g=0.2)
    monkeypatch.setattr(analyze_mod, "analyze_3mf", analyze)
    monkeypatch.setattr(analyze_mod, "format_report", fmt)

    # Sentinel: if dry-run is honored, Creds.resolve must NOT be called.
    def _boom(*_a, **_kw):
        raise AssertionError(
            "Creds.resolve called even though --dry-run was set")
    monkeypatch.setattr(Creds, "resolve", classmethod(_boom))

    rc = cmd_print(_ns(file=str(fake_3mf)))
    assert rc == 0
    out = capsys.readouterr()
    assert "stub format_report" in out.out
    assert "OK" in out.err  # stderr trailer says OK
    assert "0.2 g" in out.err


def test_dry_run_refuses_when_flush_exceeds_threshold(monkeypatch, capsys,
                                                       tmp_path):
    """15 g of flush vs default 10 g cap → refuse + exit 2."""
    from beambam.cli.lan import cmd_print
    from beambam.config import Creds
    from beambam import analyze as analyze_mod

    fake_3mf = tmp_path / "huge.gcode.3mf"
    fake_3mf.write_text("")

    analyze, fmt = _stub_analyze(flush_g=15.0)
    monkeypatch.setattr(analyze_mod, "analyze_3mf", analyze)
    monkeypatch.setattr(analyze_mod, "format_report", fmt)

    rc = cmd_print(_ns(file=str(fake_3mf)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "15.0 g" in err
    assert "10.0 g" in err  # threshold echoed


def test_dry_run_custom_threshold(monkeypatch, capsys, tmp_path):
    """`--max-flush-g 5` lowers the cap: 7g now refuses; 3g still passes."""
    from beambam.cli.lan import cmd_print
    from beambam.config import Creds
    from beambam import analyze as analyze_mod

    fake_3mf = tmp_path / "f.gcode.3mf"
    fake_3mf.write_text("")

    # Case 1: 7g vs threshold 5g → refuse
    analyze, fmt = _stub_analyze(flush_g=7.0)
    monkeypatch.setattr(analyze_mod, "analyze_3mf", analyze)
    monkeypatch.setattr(analyze_mod, "format_report", fmt)
    rc = cmd_print(_ns(file=str(fake_3mf), max_flush_g=5.0))
    assert rc == 2

    # Case 2: 3g vs threshold 5g → pass (re-stub for clean capsys)
    analyze, fmt = _stub_analyze(flush_g=3.0)
    monkeypatch.setattr(analyze_mod, "analyze_3mf", analyze)
    monkeypatch.setattr(analyze_mod, "format_report", fmt)
    rc = cmd_print(_ns(file=str(fake_3mf), max_flush_g=5.0))
    assert rc == 0


def test_dry_run_does_not_resolve_creds(monkeypatch, tmp_path):
    """Critical guarantee: dry-run works on a workstation with NO
    credentials file. Creds.resolve must never be reached."""
    from beambam.cli.lan import cmd_print
    from beambam.config import Creds
    from beambam import analyze as analyze_mod

    fake_3mf = tmp_path / "x.gcode.3mf"
    fake_3mf.write_text("")
    analyze, fmt = _stub_analyze(flush_g=0.0)
    monkeypatch.setattr(analyze_mod, "analyze_3mf", analyze)
    monkeypatch.setattr(analyze_mod, "format_report", fmt)

    # Make Creds.resolve raise if anyone calls it.
    called = []
    monkeypatch.setattr(
        Creds, "resolve",
        classmethod(lambda cls, _a: called.append(True) or "boom"),
    )

    rc = cmd_print(_ns(file=str(fake_3mf)))
    assert rc == 0
    assert called == [], "Creds.resolve was called during --dry-run"


# ----- end-to-end smoke against a real bundled fixture ------------------


@pytest.mark.skipif(
    not Path(__file__).resolve().parent.parent.joinpath(
        "rumi_frame.gcode.3mf").exists(),
    reason="rumi_frame.gcode.3mf fixture not present in repo root",
)
def test_dry_run_e2e_against_real_3mf_fixture(capsys):
    """End-to-end: run cmd_print --dry-run on the real rumi_frame fixture
    that ships in the repo. Touches the actual analyze code path (not
    the stub) — catches integration breakage between cmd_print and the
    Report → totals shape that monkeypatched unit tests would miss."""
    from beambam.cli.lan import cmd_print
    from beambam.config import Creds

    repo_root = Path(__file__).resolve().parent.parent
    rumi = repo_root / "rumi_frame.gcode.3mf"

    args = _ns(file=str(rumi), max_flush_g=100.0)  # generous cap
    rc = cmd_print(args)
    assert rc == 0
    out = capsys.readouterr()
    # The real format_report output prints the file path + size + filaments.
    assert "rumi_frame.gcode.3mf" in out.out
    assert "Filaments" in out.out
    assert "OK" in out.err
