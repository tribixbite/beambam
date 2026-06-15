"""tests/test_x2d_project_file.py — the X2D/H2D `url_enc` project_file shape.

Captured from a real Bambu Studio desktop LAN print + verified live (the printer
accepts it with err_code 0 and starts). Distinct from the legacy shape: the file
is an RSA-encrypted `url_enc` (not a plaintext `url`), `ams_mapping` is a 10-wide
array of global slots padded with -1, `ams_mapping2` is 10-wide padded with
`{255,255}`, ids are `"0"`, and there's no `dev_id`/`bed_temp`/`plate_idx`.
"""
from __future__ import annotations

import base64

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from beambam import print_job as pj


@pytest.fixture(scope="module")
def device_cert(tmp_path_factory):
    """A self-signed RSA-4096 cert standing in for the printer's device cert
    (the real one is RSA-4096, the size that makes url_enc 512 bytes)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "00M09A000000000")])
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
    p = tmp_path_factory.mktemp("devcert") / "device.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p, key


def test_build_x2d_project_file_shape(device_cert):
    p, key = device_cert
    b = pj.build_x2d_project_file(
        "zoey_x2d.gcode.3mf", "ABC123DEF456", ams_slots=[4],
        bed_type="supertack_plate", device_cert_path=p)
    # keys are sorted (matches BS serialization)
    assert list(b.keys()) == sorted(b.keys())
    # 10-wide ams_mapping, B1=global slot 4, padded -1
    assert b["ams_mapping"] == [4, -1, -1, -1, -1, -1, -1, -1, -1, -1]
    assert b["ams_mapping2"][0] == {"ams_id": 1, "slot_id": 0}    # slot 4 -> ams1 slot0
    assert b["ams_mapping2"][1] == {"ams_id": 255, "slot_id": 255}
    assert len(b["ams_mapping2"]) == 10
    # ids all "0", cfg "4", no plaintext url / dev_id / bed_temp
    assert b["task_id"] == b["subtask_id"] == b["project_id"] == "0"
    assert b["cfg"] == "4"
    assert "url" not in b and "dev_id" not in b and "bed_temp" not in b
    assert b["subtask_name"] == "zoey_x2d"            # .gcode.3mf stripped
    assert b["bed_type"] == "supertack_plate"
    # url_enc decrypts (with the matching private key) back to the ftp url
    raw = base64.b64decode(b["url_enc"])
    assert len(raw) == 512                            # RSA-4096 block
    from cryptography.hazmat.primitives.asymmetric import padding
    dec = key.decrypt(raw, padding.PKCS1v15())
    assert dec == b"ftp:///cache/zoey_x2d.gcode.3mf"


def test_multi_filament_ams_mapping(device_cert):
    p, _ = device_cert
    b = pj.build_x2d_project_file(
        "x.gcode.3mf", "M", ams_slots=[4, 9], bed_type="cool_plate",
        device_cert_path=p)
    assert b["ams_mapping"][:2] == [4, 9]
    assert b["ams_mapping2"][0] == {"ams_id": 1, "slot_id": 0}     # 4
    assert b["ams_mapping2"][1] == {"ams_id": 2, "slot_id": 1}     # 9
    assert b["ams_mapping2"][2] == {"ams_id": 255, "slot_id": 255}


def test_start_print_takes_x2d_branch_when_cert_present(device_cert, monkeypatch):
    """With the device cert present, start_print emits the X2D body (not legacy)."""
    p, key = device_cert
    monkeypatch.setattr(pj, "_DEVICE_CERT_PATH", p)               # override conftest

    captured = {}

    class _Cli:
        # no `.client` attr → _publish_signed_or_legacy uses this publish()
        creds = type("C", (), {"serial": "S1"})()
        def publish(self, payload, qos=1, **_):
            captured["payload"] = payload

    pj.start_print(_Cli(), "zoey_x2d.gcode.3mf", use_ams=True, ams_slot=4,
                   bed_type="supertack_plate", bed_temp=35, local_path=None)
    body = captured["payload"]["print"]
    assert body["command"] == "project_file"
    assert "url_enc" in body and "url" not in body
    assert body["ams_mapping"] == [4, -1, -1, -1, -1, -1, -1, -1, -1, -1]
    assert body["task_id"] == "0"
