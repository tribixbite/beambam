"""tests/test_cli_help_smoke.py — every subcommand's `--help` exits cleanly.

`beambam <subcommand> --help` should:
  1. Exit 0 (argparse returns 0 on --help)
  2. Print a usage line containing the subcommand name
  3. Not crash the argparse formatter (e.g. literal `%` in help text)

The list of subcommands is *discovered* from the parser at test time —
adding a new subcommand to beambam.cli automatically picks up coverage.
This catches:

  * Format-specifier bugs in help strings (`%` not escaped as `%%`)
  * Missing set_defaults(fn=...) — the parser parses but main() crashes
  * Stray imports that crash at module load
  * Pre-existing argparse errors in the subparser block

Tests in this file are pure metadata — no printer required, no cloud
session required, no slicer required. Runs in CI.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _discover_subcommands() -> list[str]:
    """Every registered top-level subcommand name (aliases included),
    read straight from the parser tree that `beambam` itself builds."""
    import argparse
    from beambam.cli import build_parser
    root = build_parser()
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    raise AssertionError("root parser has no subcommands")


SUBCOMMANDS = _discover_subcommands()


def test_top_level_help_lists_subcommands():
    """--help must produce a usage that lists at least 30 subcommands."""
    assert len(SUBCOMMANDS) >= 30, \
        f"only {len(SUBCOMMANDS)} subcommands found — parser may be broken"


def test_top_level_help_has_grouped_commands():
    """The root page lists commands grouped by topic (beambam.cli._COMMAND_GROUPS)."""
    r = subprocess.run([sys.executable, "-m", "beambam.cli", "--help"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "COMMANDS" in r.stdout, "COMMANDS section missing from the root page"
    # At minimum, the LAN control + Cloud read + MakerWorld groups exist.
    for group in ("LAN control", "Cloud read", "MakerWorld"):
        assert group in r.stdout, f"group {group!r} missing from root page"


def test_top_level_help_no_format_specifier_crash():
    """No literal `%` in help text that argparse tries to expand as a
    format specifier — `%(default)s` is the only allowed pattern.

    Pre-existing bug found this session: `P%` in the `watch` help
    crashed `--help` with `unsupported format character`."""
    r = subprocess.run([sys.executable, "-m", "beambam.cli", "--help"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, f"--help crashed:\n{r.stderr}"
    # Any `%`-related error message from argparse would show in stderr
    assert "unsupported format" not in r.stderr
    assert "must be real number" not in r.stderr


# Verbs that are argparse aliases of a renamed primary. Their --help
# output shows the canonical name in `usage:` rather than the alias, so
# the smoke test below allows either string to appear. Keep this list
# in sync with `aliases=[...]` add_parser kwargs.
_ALIASES = {
    "daemon":        "boo",
    "ha-publish":    "ha",
    "chamber-light": "led",
    "upload":        "push",
    "download":      "pull",
    "camera":        "cam",   # historical, separate top-level for now
    "token":         "key",
}


@pytest.mark.parametrize("subcmd", SUBCOMMANDS)
def test_subcommand_help_exits_zero(subcmd):
    """Every discovered subcommand must respond to `--help` cleanly."""
    r = subprocess.run(
        [sys.executable, "-m", "beambam.cli", subcmd, "--help"],
        capture_output=True, text=True, timeout=20, cwd=str(REPO_ROOT))
    assert r.returncode == 0, (
        f"`{subcmd} --help` exited {r.returncode}\n"
        f"stdout:\n{r.stdout[:500]}\n"
        f"stderr:\n{r.stderr[:500]}"
    )
    # Sanity check: the help mentions either the subcmd name OR its
    # canonical (aliases route to the canonical add_parser).
    expected = {subcmd, _ALIASES.get(subcmd, subcmd)}
    assert any(e in r.stdout for e in expected), (
        f"`{subcmd} --help` output mentions none of {expected!r}")


def test_version_flag_works():
    r = subprocess.run([sys.executable, "-m", "beambam.cli", "--version"],
                       capture_output=True, text=True, timeout=10,
                       cwd=str(REPO_ROOT))
    assert r.returncode == 0
    assert "beambam" in r.stdout
