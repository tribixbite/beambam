"""tests/test_cloud_spool_crud.py — `cloud-spool {add,update,delete}` CRUD.

Covers the new CloudClient.add_spool / update_spool / delete_spool
methods + the CLI handlers gated by --allow-write.

The body shape Bambu's API expects isn't publicly documented; we pin
down URL + HTTP method + the body-builder logic. If Bambu changes the
required fields, the live call surfaces a CloudError that the CLI
handler already turns into a clean exit-1 + stderr (covered).

Mocks: cloud_client._request at the URL layer; CloudClient.load_or_anonymous
to inject a logged-in session. No live network.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cloud_client


# ----- fixtures ---------------------------------------------------------


def _logged_in_client(monkeypatch) -> cloud_client.CloudClient:
    """Build a logged-in CloudClient + neutralise _ensure_fresh so the
    write tests don't try to refresh a token."""
    s = cloud_client.Session(
        access_token="AT", refresh_token="RT",
        expires_at=time.time() + 3600,
        user_id="42", region="us")
    c = cloud_client.CloudClient(session=s)
    monkeypatch.setattr(c, "_ensure_fresh", lambda: None)
    return c


@pytest.fixture
def cli(monkeypatch):
    return _logged_in_client(monkeypatch)


class _Capturer:
    """Replacement for cloud_client._request — records the call shape."""

    def __init__(self, response=None):
        self.calls: list[dict] = []
        self.response = response or {}

    def __call__(self, method, url, *, body=None, headers=None,
                 timeout=None, return_cookies=False):
        self.calls.append({"method": method, "url": url, "body": body})
        if callable(self.response):
            return self.response(method, url, body)
        return self.response


def _patch_request(monkeypatch, response=None) -> _Capturer:
    cap = _Capturer(response=response)
    monkeypatch.setattr(cloud_client, "_request", cap)
    return cap


# ===== CloudClient.add_spool =============================================


def test_add_spool_posts_to_filament_v2(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"id": 999, "filamentId": "GFB02"})
    body = {"filamentVendor": "Bambu", "filamentType": "PLA Basic",
            "filamentName": "Galaxy Black", "filamentId": "GFB02",
            "color": "#0F0F0F", "weight": 1000, "createType": "manual"}
    out = cli.add_spool(body)
    assert out["filamentId"] == "GFB02"
    assert cap.calls[0]["method"] == "POST"
    assert cap.calls[0]["url"].endswith(
        "/v1/design-user-service/my/filament/v2")
    assert cap.calls[0]["body"] == body


def test_update_spool_puts_to_filament_v2_id(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {})
    cli.update_spool("GFB02", {"color": "#FF0000"})
    assert cap.calls[0]["method"] == "PUT"
    assert cap.calls[0]["url"].endswith(
        "/v1/design-user-service/my/filament/v2/GFB02")
    assert cap.calls[0]["body"] == {"color": "#FF0000"}


def test_delete_spool_deletes_filament_v2_id(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {})
    cli.delete_spool("GFB02")
    assert cap.calls[0]["method"] == "DELETE"
    assert cap.calls[0]["url"].endswith(
        "/v1/design-user-service/my/filament/v2/GFB02")
    # DELETE has no body.
    assert cap.calls[0]["body"] is None


# ===== _spool_body_from_args ============================================


def _ns(**kw) -> argparse.Namespace:
    defaults = dict(
        vendor=None, type=None, name=None, filament_id=None,
        color=None, weight=None, allow_write=False, json=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_spool_body_from_args_skips_None_fields():
    """Caller passed only --type + --color → body must contain just
    those + the manual createType default. Other fields shouldn't
    appear (server would interpret them as empty-string updates)."""
    import x2d_bridge
    ns = _ns(type="PETG", color="#00FF00")
    body = x2d_bridge._spool_body_from_args(ns)
    assert body == {
        "filamentType": "PETG",
        "color":        "#00FF00",
        "createType":   "manual",
    }


def test_spool_body_from_args_zero_weight_is_preserved():
    """0 g is a legitimate spool state (empty spool); make sure we
    don't accidentally treat it as falsey."""
    import x2d_bridge
    ns = _ns(weight=0)
    body = x2d_bridge._spool_body_from_args(ns)
    assert "weight" in body and body["weight"] == 0


# ===== --allow-write guard ==============================================


def test_cloud_spool_add_refuses_without_allow_write(monkeypatch, capsys):
    """Without --allow-write the handler must exit 1 BEFORE touching
    CloudClient. Verify the CloudClient was never even instantiated."""
    import x2d_bridge

    def _boom(cls):
        pytest.fail("CloudClient.load_or_anonymous called without "
                    "--allow-write")
    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                         classmethod(_boom))

    rc = x2d_bridge.cmd_cloud_spool_add(_ns(filament_id="GFB02"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "--allow-write" in err
    assert "add a spool" in err


def test_cloud_spool_update_refuses_without_allow_write(monkeypatch, capsys):
    import x2d_bridge

    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                         classmethod(lambda cls: pytest.fail("leaked")))

    rc = x2d_bridge.cmd_cloud_spool_update(
        _ns(filament_id="GFB02", color="#FF0000"))
    assert rc == 1
    assert "--allow-write" in capsys.readouterr().err


def test_cloud_spool_delete_refuses_without_allow_write(monkeypatch, capsys):
    import x2d_bridge

    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                         classmethod(lambda cls: pytest.fail("leaked")))

    rc = x2d_bridge.cmd_cloud_spool_delete(_ns(filament_id="GFB02"))
    assert rc == 1
    assert "--allow-write" in capsys.readouterr().err


# ===== handlers with --allow-write set ==================================


@pytest.fixture
def fake_cli_loaded(monkeypatch):
    """Logged-in fake CloudClient that the handlers will find via
    CloudClient.load_or_anonymous."""
    cli = MagicMock(spec=cloud_client.CloudClient)
    cli.session = cloud_client.Session(
        access_token="AT", refresh_token="RT",
        expires_at=time.time() + 3600,
        user_id="42", region="us")
    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                         classmethod(lambda cls: cli))
    return cli


def test_cmd_cloud_spool_add_forwards_body(fake_cli_loaded, capsys):
    import x2d_bridge

    fake_cli_loaded.add_spool.return_value = {"ok": True}
    rc = x2d_bridge.cmd_cloud_spool_add(_ns(
        allow_write=True, vendor="Bambu", type="PLA Basic",
        name="Galaxy Black", filament_id="GFB02", color="#0F0F0F",
        weight=1000))
    assert rc == 0
    fake_cli_loaded.add_spool.assert_called_once()
    body = fake_cli_loaded.add_spool.call_args.args[0]
    assert body["filamentVendor"] == "Bambu"
    assert body["filamentId"] == "GFB02"
    assert body["color"] == "#0F0F0F"
    assert body["weight"] == 1000
    assert body["createType"] == "manual"


def test_cmd_cloud_spool_update_excludes_filament_id_from_body(
        fake_cli_loaded):
    """filament_id is the path segment, not the body — body should
    contain ONLY the fields the user provided as updates."""
    import x2d_bridge

    fake_cli_loaded.update_spool.return_value = {}
    rc = x2d_bridge.cmd_cloud_spool_update(_ns(
        allow_write=True, filament_id="GFB02", color="#FF0000"))
    assert rc == 0
    fake_cli_loaded.update_spool.assert_called_once_with(
        "GFB02", {"color": "#FF0000"})


def test_cmd_cloud_spool_update_no_fields_returns_2(fake_cli_loaded, capsys):
    """`update GFB02` with NO override fields is a user error — surface
    an actionable message instead of sending an empty PUT."""
    import x2d_bridge

    rc = x2d_bridge.cmd_cloud_spool_update(_ns(
        allow_write=True, filament_id="GFB02"))
    assert rc == 2
    assert "nothing to update" in capsys.readouterr().err
    fake_cli_loaded.update_spool.assert_not_called()


def test_cmd_cloud_spool_delete_forwards_id(fake_cli_loaded, capsys):
    import x2d_bridge

    fake_cli_loaded.delete_spool.return_value = {}
    rc = x2d_bridge.cmd_cloud_spool_delete(_ns(
        allow_write=True, filament_id="GFB02"))
    assert rc == 0
    fake_cli_loaded.delete_spool.assert_called_once_with("GFB02")
    assert "deleted spool GFB02" in capsys.readouterr().out


def test_cmd_cloud_spool_add_cloud_error_returns_1(fake_cli_loaded, capsys):
    """CloudError from the client (e.g. server-side schema rejection)
    must surface as exit 1 + clean stderr, never a raw traceback."""
    import x2d_bridge

    fake_cli_loaded.add_spool.side_effect = cloud_client.CloudError(
        "400 invalid filamentType")
    rc = x2d_bridge.cmd_cloud_spool_add(_ns(
        allow_write=True, vendor="Bambu", filament_id="GFB02"))
    assert rc == 1
    assert "cloud API failed" in capsys.readouterr().err


def test_cmd_cloud_spool_add_logged_out_returns_1(monkeypatch, capsys):
    import x2d_bridge

    anon = MagicMock(spec=cloud_client.CloudClient)
    anon.session = cloud_client.Session()  # empty
    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                         classmethod(lambda cls: anon))
    rc = x2d_bridge.cmd_cloud_spool_add(_ns(
        allow_write=True, vendor="Bambu", filament_id="GFB02"))
    assert rc == 1
    assert "not logged in" in capsys.readouterr().err
    anon.add_spool.assert_not_called()
