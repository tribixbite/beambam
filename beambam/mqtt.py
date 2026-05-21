"""beambam.mqtt — signed MQTT helpers.

Canonical home of the cert ID + signing primitive as of v1.2.0:

    from beambam.mqtt import sign_payload, BAMBU_CERT_ID

Signing
-------
Bambu's Jan-2025+ firmware rejects MQTT messages whose `header.sign_string`
doesn't verify against a recognised RSA cert. The bridge uses the
publicly-leaked Bambu Connect cert (cert_id GLOF1000000000-...) shipped
in `bambu_cert.py`:

    payload = {"print": {"sequence_id": "0", "command": "pause"}}
    signed = sign_payload(payload)
    # signed["header"]["sign_string"] = "<base64 RSA-SHA256 over payload>"
    # signed["header"]["cert_id"]     = BAMBU_CERT_ID
    # signed["header"]["payload_len"] = len(compact-JSON of payload)

CRITICAL ORDERING: the signature is computed against the compact-JSON of
the un-headered dict in DICT-INSERTION ORDER. `sort_keys=True` breaks
ALL commands (pause/resume/print/etc.) because the firmware re-serializes
the parsed payload in receive order. Always use `json.dumps(payload,
separators=(",", ":"))` with no sort_keys.

X2DClient
---------
Long-lived MQTT connection facade. Use `beambam.Printer` for the
high-level interface — X2DClient is the escape hatch for advanced flows
(custom callbacks, raw subscribe, multi-publish batching). Still lives
in x2d_bridge.py for v1.2.0; will move inline in v1.3.0.
"""
from __future__ import annotations

import base64
import json
import sys
from typing import Any


# X2DClient is reachable as `beambam.mqtt.X2DClient` via __getattr__ but
# excluded from __all__ (ruff F822: undefined name in __all__) — it's
# not a module-level binding until first access. Use the attribute access
# directly for typing too.
__all__ = ["BAMBU_CERT_ID", "sign_payload"]


# Cert ID — public-leak constant. Falls back to the literal if the user's
# bambu_cert.py doesn't define it (some leaked copies omit it).
try:
    from bambu_cert import BAMBU_CERT_ID
except (ImportError, AttributeError):
    BAMBU_CERT_ID = "GLOF1000000000-524a37c80000c6a6a274a47b3281"


def _load_private_key():
    """Lazy-load + decode the RSA private key. Cached so subsequent
    sign_payload calls don't pay the parse cost."""
    try:
        from bambu_cert import BAMBU_PRIVATE_KEY_PEM
    except ImportError:
        BAMBU_PRIVATE_KEY_PEM = None
    if BAMBU_PRIVATE_KEY_PEM is None:
        sys.exit(
            "Bambu signing cert missing. Place the PEM-encoded private key in\n"
            "  bambu_cert.py:BAMBU_PRIVATE_KEY_PEM\n"
            f"next to this script. The cert is the publicly-leaked Bambu\n"
            f"Connect global cert — search public references for\n"
            f"`{BAMBU_CERT_ID}` if you need a copy."
        )
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(
        BAMBU_PRIVATE_KEY_PEM.encode(), password=None
    )


# Cache the parsed key after first call.
_PRIVATE_KEY = None


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a Bambu MQTT payload with the `header` block the X2D / H2D /
    refreshed P1+X1 firmware require. Returns a NEW dict with `header`
    appended; the input is not mutated."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        _PRIVATE_KEY = _load_private_key()

    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = _PRIVATE_KEY.sign(body, padding.PKCS1v15(), hashes.SHA256())
    out = dict(payload)
    out["header"] = {
        "sign_ver":     "v1.0",
        "sign_alg":     "RSA_SHA256",
        "sign_string":  base64.b64encode(sig).decode("ascii"),
        "cert_id":      BAMBU_CERT_ID,
        "payload_len":  len(body),
    }
    return out


def __getattr__(name: str) -> Any:
    """X2DClient stays in x2d_bridge for v1.2.0 — lazy-import on access
    to avoid the circular import (x2d_bridge imports beambam.config)."""
    if name == "X2DClient":
        from x2d_bridge import X2DClient
        return X2DClient
    raise AttributeError(f"module 'beambam.mqtt' has no attribute {name!r}")
