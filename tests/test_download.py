"""Tests for `beambam download` CLI."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.download import add_subparser, cmd_download


def _parse(*argv: str) -> argparse.Namespace:
    """Helper: build an argparse.Namespace as if invoked via the bridge
    top-level parser (so the global --ip / --code / --serial / --printer
    flags exist on the namespace before the subparser runs)."""
    p = argparse.ArgumentParser()
    p.add_argument("--ip")
    p.add_argument("--code")
    p.add_argument("--serial")
    p.add_argument("--printer")
    sub = p.add_subparsers()
    add_subparser(sub)
    return p.parse_args(["download", *argv])


def test_subparser_required_args():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["download"])           # no remote


def test_parses_remote_only():
    args = _parse("/cache/foo.3mf")
    assert args.remote == "/cache/foo.3mf"
    assert args.local is None
    assert args.quiet is False


def test_parses_remote_and_local():
    args = _parse("/cache/foo.3mf", "out.3mf")
    assert args.local == "out.3mf"


def test_parses_quiet_flag():
    args = _parse("--quiet", "/cache/foo.3mf")
    assert args.quiet is True


def test_cmd_download_default_local(tmp_path, monkeypatch):
    """When local omitted, writes to ./<basename(remote)>."""
    monkeypatch.chdir(tmp_path)
    args = _parse("/cache/some-print.3mf")
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.ftps.download_file") as download:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="ABCD1234",
                                          serial="FAKE")
        download.return_value = 1234
        rc = cmd_download(args)
    assert rc == 0
    call = download.call_args
    assert call.args[1] == "/cache/some-print.3mf"
    assert call.args[2] == tmp_path / "some-print.3mf"


def test_cmd_download_explicit_local(tmp_path, capsys):
    args = _parse("/cache/x.3mf", str(tmp_path / "y.3mf"))
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.ftps.download_file") as download:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="ABCD1234",
                                          serial="FAKE")
        download.return_value = 5678
        rc = cmd_download(args)
    assert rc == 0
    call = download.call_args
    assert call.args[2] == tmp_path / "y.3mf"
    out = capsys.readouterr().out
    assert "wrote" in out


def test_cmd_download_local_directory_appends_basename(tmp_path):
    """If `local` is an existing directory, write inside it."""
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    args = _parse("/cache/eevee.3mf", str(dest_dir))
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.ftps.download_file") as download:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="ABCD1234",
                                          serial="FAKE")
        download.return_value = 1
        cmd_download(args)
    assert download.call_args.args[2] == dest_dir / "eevee.3mf"


def test_cmd_download_quiet_suppresses_output(tmp_path, capsys):
    args = _parse("--quiet", "/cache/x.3mf", str(tmp_path / "y.3mf"))
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.ftps.download_file") as download:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="ABCD1234",
                                          serial="FAKE")
        download.return_value = 1
        cmd_download(args)
    assert capsys.readouterr().out == ""


def test_cmd_download_handles_ftps_error(tmp_path, capsys):
    args = _parse("/cache/x.3mf", str(tmp_path / "y.3mf"))
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.ftps.download_file") as download:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="ABCD1234",
                                          serial="FAKE")
        download.side_effect = OSError("network unreachable")
        rc = cmd_download(args)
    assert rc == 1
    assert "download failed" in capsys.readouterr().err
