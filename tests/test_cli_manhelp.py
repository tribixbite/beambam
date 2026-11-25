"""tests/test_cli_manhelp.py — man-style help, usage-error pages, and the
friendly printer-unreachable error (beambam.cli.manhelp / arghelp / main).

Pure metadata + loopback sockets: no printer, cloud session or slicer needed.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from beambam.cli import build_parser
from beambam.cli import manhelp
from beambam.cli.arghelp import iter_commands

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # Start from a neutral colour/debug state so the caller's shell can't
    # change the output, then apply this test's overrides.
    full_env = {k: v for k, v in os.environ.items()
                if k not in ("FORCE_COLOR", "CLICOLOR_FORCE", "NO_COLOR", "BEAMBAM_DEBUG")}
    # UTF-8 both ways: Windows pipes default to cp1252, which would mangle
    # the — and → in the pages before the assertions see them.
    full_env.update({"COLUMNS": "80", "PYTHONIOENCODING": "utf-8", **(env or {})})
    return subprocess.run([sys.executable, "-m", "beambam.cli", *argv],
                          capture_output=True, text=True, encoding="utf-8", timeout=60,
                          cwd=str(REPO_ROOT), env=full_env)


@pytest.fixture(scope="module")
def root() -> argparse.ArgumentParser:
    return build_parser()


# --- coverage of the help text itself --------------------------------------

def test_every_argument_has_help(root):
    """No visible argument anywhere in the tree may render with an empty
    description. A new flag without help= (and no arghelp entry) fails here."""
    missing = []
    for path, sub in iter_commands(root):
        for a in sub._actions:
            if isinstance(a, argparse._SubParsersAction) or a.help is argparse.SUPPRESS:
                continue
            if not a.help:
                missing.append(f"{' '.join(path)}: {a.option_strings or a.dest}")
    assert not missing, "arguments without help text:\n" + "\n".join(missing)


def test_every_command_has_a_summary(root):
    blank = [" ".join(p) for p, sub in iter_commands(root) if not getattr(sub, "_bb_summary", "")]
    assert not blank, f"commands without a one-line summary: {blank}"


def test_examples_parse_against_the_real_parser(root):
    """Each hand-written EXAMPLES entry must name a real command and parse
    without an argparse error (parsing never runs the command)."""
    for key, rows in manhelp.EXAMPLES.items():
        if key:
            assert manhelp.find_parser(root, key.split()) is not None, f"no command {key!r}"
        for cmd, _ in rows:
            argv = shlex.split(cmd)
            assert argv[0] == "beambam", cmd
            if argv[1:2] == ["help"]:
                continue
            try:
                root.parse_args(argv[1:])
            except SystemExit as e:  # ManParser.error exits 2
                pytest.fail(f"example does not parse ({e.code}): {cmd}")


def test_every_command_renders_at_narrow_and_wide_widths(root):
    for parser in [root, *manhelp.iter_parsers(root)]:
        page = manhelp.build_page(parser)
        for width in (40, 100):
            text = manhelp.render_terminal(page, manhelp.Style(False), width)
            assert "NAME" in text and "SYNOPSIS" in text
            # Wrapping keeps prose inside the width; only unbreakable tokens
            # (URLs, long flags) may overflow, so allow a small share.
            lines = text.splitlines()
            long = [ln for ln in lines if len(ln) > width]
            assert len(long) <= max(3, len(lines) // 20), (parser.prog, width, long[:3])


# --- CLI behaviour ---------------------------------------------------------

def test_command_page_has_man_sections():
    r = _run("led", "--help")
    assert r.returncode == 0, r.stderr
    for section in ("NAME", "SYNOPSIS", "ALIASES", "ARGUMENTS", "OPTIONS",
                    "EXAMPLES", "GLOBAL OPTIONS", "SEE ALSO"):
        assert f"\n{section}\n" in r.stdout, f"{section} missing"
    assert "Flashing mode: milliseconds the LED stays on per blink." in r.stdout
    assert "Default: 0." in r.stdout  # --loops 0 must not be hidden as falsy


def test_missing_argument_prints_full_page_then_error():
    r = _run("print")
    assert r.returncode == 2
    assert r.stdout == ""
    assert "\nARGUMENTS\n" in r.stderr and "\nOPTIONS\n" in r.stderr
    assert r.stderr.rstrip().endswith(
        "beambam print: error: the following arguments are required: file")


def test_missing_subcommand_names_it_plainly():
    r = _run("config")
    assert r.returncode == 2
    assert "\nCOMMANDS\n" in r.stderr
    assert "no command given — choose one from COMMANDS above" in r.stderr
    assert "config_cmd" not in r.stderr  # argparse's internal dest name


def test_unknown_command_suggests_close_matches():
    r = _run("statsu")
    assert r.returncode == 2
    assert "unknown command 'statsu'" in r.stderr
    assert "did you mean: status" in r.stderr
    assert "SYNOPSIS" not in r.stderr  # a short message, not the manual


def test_nested_unknown_command():
    r = _run("cam", "strat")
    assert r.returncode == 2
    assert "beambam cam: unknown command 'strat'" in r.stderr
    assert "did you mean: start?" in r.stderr


def test_help_nested_topic_equals_direct_help():
    assert _run("help", "cam", "start").stdout == _run("cam", "start", "--help").stdout


def test_color_only_when_forced_or_tty():
    assert "\x1b[" not in _run("status", "--help").stdout
    forced = _run("status", "--help", env={"FORCE_COLOR": "1"})
    assert "\x1b[1;36mNAME" in forced.stdout
    off = _run("status", "--help", env={"FORCE_COLOR": "1", "NO_COLOR": "1"})
    assert "\x1b[" not in off.stdout


def test_help_all_contains_every_page(root):
    r = _run("help", "--all")
    assert r.returncode == 0
    for parser in manhelp.iter_parsers(root):
        assert manhelp.build_page(parser).name.upper().replace(" ", "-") + "(1)" in r.stdout


def test_roff_manual_has_every_command(root):
    r = _run("help", "--man")
    assert r.returncode == 0
    assert r.stdout.startswith(".TH BEAMBAM 1 ")
    for parser in manhelp.iter_parsers(root):
        name = manhelp.build_page(parser).name
        assert f".SS {name.replace('-', chr(92) + '-')}\n" in r.stdout, name


def test_install_man_writes_page(tmp_path):
    r = _run("help", "--install-man", str(tmp_path / "man1"))
    assert r.returncode == 0, r.stderr
    page = (tmp_path / "man1" / "beambam.1").read_text(encoding="utf-8")
    assert page.startswith(".TH BEAMBAM 1 ")


# --- printer unreachable ---------------------------------------------------

def _closed_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # closed again once the socket is released


def test_x2dclient_connect_raises_printer_unreachable(monkeypatch):
    from beambam.config import Creds
    from beambam.mqtt import PrinterUnreachableError, X2DClient
    monkeypatch.setattr(X2DClient, "PORT", _closed_port())
    cli = X2DClient(Creds(ip="127.0.0.1", code="00000000", serial="TEST"))
    with pytest.raises(PrinterUnreachableError) as ei:
        cli.connect(timeout=2)
    assert isinstance(ei.value.cause, ConnectionRefusedError)
    assert any("refused port" in h for h in ei.value.hints())


def test_unreachable_hint_flags_other_subnet():
    from beambam.mqtt import PrinterUnreachableError
    err = PrinterUnreachableError("192.168.0.138", 8883, TimeoutError("timed out"))
    err.local_ip = "192.168.39.8"  # e.g. joined a car / hotspot network
    assert "different network" in err.hints()[0]
    err.local_ip = "192.168.0.81"
    assert not any("different network" in h for h in err.hints())


def test_status_unreachable_is_a_message_not_a_traceback():
    # 127.0.0.1:8883 has no listener in CI or on a dev box without a broker.
    r = _run("--ip", "127.0.0.1", "--code", "00000000", "--serial", "TEST", "status")
    assert r.returncode == 3, r.stderr
    assert "Traceback" not in r.stderr
    assert "beambam status: cannot reach printer at 127.0.0.1:8883" in r.stderr
    assert "beambam find" in r.stderr


def test_unrecognized_flag_shows_the_subcommands_usage_and_a_suggestion():
    """argparse reports leftover args from the root parser; the usage shown
    must still be the typed command's, short enough to keep the mistake in
    view, with the closest real option and a pointer to the manual."""
    r = _run("install-completion", "--print", "bash")
    assert r.returncode == 2
    assert r.stderr.startswith("usage: beambam install-completion ")
    assert "beambam install-completion: error: unrecognized arguments: --print" in r.stderr
    assert "--print: did you mean --install?" in r.stderr
    assert r.stderr.rstrip().endswith("run `beambam install-completion --help` for the full manual")
    assert "OPTIONS" not in r.stderr                       # not the whole page


def test_bad_value_is_short_too():
    r = _run("print", "x.gcode.3mf", "--slot", "99")
    assert r.returncode == 2
    assert r.stderr.startswith("usage: beambam print ")
    assert "AMS slots are 0..15" in r.stderr and "OPTIONS" not in r.stderr


def test_trailing_help_word_opens_the_page():
    for argv in (("ams", "map", "help"), ("print", "help"), ("--printer", "x", "ams", "help")):
        r = _run(*argv)
        assert r.returncode == 0, (argv, r.stderr)
        name = "-".join(a for a in argv if a not in ("help", "--printer", "x")).upper()
        assert r.stdout.startswith(f"BEAMBAM-{name}(1)"), argv


def test_help_unknown_topic_suggests_and_searches():
    r = _run("help", "ams", "mpa")
    assert r.returncode == 1 and "did you mean: beambam ams map?" in r.stderr
    r = _run("help", "filament")
    assert "commands mentioning 'filament':" in r.stderr and "beambam ams map" in r.stderr


def test_missing_creds_points_at_init(tmp_path):
    # BEAMBAM_HOME isolates the config dir on every platform; HOME alone does
    # not on Windows (~/.x2d resolves via USERPROFILE), so a real saved
    # printer would leak in.
    r = _run("status", env={"HOME": str(tmp_path), "BEAMBAM_HOME": str(tmp_path),
                            "X2D_IP": "", "X2D_CODE": "",
                            "X2D_SERIAL": "", "X2D_PRINTER": ""})
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "no printer configured (missing: ip, code, serial)" in r.stderr
    assert "beambam init" in r.stderr


def test_doctor_runs_without_a_configured_printer(tmp_path):
    r = _run("doctor", "--no-color", env={"HOME": str(tmp_path), "BEAMBAM_HOME": str(tmp_path),
                                          "X2D_IP": "", "X2D_CODE": "",
                                          "X2D_SERIAL": "", "X2D_PRINTER": ""})
    assert r.returncode == 2 and "Traceback" not in r.stderr
    assert "Printer configured" in r.stdout and "beambam init" in r.stdout
    assert "\033[" not in r.stdout


def test_narrow_console_encoding_does_not_crash():
    """A redirected Windows console is cp1252: pages with → / — must still print."""
    for argv in (("ams", "map", "--help"), ("help", "filament"), ("print", "x", "--slto", "1")):
        r = subprocess.run([sys.executable, "-m", "beambam.cli", *argv], capture_output=True,
                           cwd=str(REPO_ROOT), timeout=60,
                           env={**os.environ, "PYTHONIOENCODING": "cp1252", "COLUMNS": "80"})
        assert b"UnicodeEncodeError" not in r.stderr, argv
        assert r.returncode in (0, 1, 2), (argv, r.stderr[-300:])
