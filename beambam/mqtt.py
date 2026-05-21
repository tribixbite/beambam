"""beambam.mqtt — signed MQTT helpers.

Re-exports the cert + signing + publish primitives from x2d_bridge.
Future v1.3.0 will move the implementation inline; the import path
stays stable:

    from beambam.mqtt import sign_payload, X2DClient, BAMBU_CERT_ID

Signing
-------

Bambu's Jan-2025+ firmware rejects MQTT messages whose `header.sign_string`
doesn't verify against a recognised RSA cert. The bridge uses the
publicly-leaked Bambu Connect cert embedded in `bambu_cert.py`:

    payload = {"print": {"sequence_id": "0", "command": "pause"}}
    signed = sign_payload(payload)
    # signed["header"]["sign_string"] = "<base64 RSA-SHA256 over payload>"
    # signed["header"]["cert_id"]     = "GLOF381...524a..."

X2DClient
---------

`X2DClient(creds)` is the long-lived MQTT connection. Use
`beambam.Printer` for the high-level facade instead — X2DClient is the
escape hatch for advanced flows (custom callbacks, raw subscribe,
multi-publish batching). Holds a paho client + sign cache + last-message
timestamp persisted under ~/.x2d/last_message_ts_<serial>.
"""
from __future__ import annotations

from x2d_bridge import (
    BAMBU_CERT_ID,
    X2DClient,
    sign_payload,
)

__all__ = ["BAMBU_CERT_ID", "X2DClient", "sign_payload"]
