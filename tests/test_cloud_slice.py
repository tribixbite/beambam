"""tests/test_cloud_slice.py — the cloud-slice REST signing.

The crux: `x-bbl-device-security-sign` is a raw PKCS#1 v1.5 RSA signature of the
ASCII millisecond-timestamp string (no hash). We reproduce a REAL captured Handy
signature byte-for-byte, which proves the scheme. (Network-free — no actual POST.)
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric import rsa

from beambam import cloud_slice


def test_device_sign_roundtrips_to_timestamp():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ts = 1781283791985
    sig_b64 = cloud_slice.device_security_sign(key, ts)
    # RSA-decrypt with the public half → recover the padded timestamp
    n = key.public_key().public_numbers().n
    e = key.public_key().public_numbers().e
    em = pow(int.from_bytes(base64.b64decode(sig_b64), "big"), e, n).to_bytes(256, "big")
    assert em[:2] == b"\x00\x01"                      # PKCS#1 v1.5 type 1
    recovered = em[em.rfind(b"\x00") + 1:].decode("ascii")
    assert recovered == str(ts)


def test_app_certification_id_swaps_halves():
    cert_id = "0123456789abcdef0123456789abcdefCN=GLOF1000000000.bambulab.com"
    assert (cloud_slice.app_certification_id(cert_id)
            == "CN=GLOF1000000000.bambulab.com:0123456789abcdef0123456789abcdef")


def test_signed_headers_has_both():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    h = cloud_slice.signed_headers(
        key, "deadbeefCN=GLOF1.bambulab.com", 1781283791985)
    assert h["x-bbl-app-certification-id"] == "CN=GLOF1.bambulab.com:deadbeef"
    assert len(base64.b64decode(h["x-bbl-device-security-sign"])) == 256


def test_build_cloud_slice_body_shape():
    b = cloud_slice.build_cloud_slice_body(
        design_id=2831282, model_id="US76", instance_id=3154751,
        profile_id=777165123, title="0.2mm", cover="http://x", serial="S1",
        ams_mapping=[9],
        ams_detail_mapping=[{"ams": 9, "amsId": 2, "slotId": 1, "nozzleId": 1}],
        nozzle_infos=[{"id": 1, "diameter": 0.4}, {"id": 0, "diameter": 0.4}],
        filament_setting_ids=["GFSG99_15"], bed_type="Supertack Plate")
    assert b["mode"] == "cloud_slice"
    assert b["amsMapping"] == [9]
    assert b["amsMapping2"] == [{"amsId": 2, "slotId": 1}]    # 9 -> ams2 slot1
    assert b["bedType"] == "Supertack Plate"
    assert b["hasFilamentSwitcher"] == 1
    assert len(b["nozzleInfos"]) == 2
