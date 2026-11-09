"""beambam.signing: what LAN print-control signing is missing (local files only)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from beambam.signing import signing_status


def _cert(path: Path, not_after: dt.datetime) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GLOFTEST")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(not_after - dt.timedelta(days=400)).not_valid_after(not_after)
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BEAMBAM_HOME", str(tmp_path))
    monkeypatch.delenv("X2D_USER_ID", raising=False)
    return tmp_path


NOW = dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc)


def test_nothing_set_up(home: Path) -> None:
    st = signing_status(NOW)
    assert not st.ready
    assert any("beambam key" in p for p in st.problems)
    assert any("user_id" in p for p in st.problems)


def test_expired_app_cert_is_named(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "printer_sign_key.pem").write_text("k")
    (home / "printer_cert_id.txt").write_text("id")
    _cert(home / "printer_app_cert.pem", dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc))
    monkeypatch.setenv("X2D_USER_ID", "12345")
    st = signing_status(NOW)
    assert st.problems == ["the Handy app cert expired 2026-08-22 — re-capture it and re-run `beambam key`"]
    assert st.user_id_source == "X2D_USER_ID"


def test_ready_with_session_user_id(home: Path) -> None:
    (home / "printer_sign_key.pem").write_text("k")
    (home / "printer_cert_id.txt").write_text("id")
    _cert(home / "printer_app_cert.pem", dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))
    (home / "cloud_session.json").write_text(json.dumps({"user_id": "777"}))
    st = signing_status(NOW)
    assert st.ready and st.user_id_source == "cloud_session.json"


def test_signing_status_device_cert_is_per_serial(home: Path) -> None:
    """The key + cert id are global (they belong to the Handy install); only the
    device cert is per printer, with a fallback to the global file (F-03)."""
    (home / "printer_sign_key.pem").write_text("k")
    (home / "printer_cert_id.txt").write_text("c")
    # Key + cert id are found whatever the serial.
    assert signing_status(NOW, serial="SN-A").key and signing_status(NOW, serial="SN-A").cert_id
    # No device cert anywhere -> missing.
    assert not signing_status(NOW, serial="SN-A").device_cert
    # A per-serial device cert counts only for its own printer.
    (home / "SN-A").mkdir()
    (home / "SN-A" / "printer_device_cert.pem").write_text("d")
    assert signing_status(NOW, serial="SN-A").device_cert
    assert not signing_status(NOW, serial="SN-B").device_cert
    # A legacy global device cert is the fallback for any printer.
    (home / "printer_device_cert.pem").write_text("d")
    assert signing_status(NOW, serial="SN-B").device_cert


def test_cert_id_is_app_cert_serial_plus_issuer_cn() -> None:
    """cert_id = app cert serial (hex) + CN=<issuer CN>, not the printer serial."""
    from beambam import mqtt_sign
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GLOF1234567890.bambulab.com")])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GLOF-app-user")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key()).serial_number(0x77BCFB01)
            .not_valid_before(NOW - dt.timedelta(days=1))
            .not_valid_after(NOW + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    assert mqtt_sign.cert_id_from_app_cert(cert) == "77bcfb01CN=GLOF1234567890.bambulab.com"
    assert mqtt_sign.make_cert_id("77bcfb01", "1234567890") == "77bcfb01CN=GLOF1234567890.bambulab.com"
