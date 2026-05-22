"""tests/test_help_alias.py — `beambam help <topic>` alias.

`beambam help X` should produce identical output to `beambam X --help`.
This is just a delegating shim so we test the contract end-to-end via
subprocess to catch any drift in the parser wiring (an internal
refactor that broke the alias would silently regress).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "beambam.cli", *argv],
        capture_output=True, text=True, timeout=20,
        cwd=str(REPO_ROOT),
    )


def test_help_subcommand_exists():
    """`beambam --help` should mention the new `help` subcommand in the
    top-level usage block."""
    r = _run("--help")
    assert r.returncode == 0, r.stderr
    # The usage line lists every subcommand between {…}; "help" must be there.
    assert ",help," in r.stdout or ",help}" in r.stdout or "{help," in r.stdout


@pytest.mark.parametrize("topic", ["print", "ams", "cloud-search", "doctor"])
def test_help_topic_matches_topic_dash_dash_help(topic):
    """`beambam help <topic>` output must equal `beambam <topic> --help`.
    Exact-match catches future drift (someone customizes one path but
    forgets the other)."""
    alias = _run("help", topic)
    direct = _run(topic, "--help")
    assert alias.returncode == 0, alias.stderr
    assert direct.returncode == 0, direct.stderr
    assert alias.stdout == direct.stdout, (
        f"`help {topic}` diverged from `{topic} --help`\n"
        f"--- alias ---\n{alias.stdout}\n--- direct ---\n{direct.stdout}")


def test_help_unknown_topic_exits_1():
    """Unknown topic → exit 1 + stderr lists the available subcommands."""
    r = _run("help", "nonesuch-zzz")
    assert r.returncode == 1
    assert "unknown topic" in r.stderr
    assert "available topics" in r.stderr
    # The list should include real topics we know exist.
    assert "print" in r.stderr
    assert "ams" in r.stderr


def test_help_help_works():
    """`beambam help --help` should describe the alias itself."""
    r = _run("help", "--help")
    assert r.returncode == 0
    assert "topic" in r.stdout
    assert "alias" in r.stdout.lower() or "--help" in r.stdout
