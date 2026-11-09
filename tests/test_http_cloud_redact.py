"""GET /cloud/printers must not hand out the printers' LAN access codes.

Audit finding P1: the route returned Bambu's bind list verbatim, which
carries `dev_access_code` (the MQTT/FTPS password) for every printer on
the account, to anything that could reach the daemon.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cloud_client  # noqa: E402
from beambam import http_cloud  # noqa: E402

_DEVICES = [
    {"dev_id": "SER1", "name": "X2D", "online": True,
     "dev_access_code": "12345678", "dev_product_name": "X2D",
     "nested": {"access_token": "abc", "keep": 1,
                "list": [{"password": "p", "ok": 2}]}},
    {"dev_id": "SER2", "name": "P1S", "devAccessCode": "87654321",
     "AccessCode": "x"},
]


def test_redact_secrets_strips_credential_keys():
    out = http_cloud._redact_secrets(_DEVICES)
    assert out[0] == {"dev_id": "SER1", "name": "X2D", "online": True,
                      "dev_product_name": "X2D",
                      "nested": {"keep": 1, "list": [{"ok": 2}]}}
    assert out[1] == {"dev_id": "SER2", "name": "P1S"}
    # The input is not modified.
    assert _DEVICES[0]["dev_access_code"] == "12345678"


def test_cloud_printers_route_redacts(monkeypatch):
    fake = types.SimpleNamespace(
        session=types.SimpleNamespace(empty=False),
        get_bound_devices=lambda: [dict(d) for d in _DEVICES])
    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                        staticmethod(lambda: fake))
    status, payload = http_cloud._http_cloud_printers()
    assert status == 200
    text = repr(payload)
    for secret in ("12345678", "87654321", "abc", "dev_access_code"):
        assert secret not in text
    assert [p["dev_id"] for p in payload["printers"]] == ["SER1", "SER2"]
