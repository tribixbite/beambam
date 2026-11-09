"""Regression tests for three latent core bugs."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


def test_filter_3mf_refuses_same_src_and_dst(tmp_path: Path) -> None:
    from beambam.plate import filter_3mf
    f = tmp_path / "x.gcode.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28\n")
    with pytest.raises(ValueError, match="same file"):
        filter_3mf(f, f, keep={1})


def test_cloud_refresh_rejects_a_response_without_a_token(monkeypatch) -> None:
    import cloud_client as cc
    sess = cc.Session(access_token="old", refresh_token="r", expires_at=0.0,
                      user_id="u", region="us")
    client = cc.CloudClient(session=sess)
    monkeypatch.setattr(cc, "_request", lambda *a, **k: {"expiresIn": 3600})
    monkeypatch.setattr(client, "save", lambda: None)
    with pytest.raises(cc.CloudError, match="re-login"):
        client.refresh()
    assert sess.access_token == "old" and sess.expires_at == 0.0   # not advanced


def test_download_target_trailing_slash_is_a_directory(monkeypatch, tmp_path: Path) -> None:
    import argparse
    import beambam.download as dl
    captured = {}
    import beambam.ftps as ftps
    monkeypatch.setattr(ftps, "download_file", lambda creds, remote, local: captured.setdefault("local", local) or 3)
    from beambam.config import Creds
    monkeypatch.setattr(Creds, "resolve", classmethod(lambda cls, a: Creds(ip="i", code="c", serial="s")))
    d = tmp_path / "into"
    args = argparse.Namespace(remote="cache/model.gcode.3mf", local=str(d) + "/",
                              ip=None, code=None, serial=None, printer=None, quiet=True)
    dl.cmd_download(args)
    assert captured["local"] == d / "model.gcode.3mf"       # treated as a dir, not a file named "into"
