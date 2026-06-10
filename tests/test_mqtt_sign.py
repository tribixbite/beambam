"""tests/test_mqtt_sign.py — the printer-control MQTT signing scheme.

Two kinds of check:
  1. Round-trip with a stand-in RSA key (the code is correct).
  2. A REAL captured Bambu Handy `print.stop` signature verifies against the real
     app-cert public key with our reconstructed pre-image — this is the proof that
     the reverse-engineered scheme (`{"<fam>":<cmd>,"user_id":"<uid>"}`, SHA-256,
     RSA-PKCS#1-v1.5) is exactly what the firmware signs.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from beambam import mqtt_sign  # noqa: E402

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers  # noqa: E402


# --- real capture (print.stop), public data only -------------------------------
REAL = {
    "family": "print",
    "command": {"command": "stop", "sequence_id": "2009", "timestamp": 1780996597017},
    "user_id": "2000000001",
    "sign_string": (
        "wuCtstj3csDe9BcrozO2RYbvWF+wwi88nGH0sOHXTJLahSuH89sHc53FopoKD4X8qtp8Xc1Htp+"
        "QUILnWOJOU91/MkVuTJ2uKMKiBOg1/eLwfJTJyA7dG1BDxIEhRXt7OWgWZ1j5ptz5NJUq1q1XeDf"
        "BiYn6PiHbAIbePjht/75QyFevdCw6cDAB2QZLJ8L9VLkWYUFcBjCqEG3apoqOF+hL8GPPwmDi+wW"
        "BDwBbhLrn7FZIkbpRij9hNBVk9BLOAXuY0NEQWU9bcEW9z4tmLPSCQyNIjQtpz1P2IT52zuUTpJL"
        "ue2/XhSsucWYC8IAJLGiKIJl2soQ8/QBQvqhgcw=="),
    "cert_id": "0123456789abcdef0123456789abcdefCN=GLOF1000000000.bambulab.com",
    "payload_len": 98,
    "n_hex": (
        "c2ee2cae650ffc11be0fe403dfa2d0feaec4a98c1d11bdd3a6e2d879c7469be306aa428a929c"
        "5c3876cf748218a2c1906c17285c39507bb5782261cdf318a8b9042e5e2bd4c0f9d957ee3c7b1"
        "e6c6e8292987b4d8cbff87cf0ca4a8c9ce0a94675e280ee912e948f3adc5de314f92b57fd21dc"
        "c337f7b4e1b444f9548d24da49a7fe6a851c235bf67fdfb03b2fd729df433d67822ccc6f21831"
        "391b235f66d937462e6345ca67633ce0f4ea2bdd5bb1f3b2a9460d87c40c6aa210f1c3563821d"
        "091eef0a76dfe394851bf3626d5851de89f13e6d3a3d7dd416f938619914268a0115cf8f83592"
        "03e3309e46530b61ca2f1ef20b2bcb57f50fcb6877fc24ecf97"),
    "e": 65537,
}


def _real_pubkey():
    return RSAPublicNumbers(REAL["e"], int(REAL["n_hex"], 16)).public_key()


def test_preimage_is_body_minus_header():
    pre = mqtt_sign.preimage(REAL["family"], REAL["command"], REAL["user_id"])
    assert pre == (
        b'{"print":{"command":"stop","sequence_id":"2009","timestamp":1780996597017},'
        b'"user_id":"2000000001"}')
    assert len(pre) == REAL["payload_len"] == 98


def test_real_captured_signature_verifies():
    """The crux: the genuine Handy signature verifies against the app cert pubkey
    using our pre-image — i.e. we reproduce exactly what the firmware signs."""
    published = (mqtt_sign.preimage(REAL["family"], REAL["command"], REAL["user_id"])[:-1]
                 + b',"header":' + json.dumps({
                     "sign_ver": "v1.0", "sign_alg": "RSA_SHA256",
                     "sign_string": REAL["sign_string"], "cert_id": REAL["cert_id"],
                     "payload_len": REAL["payload_len"]},
                     separators=(",", ":")).encode() + b'}')
    assert mqtt_sign.verify_message(published, _real_pubkey()) is True


def test_cert_id_construction():
    assert mqtt_sign.make_cert_id(
        "0123456789abcdef0123456789abcdef", "1000000000") == REAL["cert_id"]


def test_signed_message_roundtrip_with_standin_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    msg = mqtt_sign.signed_message(
        "print", {"command": "pause", "sequence_id": "0"}, "2000000001",
        signer=mqtt_sign.key_signer(key), cert_id="DEADBEEFCN=GLOF1.bambulab.com")
    # wire shape: body + header, valid JSON, header last
    obj = json.loads(msg)
    assert obj["print"]["command"] == "pause"
    assert obj["user_id"] == "2000000001"
    assert obj["header"]["sign_alg"] == "RSA_SHA256"
    assert obj["header"]["payload_len"] == len(
        mqtt_sign.preimage("print", {"command": "pause", "sequence_id": "0"}, "2000000001"))
    # and it verifies against the stand-in public key
    assert mqtt_sign.verify_message(msg, key.public_key()) is True


def test_tampered_message_fails_verify():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    msg = mqtt_sign.signed_message(
        "print", {"command": "pause", "sequence_id": "0"}, "2000000001",
        signer=mqtt_sign.key_signer(key), cert_id="X")
    tampered = msg.replace(b'"pause"', b'"stop"x'[:7]) if b'"pause"' in msg else msg
    # flip the command -> signature no longer matches
    bad = json.loads(msg)
    bad["print"]["command"] = "stop"
    bad_bytes = (mqtt_sign.preimage("print", bad["print"], "2000000001")[:-1]
                 + b',"header":' + json.dumps(bad["header"], separators=(",", ":")).encode() + b'}')
    assert mqtt_sign.verify_message(bad_bytes, key.public_key()) is False
