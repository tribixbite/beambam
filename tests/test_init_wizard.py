"""Tests for beambam.init_wizard — first-run setup."""
from __future__ import annotations

import argparse
import configparser
import socket
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.init_wizard import (
    _check_tcp,
    _pick_printer,
    _resolve_target,
    add_subparser,
    cmd_init,
)


@pytest.fixture
def creds_file(tmp_path, monkeypatch):
    path = tmp_path / "credentials"
    monkeypatch.setattr("beambam.configcli.CREDS_PATH", path)
    return path


# ----- _check_tcp ---------------------------------------------------------


def test_check_tcp_open(monkeypatch):
    with patch("socket.create_connection") as conn:
        conn.return_value.__enter__.return_value = object()
        ok, err = _check_tcp("1.2.3.4", 8883)
    assert ok is True and err == ""


def test_check_tcp_refused():
    with patch("socket.create_connection",
                side_effect=OSError("connection refused")):
        ok, err = _check_tcp("1.2.3.4", 8883)
    assert ok is False
    assert "refused" in err


# ----- _pick_printer ------------------------------------------------------


def test_pick_printer_returns_zero_based_index(monkeypatch, capsys):
    from beambam.find import FoundPrinter
    printers = [FoundPrinter(ip="1.1.1.1", serial="A"),
                FoundPrinter(ip="2.2.2.2", serial="B")]
    with patch("beambam.init_wizard._prompt", return_value="2"):
        idx = _pick_printer(printers)
    assert idx == 1


def test_pick_printer_retries_on_invalid(monkeypatch):
    from beambam.find import FoundPrinter
    printers = [FoundPrinter(ip="1.1.1.1", serial="A")]
    inputs = iter(["x", "0", "5", "1"])
    with patch("beambam.init_wizard._prompt",
                side_effect=lambda *a, **kw: next(inputs)):
        idx = _pick_printer(printers)
    assert idx == 0


# ----- _resolve_target ---------------------------------------------------


def test_resolve_target_with_explicit_ip_skips_discovery():
    args = argparse.Namespace(ip="1.2.3.4", serial="ABC",
                               timeout=3.0)
    with patch("beambam.find.discover") as disc:
        ip, serial, name, model = _resolve_target(args)
    assert ip == "1.2.3.4" and serial == "ABC"
    disc.assert_not_called()


def test_resolve_target_no_printers_exits():
    args = argparse.Namespace(ip=None, serial=None, timeout=0.1)
    with patch("beambam.find.discover", return_value=[]), \
         pytest.raises(SystemExit):
        _resolve_target(args)


def test_resolve_target_single_printer_returns_it(capsys):
    from beambam.find import FoundPrinter
    args = argparse.Namespace(ip=None, serial=None, timeout=0.1)
    with patch("beambam.find.discover",
                return_value=[FoundPrinter(ip="1.2.3.4", serial="X",
                                            name="x2d", model="N6")]):
        ip, serial, name, model = _resolve_target(args)
    assert ip == "1.2.3.4" and serial == "X" and name == "x2d" and model == "N6"


def test_resolve_target_multi_printer_prompts(monkeypatch):
    from beambam.find import FoundPrinter
    args = argparse.Namespace(ip=None, serial=None, timeout=0.1)
    found = [FoundPrinter(ip="1.1.1.1", serial="A"),
             FoundPrinter(ip="2.2.2.2", serial="B")]
    with patch("beambam.find.discover", return_value=found), \
         patch("beambam.init_wizard._pick_printer", return_value=1):
        ip, serial, _, _ = _resolve_target(args)
    assert ip == "2.2.2.2" and serial == "B"


# ----- cmd_init dispatch -------------------------------------------------


def _full_args(**overrides):
    """Build the full init Namespace with sensible defaults."""
    base = dict(name=None, ip=None, serial=None, code=None,
                timeout=0.1, force=False, non_interactive=True,
                # --cloud-only branch fields (default off).
                cloud_only=False, email=None, email_code=None, region=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_init_full_flow_writes_credentials(creds_file, capsys):
    """Non-interactive end-to-end with mocked discovery + connectivity."""
    args = _full_args(ip="1.2.3.4", serial="FAKE", code="12345678",
                       name="studio", force=True)
    with patch("beambam.init_wizard._check_tcp", return_value=(True, "")):
        rc = cmd_init(args)
    assert rc == 0
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert cp.has_section("printer:studio")
    assert cp.get("printer:studio", "ip") == "1.2.3.4"
    assert cp.get("printer:studio", "code") == "12345678"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX chmod 0o600 semantics — Windows uses ACLs")
def test_cmd_init_chmod_600(creds_file):
    args = _full_args(ip="1.2.3.4", serial="FAKE", code="12345678",
                       name="t", force=True)
    with patch("beambam.init_wizard._check_tcp", return_value=(True, "")):
        cmd_init(args)
    assert creds_file.stat().st_mode & 0o777 == 0o600


def test_cmd_init_rejects_invalid_code(creds_file, capsys):
    args = _full_args(ip="1.2.3.4", serial="FAKE", code="abcd")
    with patch("beambam.init_wizard._check_tcp", return_value=(True, "")):
        rc = cmd_init(args)
    assert rc == 2
    assert "8 digits" in capsys.readouterr().err


def test_cmd_init_no_name_writes_default_section(creds_file):
    """No --name and no model name → [printer]."""
    args = _full_args(ip="1.2.3.4", serial="FAKE", code="12345678",
                       force=True)
    with patch("beambam.init_wizard._check_tcp", return_value=(True, "")):
        cmd_init(args)
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert cp.has_section("printer")


def test_cmd_init_uses_model_name_when_no_explicit_name(creds_file):
    """Discovery returns name='x2d' → [printer:x2d] when --name unset."""
    from beambam.find import FoundPrinter
    args = _full_args(code="12345678", force=True)
    with patch("beambam.find.discover",
                return_value=[FoundPrinter(ip="1.1.1.1", serial="A",
                                            name="x2d", model="N6")]), \
         patch("beambam.init_wizard._check_tcp", return_value=(True, "")):
        cmd_init(args)
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert cp.has_section("printer:x2d")


def test_cmd_init_force_overwrites_existing(creds_file):
    """--force should silently overwrite an existing section."""
    creds_file.write_text(
        "[printer:t]\nip = 9.9.9.9\ncode = 11111111\nserial = OLD\n")
    args = _full_args(ip="1.2.3.4", serial="NEW", code="12345678",
                       name="t", force=True)
    with patch("beambam.init_wizard._check_tcp", return_value=(True, "")):
        cmd_init(args)
    cp = configparser.ConfigParser()
    cp.read(creds_file)
    assert cp.get("printer:t", "ip") == "1.2.3.4"
    assert cp.get("printer:t", "serial") == "NEW"


# ----- subparser ---------------------------------------------------------


def test_subparser_defaults():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["init"])
    assert args.timeout == 3.0
    assert args.non_interactive is False
    assert args.force is False
    assert args.name is None
    assert args.ip is None


def test_subparser_with_all_flags():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args([
        "init", "--name", "studio", "--ip", "1.2.3.4",
        "--serial", "ABC", "--code", "12345678",
        "--timeout", "10", "--force", "--non-interactive",
    ])
    assert args.name == "studio"
    assert args.ip == "1.2.3.4"
    assert args.timeout == 10.0
    assert args.force is True
    assert args.non_interactive is True


# ----- --cloud-only branch --------------------------------------------------


def test_subparser_cloud_only_flag():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    ns = p.parse_args([
        "init", "--cloud-only", "--email", "u@example.com",
        "--email-code", "123456", "--region", "us",
    ])
    assert ns.cloud_only is True
    assert ns.email == "u@example.com"
    assert ns.email_code == "123456"
    assert ns.region == "us"


def test_cloud_only_skips_lan_path_entirely(monkeypatch, capsys):
    """The --cloud-only path must NOT call _resolve_target or _check_tcp.
    If either is invoked, the LAN flow leaked into the cloud path."""
    import beambam.init_wizard as iw
    import cloud_client

    def _boom_lan(*_a, **_kw):
        raise AssertionError(
            "LAN code reached under --cloud-only — flag isn't gating")
    monkeypatch.setattr(iw, "_resolve_target", _boom_lan)
    monkeypatch.setattr(iw, "_check_tcp", _boom_lan)

    # Replace login_code_only with a stub that records the call and
    # populates the session so the success path can be exercised.
    calls = []

    def _stub_login(self, email, region=None, code_resolver=None):
        calls.append({"email": email, "region": region,
                       "resolver": code_resolver})
        self.session = cloud_client.Session(
            access_token="AT", refresh_token="RT",
            expires_at=9999999999, user_id="42", region=region or "us")

    monkeypatch.setattr(cloud_client.CloudClient, "login_code_only",
                         _stub_login)

    args = _full_args(cloud_only=True, email="user@example.com",
                       email_code="123456", region="us")
    rc = cmd_init(args)
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["email"] == "user@example.com"
    assert calls[0]["region"] == "us"
    out = capsys.readouterr().out
    assert "logged in" in out
    assert "user 42" in out
    assert "cloud-printers" in out  # next-steps hint


def test_cloud_only_no_email_non_interactive_fails(creds_file):
    """Non-interactive cloud-only without --email must exit 2."""
    args = _full_args(cloud_only=True, non_interactive=True, email=None)
    rc = cmd_init(args)
    assert rc == 2


def test_cloud_only_login_failure_returns_1(monkeypatch, capsys):
    """A CloudError on login_code_only must surface as exit 1 with a
    clean stderr message — NOT crash."""
    import beambam.init_wizard as iw
    import cloud_client

    monkeypatch.setattr(iw, "_resolve_target",
                         lambda *_a, **_kw: pytest.fail("LAN path reached"))

    def _raise(self, *args, **kw):
        raise cloud_client.CloudError("403 bad credentials")
    monkeypatch.setattr(cloud_client.CloudClient, "login_code_only", _raise)

    args = _full_args(cloud_only=True, email="u@example.com",
                       email_code="000000", region="us")
    rc = cmd_init(args)
    assert rc == 1
    assert "cloud-login failed" in capsys.readouterr().err


def test_cloud_only_uses_env_BAMBU_EMAIL_when_no_flag(monkeypatch):
    """--email is optional under --non-interactive=False if $BAMBU_EMAIL
    is set (matches cmd_cloud_login's behavior)."""
    import beambam.init_wizard as iw
    import cloud_client

    monkeypatch.setenv("BAMBU_EMAIL", "fromenv@example.com")
    monkeypatch.setattr(iw, "_resolve_target",
                         lambda *_a, **_kw: pytest.fail("LAN path reached"))

    seen = {}

    def _stub_login(self, email, region=None, code_resolver=None):
        seen["email"] = email
        self.session = cloud_client.Session(
            access_token="AT", refresh_token="RT",
            expires_at=9999999999, user_id="42", region="us")

    monkeypatch.setattr(cloud_client.CloudClient, "login_code_only",
                         _stub_login)

    args = _full_args(cloud_only=True, email=None,
                       email_code="123456", region="us")
    rc = cmd_init(args)
    assert rc == 0
    assert seen["email"] == "fromenv@example.com"
