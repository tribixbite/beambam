"""tests/test_mqtt_sign.py — the printer-control MQTT signing scheme.

Two kinds of check:
  1. Round-trip with a stand-in RSA key (the code is correct).
  2. A FROZEN stand-in vector — a generated key signing the reconstructed
     pre-image — verifies against its public key. This is a regression guard on
     the exact byte serialization (`{"<fam>":<cmd>,"user_id":"<uid>"}`, SHA-256,
     RSA-PKCS#1-v1.5): if the pre-image format ever drifts, the frozen signature
     stops verifying. The real device key material that the live firmware signs
     with is never committed — it lives only in `~/.x2d/printer_sign_key.pem`.
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


# --- frozen stand-in vector (no real key material) ------------------------------
# Generated once: a throwaway RSA-2048 key signs the EXACT reconstructed pre-image
# for a `print.stop`. Same shape as a live capture, but the modulus + signature
# belong to a disposable key, and the uid / cert-id are placeholders.
REAL = {
    "family": "print",
    "command": {"command": "stop", "sequence_id": "2009", "timestamp": 1780996597017},
    "user_id": "2000000001",
    "sign_string": (
        "B6zPQ8N2xMhf59SQRB6KDZDz9LXyWbTokjYHElDJ5IoGdIrPzKg5aHsbWoEjdtVBtfdMufDLDLz0"
        "FQlXRtxDSyiNR0Dn/5vhd4MxXe1/3wXkbeQpsagjDxSk8B6G3xI5ndjkr0nju6+gVYeuRqxPXVrY"
        "GxKLum3jjB5HEgjtWYHcCKBCUraj9qgjzf9Rw79wneuEyMwz8q8cb4z5a8xBOJFI8Fjob2ic65Kg"
        "/BxP8ayj+RnGU/xRKsmvym16gPvYXl22ArOvzCjq0a4wLfNV5XxP8GSL+PCFNzlnA2Ybjt4q+/Mb"
        "gbdA4ZhvjK+XYMctltd2azRnq+eDCUmq+8Xe+A=="),
    "cert_id": "0123456789abcdef0123456789abcdefCN=GLOF1000000000.bambulab.com",
    "payload_len": 98,
    "n_hex": (
        "c5030c9e27a6af72df99c3b0b6d9cf3f34341e024caa3060974cfa9664145108376686d252b24"
        "8b071c56c2ae828168fc843e03be1abdee8495a3f8ab5aa2496429adda1fdb29f4c7e4c6f3d42"
        "86faf79b39c3480af7999568ee19a34d88f0630d62981ea67ea903921604e536175da2f03daa2"
        "6ff0ea45903f455f3e55840911cf2d8d050e700fd1e6b0ec60898928a966cddc0f632c4c2eb3d"
        "9d0c05f4e55e87c8a6287c4df31ddbfe551e3c6de20acd83e39de89ea4bc0c078ed3f7428082e"
        "7caf157e63a7fcc151767429ac48a8b5747666d88a5a08b9551ebb5e492345c2f7e4887a19b16"
        "c668a5d86c8a9d5af731dbdc2e5c52eb9c8bd9bdbd26d818c5"),
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


def test_frozen_vector_verifies():
    """The frozen stand-in signature verifies against its public key using our
    pre-image — proves the construction byte-for-byte without real key material."""
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
