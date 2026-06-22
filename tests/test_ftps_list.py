"""tests/test_ftps_list.py — list_files transport (retry + parse).

list_files now routes through the resilient `_manual_ftp_tls` path (the one
download_file uses, which survives mid-print) instead of a bare-TLS subclass
that tripped `[SSL: INVALID_ALERT]` on subdir listings during an active print.
These tests mock the transport so the retry + line-parse contract is locked in
without a printer.
"""
from __future__ import annotations

import ssl
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam import ftps
from beambam.config import Creds


_CREDS = Creds(ip="1.2.3.4", code="ABCD1234", serial="FAKE")
_LIST = (b"drwxr-xr-x 2 0 0 0 Jan 1 00:00 cache\r\n"
         b"-rw-r--r-- 1 0 0 441800 Jan 1 00:00 zoey_frame.gcode.3mf\r\n")


class _FakeConn:
    """A data socket that yields the LIST bytes then EOF. NOT an ssl.SSLSocket,
    so list_files' unwrap branch is correctly skipped."""
    def __init__(self, payload: bytes):
        self._chunks = [payload, b""]
    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""
    def close(self):
        pass


class _FakeFTP:
    def __init__(self, payload=b"", raise_on_transfer=None):
        self._payload = payload
        self._raise = raise_on_transfer
        self.quit_called = 0
    def voidcmd(self, _cmd):
        return "200 OK"
    def transfercmd(self, _cmd):
        if self._raise is not None:
            raise self._raise
        return _FakeConn(self._payload)
    def voidresp(self):
        return "226 OK"
    def quit(self):
        self.quit_called += 1


def test_list_files_parses_lines():
    ftp = _FakeFTP(payload=_LIST)
    with patch("beambam.ftps._manual_ftp_tls", return_value=ftp):
        out = ftps.list_files(_CREDS, "cache")
    assert out == [
        "drwxr-xr-x 2 0 0 0 Jan 1 00:00 cache",
        "-rw-r--r-- 1 0 0 441800 Jan 1 00:00 zoey_frame.gcode.3mf",
    ]
    assert ftp.quit_called == 1                       # cleaned up


def test_list_files_retries_then_succeeds(monkeypatch):
    """SSLError on the first two attempts (the INVALID_ALERT failure mode),
    success on the third — list_files should retry and return."""
    monkeypatch.setattr(ftps.time, "sleep", lambda _s: None)   # no real delay
    attempts = [
        _FakeFTP(raise_on_transfer=ssl.SSLError("INVALID_ALERT")),
        _FakeFTP(raise_on_transfer=ssl.SSLError("INVALID_ALERT")),
        _FakeFTP(payload=_LIST),
    ]
    with patch("beambam.ftps._manual_ftp_tls", side_effect=attempts):
        out = ftps.list_files(_CREDS, "cache", retries=3)
    assert len(out) == 2
    assert all(a.quit_called == 1 for a in attempts)  # every attempt cleaned up


def test_list_files_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(ftps.time, "sleep", lambda _s: None)
    err = ssl.SSLError("INVALID_ALERT")
    with patch("beambam.ftps._manual_ftp_tls",
               side_effect=lambda *a, **k: _FakeFTP(raise_on_transfer=err)):
        with pytest.raises(ssl.SSLError):
            ftps.list_files(_CREDS, "cache", retries=2)


def test_list_files_root_path_no_arg():
    ftp = _FakeFTP(payload=b"-rw-r--r-- 1 0 0 10 Jan 1 00:00 a.txt\r\n")
    captured = {}
    orig = ftp.transfercmd
    ftp.transfercmd = lambda cmd: (captured.__setitem__("cmd", cmd), orig(cmd))[1]
    with patch("beambam.ftps._manual_ftp_tls", return_value=ftp):
        out = ftps.list_files(_CREDS)                 # empty path → bare LIST
    assert captured["cmd"] == "LIST"                  # no trailing space
    assert out == ["-rw-r--r-- 1 0 0 10 Jan 1 00:00 a.txt"]
