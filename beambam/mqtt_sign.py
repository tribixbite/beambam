"""beambam/mqtt_sign.py — build the RSA-signed printer-control MQTT messages that
Bambu's X-series firmware requires.

Verified (2026-06-09/10) that the printer rejects unsigned control commands
(`print.pause` → `"mqtt message verify failed"`), and the signing scheme was
reverse-engineered offline from captured Bambu Handy traffic (see
`runtime/handy_extract/` + `SIGNER_PLAN.md`):

  * The **signed pre-image is the message body WITHOUT the header** —
    `{"<family>":<command>,"user_id":"<uid>"}` in compact JSON, family key first.
  * It is SHA-256 hashed and **RSA-PKCS#1-v1.5** signed with the app's per-install
    RSA-2048 private key (proved: all 11 captured signatures verify against the
    app cert's public key with this exact pre-image).
  * A `header` sibling is then appended to those exact bytes:
    `{sign_ver:"v1.0", sign_alg:"RSA_SHA256", sign_string:<b64 sig>,
      cert_id:"<appCertSerialHex>CN=GLOF<printerSerial>.bambulab.com",
      payload_len:<len(pre-image)>}`.

The full scheme is known; the ONLY missing input is the app's private key, which
is held in the Dart/SHIELD layer and not yet extractable (see SIGNER_PLAN.md).
Supply a `signer` — either a loaded `cryptography` RSA private key via
`key_signer()`, or an oracle callable that proxies to a running Handy — and this
module produces wire-ready bytes for `device/<serial>/request`.
"""
from __future__ import annotations

import base64
import json
from typing import Callable

SIGN_VER = "v1.0"
SIGN_ALG = "RSA_SHA256"

# A signer takes the pre-image bytes and returns the raw RSA-PKCS#1-v1.5-SHA256
# signature bytes (256 bytes for RSA-2048).
Signer = Callable[[bytes], bytes]


def preimage(family: str, command: dict, user_id: str) -> bytes:
    """The exact bytes the firmware hashes+verifies: the message body minus the
    header. Key order is fixed (family first, then user_id) and JSON is compact.
    The published message MUST reuse these exact bytes, so callers build on top of
    this rather than re-serialising the whole object (see `signed_message`)."""
    body = '{"%s":%s,"user_id":"%s"}' % (
        family, json.dumps(command, separators=(",", ":")), user_id)
    return body.encode("utf-8")


def make_cert_id(app_cert_serial_hex: str, printer_serial: str) -> str:
    """`cert_id = <app cert X.509 serial number, hex><CN=GLOF<printerSerial>.bambulab.com>`
    (verified: the leading hex blob is exactly the app cert's serial number, not a
    digest). For a given install it is constant, so it can also be copied verbatim
    from a capture."""
    return f"{app_cert_serial_hex}CN=GLOF{printer_serial}.bambulab.com"


def signed_message(family: str, command: dict, user_id: str, *,
                   signer: Signer, cert_id: str) -> bytes:
    """Build the wire-ready signed publish payload for `device/<serial>/request`.

    `signer(pre_image_bytes) -> raw_rsa_signature_bytes`. The header is appended
    to the EXACT signed bytes (we drop the pre-image's closing `}` and re-add it
    after the header) so the bytes the printer hashes are byte-identical to what
    was signed."""
    pre = preimage(family, command, user_id)
    sig = signer(pre)
    header = {
        "sign_ver": SIGN_VER,
        "sign_alg": SIGN_ALG,
        "sign_string": base64.b64encode(sig).decode("ascii"),
        "cert_id": cert_id,
        "payload_len": len(pre),
    }
    return (pre[:-1] + b',"header":'
            + json.dumps(header, separators=(",", ":")).encode("utf-8") + b'}')


def key_signer(private_key) -> Signer:
    """Adapt a `cryptography` RSA private key into a `Signer` (RSA-PKCS#1-v1.5,
    SHA-256). Use once the app key is recovered, or with a stand-in key in tests."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def _sign(pre: bytes) -> bytes:
        return private_key.sign(pre, padding.PKCS1v15(), hashes.SHA256())

    return _sign


def verify_message(published: bytes, public_key) -> bool:
    """Verify a signed message (built here, or captured from Handy) against the app
    cert's RSA public key — proves the pre-image + signature are consistent. Note:
    re-derives the pre-image by compact re-serialisation, which matches Handy's
    output for the captured commands."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    obj = json.loads(published)
    header = obj.get("header") or {}
    sig = base64.b64decode(header["sign_string"])
    family = next(k for k in obj if k not in ("user_id", "header"))
    pre = preimage(family, obj[family], obj["user_id"])
    if len(pre) != header.get("payload_len"):
        return False
    try:
        public_key.verify(sig, pre, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False
