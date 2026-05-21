"""Tests for beambam.configcli — credentials management CLI."""
from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.configcli import (
    _mask,
    _section_name,
    _short_name,
    add_subparser,
    cmd_add,
    cmd_list,
    cmd_remove,
    cmd_rename,
    cmd_show,
    list_sections,
)


@pytest.fixture
def creds_file(tmp_path, monkeypatch):
    """Each test gets a fresh credentials file at a temp path."""
    path = tmp_path / "credentials"
    monkeypatch.setattr("beambam.configcli.CREDS_PATH", path)
    return path


# ----- helpers ------------------------------------------------------------


def test_section_name_normalizes():
    assert _section_name("studio") == "printer:studio"
    assert _section_name("printer:studio") == "printer:studio"
    assert _section_name("printer") == "printer"


def test_short_name_inverts():
    assert _short_name("printer") == "(default)"
    assert _short_name("printer:studio") == "studio"
    assert _short_name("other") == "other"        # passthrough for unknown


def test_mask_codes():
    assert _mask("") == ""
    assert _mask("ab") == "**"
    assert _mask("12345678") == "12******"


# ----- list_sections ------------------------------------------------------


def test_list_sections_empty(creds_file):
    assert list_sections(creds_file) == []


def test_list_sections_with_default_and_named(creds_file):
    creds_file.write_text(
        "[printer]\nip = 1.2.3.4\ncode = 12345678\nserial = AAA\n\n"
        "[printer:studio]\nip = 5.6.7.8\ncode = 87654321\nserial = BBB\n"
    )
    sections = list_sections(creds_file)
    names = [n for n, _ in sections]
    assert "(default)" in names
    assert "studio" in names
    # Field passthrough
    studio = next(d for n, d in sections if n == "studio")
    assert studio["ip"] == "5.6.7.8"


def test_list_sections_ignores_non_printer(creds_file):
    """Sections like [global] or [logging] shouldn't show up."""
    creds_file.write_text(
        "[printer]\nip = 1.2.3.4\ncode = 12345678\nserial = AAA\n\n"
        "[other]\nfoo = bar\n"
    )
    sections = list_sections(creds_file)
    assert len(sections) == 1


# ----- cmd_list -----------------------------------------------------------


def test_cmd_list_no_sections_returns_1(creds_file, capsys):
    args = argparse.Namespace(reveal=False)
    rc = cmd_list(args)
    assert rc == 1
    assert "no printer sections" in capsys.readouterr().err


def test_cmd_list_masks_codes_by_default(creds_file, capsys):
    creds_file.write_text("[printer]\nip=1.2.3.4\ncode=12345678\nserial=A\n")
    args = argparse.Namespace(reveal=False)
    cmd_list(args)
    out = capsys.readouterr().out
    assert "12******" in out
    assert "12345678" not in out


def test_cmd_list_reveal_shows_full_code(creds_file, capsys):
    creds_file.write_text("[printer]\nip=1.2.3.4\ncode=12345678\nserial=A\n")
    args = argparse.Namespace(reveal=True)
    cmd_list(args)
    out = capsys.readouterr().out
    assert "12345678" in out


# ----- cmd_show -----------------------------------------------------------


def test_cmd_show_missing_section(creds_file, capsys):
    args = argparse.Namespace(name="nope", reveal=False)
    rc = cmd_show(args)
    assert rc == 1
    assert "no such section" in capsys.readouterr().err


def test_cmd_show_present(creds_file, capsys):
    creds_file.write_text(
        "[printer:studio]\nip=1.2.3.4\ncode=12345678\nserial=AAA\n")
    args = argparse.Namespace(name="studio", reveal=True)
    rc = cmd_show(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1.2.3.4" in out
    assert "12345678" in out
    assert "AAA" in out


# ----- cmd_add ------------------------------------------------------------


def test_cmd_add_creates_section(creds_file, capsys):
    args = argparse.Namespace(name="studio", ip="1.2.3.4",
                               code="12345678", serial="AAA", force=False)
    rc = cmd_add(args)
    assert rc == 0
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert cp.has_section("printer:studio")
    assert cp.get("printer:studio", "ip") == "1.2.3.4"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX chmod 0o600 semantics — Windows uses ACLs")
def test_cmd_add_chmod_600(creds_file):
    args = argparse.Namespace(name="t", ip="1.2.3.4",
                               code="12345678", serial="A", force=False)
    cmd_add(args)
    assert creds_file.stat().st_mode & 0o777 == 0o600


def test_cmd_add_rejects_invalid_code(creds_file, capsys):
    args = argparse.Namespace(name="t", ip="1.2.3.4",
                               code="abcd", serial="A", force=False)
    rc = cmd_add(args)
    assert rc == 2
    assert "8 digits" in capsys.readouterr().err


def test_cmd_add_refuses_overwrite_without_force(creds_file, capsys):
    creds_file.write_text("[printer:t]\nip=1.1.1.1\ncode=11111111\nserial=A\n")
    args = argparse.Namespace(name="t", ip="9.9.9.9",
                               code="22222222", serial="B", force=False)
    rc = cmd_add(args)
    assert rc == 1
    assert "--force" in capsys.readouterr().err


def test_cmd_add_force_overwrites(creds_file):
    creds_file.write_text("[printer:t]\nip=1.1.1.1\ncode=11111111\nserial=A\n")
    args = argparse.Namespace(name="t", ip="9.9.9.9",
                               code="22222222", serial="B", force=True)
    rc = cmd_add(args)
    assert rc == 0
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert cp.get("printer:t", "ip") == "9.9.9.9"


# ----- cmd_remove ---------------------------------------------------------


def test_cmd_remove_existing(creds_file, capsys):
    creds_file.write_text("[printer:t]\nip=1.2.3.4\ncode=12345678\nserial=A\n")
    args = argparse.Namespace(name="t")
    rc = cmd_remove(args)
    assert rc == 0
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert not cp.has_section("printer:t")


def test_cmd_remove_missing(creds_file, capsys):
    args = argparse.Namespace(name="nope")
    rc = cmd_remove(args)
    assert rc == 1


# ----- cmd_rename ---------------------------------------------------------


def test_cmd_rename_moves_section(creds_file):
    creds_file.write_text(
        "[printer:old]\nip=1.2.3.4\ncode=12345678\nserial=AAA\n")
    args = argparse.Namespace(old="old", new="new")
    rc = cmd_rename(args)
    assert rc == 0
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert not cp.has_section("printer:old")
    assert cp.has_section("printer:new")
    assert cp.get("printer:new", "ip") == "1.2.3.4"


def test_cmd_rename_refuses_target_exists(creds_file, capsys):
    creds_file.write_text(
        "[printer:a]\nip=1.1.1.1\ncode=12345678\nserial=A\n"
        "[printer:b]\nip=2.2.2.2\ncode=12345678\nserial=B\n"
    )
    args = argparse.Namespace(old="a", new="b")
    rc = cmd_rename(args)
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


# ----- subparser ---------------------------------------------------------


def test_subparser_requires_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["config"])


def test_subparser_add_requires_all_three():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["config", "add", "t"])  # missing --ip --code --serial


def test_subparser_remove_aliased_as_rm():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["config", "rm", "t"])
    assert args.config_cmd == "rm"
    assert args.name == "t"
