"""Tests for beambam.find — LAN SSDP discovery."""
from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.find import (
    BAMBU_ST,
    FoundPrinter,
    _parse_response,
    add_subparser,
    cmd_find,
    discover,
    format_table,
    write_credentials_section,
)


# ----- _parse_response -----------------------------------------------------


def _bambu_response(serial: str = "20P9ABCDEF12345",
                    model: str = "N6", name: str = "x2d") -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        "Cache-Control: max-age=1800\r\n"
        "Location: http://192.168.1.42:80/Bambu.xml\r\n"
        f"Server: Bambu Lab X2D 01.01.50.00\r\n"
        f"USN: uuid:{serial}::urn:bambulab-com:device:3dprinter:1\r\n"
        f"ST: urn:bambulab-com:device:3dprinter:1\r\n"
        "EXT:\r\n"
        "NT: urn:bambulab-com:device:3dprinter:1\r\n"
        f"Devmodel.bambu.com: {model}\r\n"
        f"Devname.bambu.com: {name}\r\n"
        "Devsignal.bambu.com: -54\r\n"
        "DevConnect.bambu.com: lan\r\n"
        "DevBind.bambu.com: bound\r\n"
        "\r\n"
    ).encode()


def test_parse_response_bambu_printer():
    p = _parse_response(_bambu_response(), "192.168.1.42")
    assert p is not None
    assert p.ip == "192.168.1.42"
    assert p.serial == "20P9ABCDEF12345"
    assert p.name == "x2d"
    assert p.model == "N6"
    assert p.connection == "lan"
    assert p.bind == "bound"
    assert p.signal == "-54"
    assert "Bambu" in p.version


def test_parse_response_non_bambu_returns_none():
    """Generic UPnP devices on the LAN must be filtered out."""
    raw = (
        "HTTP/1.1 200 OK\r\n"
        "Cache-Control: max-age=1800\r\n"
        "Location: http://192.168.0.50:80/desc.xml\r\n"
        "Server: Linux/3.10 UPnP/1.1\r\n"
        "USN: uuid:abc::urn:schemas-upnp-org:device:MediaServer:1\r\n"
        "ST: urn:schemas-upnp-org:device:MediaServer:1\r\n"
        "\r\n"
    ).encode()
    assert _parse_response(raw, "192.168.0.50") is None


def test_parse_response_malformed_returns_none():
    """Bogus byte strings shouldn't crash."""
    assert _parse_response(b"not http\r\n", "192.168.0.50") is None
    assert _parse_response(b"", "192.168.0.50") is None


def test_parse_response_missing_optional_fields():
    raw = (
        "HTTP/1.1 200 OK\r\n"
        "Server: Bambu Lab P1S\r\n"
        "USN: uuid:01P1ABC::urn:bambulab-com:device:3dprinter:1\r\n"
        "ST: urn:bambulab-com:device:3dprinter:1\r\n"
        "\r\n"
    ).encode()
    p = _parse_response(raw, "192.168.0.42")
    assert p is not None
    assert p.serial == "01P1ABC"
    assert p.model == ""               # missing → empty string
    assert p.name == ""


# ----- discover (mocked socket) -------------------------------------------


def test_discover_collects_responses():
    """Two distinct printers respond; both should show up."""
    sock = MagicMock()
    r1 = _bambu_response(serial="20P9AAA")
    r2 = _bambu_response(serial="01P1BBB", model="C12", name="p1s")
    # First recvfrom returns the X2D, second returns the P1S, third times out.
    sock.recvfrom.side_effect = [
        (r1, ("192.168.1.42", 1900)),
        (r2, ("192.168.0.42", 1900)),
        TimeoutError(),
        TimeoutError(),
    ]
    with patch("socket.socket", return_value=sock):
        with patch("time.monotonic", side_effect=[0, 0, 0, 0, 0, 100]):
            found = discover(timeout=0.5)
    serials = {f.serial for f in found}
    assert "20P9AAA" in serials
    assert "01P1BBB" in serials


def test_discover_deduplicates_by_ip_serial():
    """If a printer responds twice in the window, only one entry."""
    sock = MagicMock()
    r = _bambu_response(serial="20P9DUP")
    sock.recvfrom.side_effect = [
        (r, ("192.168.1.42", 1900)),
        (r, ("192.168.1.42", 1900)),   # dupe
        TimeoutError(),
    ]
    with patch("socket.socket", return_value=sock):
        with patch("time.monotonic", side_effect=[0, 0, 0, 0, 100]):
            found = discover(timeout=0.5)
    assert len(found) == 1


def test_discover_filters_non_bambu_responses():
    """A generic UPnP MediaServer reply should NOT appear."""
    sock = MagicMock()
    bambu = _bambu_response()
    other = (
        b"HTTP/1.1 200 OK\r\nServer: Linux UPnP\r\n"
        b"USN: uuid:x::urn:schemas-upnp-org:device:MediaServer:1\r\nST: a\r\n\r\n"
    )
    sock.recvfrom.side_effect = [
        (bambu, ("192.168.1.42", 1900)),
        (other, ("192.168.0.50", 1900)),
        TimeoutError(),
    ]
    with patch("socket.socket", return_value=sock):
        with patch("time.monotonic", side_effect=[0, 0, 0, 0, 100]):
            found = discover(timeout=0.5)
    assert len(found) == 1
    assert found[0].ip == "192.168.1.42"


def test_discover_handles_sendto_error():
    """OSError on M-SEARCH send must return [] rather than raise."""
    sock = MagicMock()
    sock.sendto.side_effect = OSError("ENETUNREACH")
    with patch("socket.socket", return_value=sock):
        found = discover(timeout=0.5)
    assert found == []


# ----- format_table -------------------------------------------------------


def test_format_table_no_printers_explains_why():
    text = format_table([])
    assert "no Bambu printers found" in text
    assert "LAN-Only" in text
    assert "udp/1900" in text


def test_format_table_one_printer():
    p = FoundPrinter(ip="192.168.1.42", serial="00M09A000000000",
                     name="x2d", model="N6", connection="lan",
                     bind="bound", signal="-54", version="Bambu Lab X2D")
    text = format_table([p])
    assert "192.168.1.42" in text
    assert "00M09A000000000" in text
    assert "N6" in text
    assert "x2d" in text


# ----- write_credentials_section ------------------------------------------


def test_write_credentials_section_creates_file(tmp_path):
    creds = tmp_path / "credentials"
    p = FoundPrinter(ip="1.2.3.4", serial="FAKE", name="t")
    ok = write_credentials_section("test", p, access_code="12345678",
                                    path=creds)
    assert ok is True
    cp = configparser.ConfigParser()
    cp.read(creds)
    assert cp.has_section("printer:test")
    assert cp.get("printer:test", "ip") == "1.2.3.4"
    assert cp.get("printer:test", "code") == "12345678"
    assert cp.get("printer:test", "serial") == "FAKE"


def test_write_credentials_section_refuses_overwrite_with_different_ip(tmp_path):
    """If [printer:NAME] already exists with a different ip, refuse."""
    creds = tmp_path / "credentials"
    creds.write_text("[printer:t]\nip = 9.9.9.9\ncode = 00000000\nserial = OTHER\n")
    p = FoundPrinter(ip="1.2.3.4", serial="FAKE")
    ok = write_credentials_section("t", p, access_code="12345678", path=creds)
    assert ok is False
    # Original untouched
    text = creds.read_text()
    assert "9.9.9.9" in text


def test_write_credentials_section_chmod_600(tmp_path):
    """Access code is sensitive — file must be 0600."""
    creds = tmp_path / "credentials"
    p = FoundPrinter(ip="1.2.3.4", serial="FAKE")
    write_credentials_section("t", p, access_code="12345678", path=creds)
    mode = creds.stat().st_mode & 0o777
    assert mode == 0o600


# ----- cli ---------------------------------------------------------------


def test_subparser_defaults():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["find"])
    assert args.timeout == 3.0
    assert args.json_out is False
    assert args.include_other is False
    assert args.add is None


def test_cmd_find_no_printers_table_output(capsys):
    args = argparse.Namespace(timeout=0.1, include_other=False,
                               json_out=False, add=None)
    with patch("beambam.find.discover", return_value=[]):
        rc = cmd_find(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no Bambu printers" in out


def test_cmd_find_json_output(capsys):
    p = FoundPrinter(ip="1.2.3.4", serial="ABC", name="t", model="N6")
    args = argparse.Namespace(timeout=0.1, include_other=False,
                               json_out=True, add=None)
    with patch("beambam.find.discover", return_value=[p]):
        cmd_find(args)
    import json as _json
    parsed = _json.loads(capsys.readouterr().out)
    assert parsed[0]["ip"] == "1.2.3.4"
    assert parsed[0]["serial"] == "ABC"


def test_cmd_find_add_fails_when_no_printers(capsys):
    args = argparse.Namespace(timeout=0.1, include_other=False,
                               json_out=False, add="test")
    with patch("beambam.find.discover", return_value=[]):
        rc = cmd_find(args)
    assert rc == 1
    assert "Nothing to add" in capsys.readouterr().err
