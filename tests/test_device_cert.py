"""tests/test_device_cert.py — fetch + cache the printer's RSA device cert.

The device cert's public key is what the X2D LAN print's `url_enc` encrypts to;
it's obtained from the unsigned `security.app_cert_install` `printer_cert`.
No printer needed — a fake client replays a generated cert chain.
"""
from __future__ import annotations

import datetime
import types

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from beambam import device_cert as dc


def test_glof_cn_from_cert_id(tmp_path):
    cid = tmp_path / "cert_id.txt"
    cid.write_text("0123456789abcdef0123456789abcdefCN=GLOF1000000000.bambulab.com")
    assert dc._glof_cn(cid) == "GLOF1000000000.bambulab.com"


def test_glof_cn_explicit_overrides(tmp_path):
    assert dc._glof_cn(tmp_path / "missing.txt",
                       explicit_cn="GLOF999.bambulab.com") == "GLOF999.bambulab.com"


def test_glof_cn_missing_raises(tmp_path):
    with pytest.raises(ValueError):
        dc._glof_cn(tmp_path / "missing.txt")


def test_throwaway_app_cert_is_self_signed_pair():
    cert_pem, crl_pem = dc._throwaway_app_cert("GLOF1.bambulab.com")
    cert = x509.load_pem_x509_certificate(cert_pem)
    assert cert.subject == cert.issuer                       # self-signed
    assert cert.subject.rfc4514_string() == "CN=GLOF1.bambulab.com"
    crl = x509.load_pem_x509_crl(crl_pem)
    assert crl.issuer == cert.issuer


def _make_device_chain():
    """A stand-in printer device-cert chain (leaf CN=<serial> + a fake CA)."""
    pems = []
    for cn, bits in (("00M09A000000000", 4096), ("BBL Device CA N6-V2", 2048)):
        key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        now = datetime.datetime.now(datetime.timezone.utc)
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
             .public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=3650))
             .sign(key, hashes.SHA256()))
        pems.append(c.public_bytes(serialization.Encoding.PEM).decode())
    return "".join(pems)


def test_leaf_from_chain_picks_first():
    chain = _make_device_chain()
    leaf = dc._leaf_from_chain(chain)
    assert leaf.subject.rfc4514_string() == "CN=00M09A000000000"
    assert leaf.public_key().key_size == 4096


def test_fetch_device_cert_caches_leaf(tmp_path):
    chain = _make_device_chain()

    class _Paho:
        def __init__(self): self.on_message = None
        def subscribe(self, *a, **k): pass
        def publish(self, topic, payload, qos=1):
            # simulate the printer's app_cert_install reply on /report
            import json
            msg = types.SimpleNamespace(payload=json.dumps(
                {"security": {"command": "app_cert_install",
                              "printer_cert": chain, "result": "SUCCESS"}}).encode())
            if self.on_message:
                self.on_message(self, None, msg)

    class _Cli:
        creds = types.SimpleNamespace(serial="00M09A000000000")
        client = _Paho()

    cid = tmp_path / "cert_id.txt"
    cid.write_text("deadbeefCN=GLOF1000000000.bambulab.com")
    out = tmp_path / "device.pem"
    res = dc.fetch_device_cert(_Cli(), out_path=out, cert_id_path=cid, wait=3.0)
    assert res == out and out.is_file()
    leaf = x509.load_pem_x509_certificate(out.read_bytes())
    assert leaf.subject.rfc4514_string() == "CN=00M09A000000000"
    assert leaf.public_key().key_size == 4096
