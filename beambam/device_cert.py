"""beambam.device_cert — fetch + cache the printer's RSA device certificate.

The X2D / H2D LAN start-print needs the printer's **device-cert public key** to
build the `print.project_file` `url_enc` field (the file URL is RSA-encrypted
with it; the printer decrypts with its own device private key — see
`beambam.print_job._device_url_enc`). The printer returns its device-cert chain
in the response to the unsigned `security.app_cert_install` MQTT command, which
is gated only by the LAN access code (no cloud / no signature). We install a
throwaway self-signed cert (harmless — a self-signed cert is only ever accepted
for the never-signature-verified `system.*` tier, NOT for `print.*`, see
`runtime/handy_extract/DART_HEAP_KEY_EXTRACTION.md`) purely to elicit the
`printer_cert`, then cache the leaf at ~/.x2d/printer_device_cert.pem.

Usage:
    from beambam.device_cert import fetch_device_cert
    fetch_device_cert(x2d_client)          # caches the leaf cert; returns its Path
"""
from __future__ import annotations

import base64
import datetime
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from beambam.mqtt import X2DClient

DEFAULT_DEVICE_CERT_PATH = Path.home() / ".x2d" / "printer_device_cert.pem"
DEFAULT_CERTID_PATH = Path.home() / ".x2d" / "printer_cert_id.txt"


def _glof_cn(cert_id_path: Path | str = DEFAULT_CERTID_PATH,
             explicit_cn: Optional[str] = None) -> str:
    """The `CN=GLOF<n>.bambulab.com` to put on the throwaway app cert. Taken from
    the printer-control cert_id (`<hex>CN=GLOF<n>.bambulab.com`) when available —
    the printer's trust set is issued under that CN — else a caller-supplied CN."""
    if explicit_cn:
        return explicit_cn
    try:
        cid = Path(cert_id_path).read_text().strip()
        m = re.search(r"CN=([^,\s]+)", cid)
        if m:
            return m.group(1)
    except OSError:
        pass
    raise ValueError(
        "no cert_id to derive the GLOF CN from; pass cn=... "
        "(e.g. 'GLOF<printer-numeric-id>.bambulab.com')")


def _throwaway_app_cert(cn: str) -> tuple[bytes, bytes]:
    """A self-signed cert (subject=issuer=`CN=<cn>`) + a self-issued empty CRL,
    PEM bytes — the minimal pair `security.app_cert_install` accepts."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
    crl = (x509.CertificateRevocationListBuilder().issuer_name(name)
           .last_update(now).next_update(now + datetime.timedelta(days=3650))
           .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            crl.public_bytes(serialization.Encoding.PEM))


def _leaf_from_chain(chain_pem: str):
    """First (leaf) cert from a concatenated PEM chain — the printer's device
    cert (CN=<printer serial>, the RSA-4096 key url_enc encrypts to)."""
    from cryptography import x509
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        chain_pem, re.S)
    if not blocks:
        raise ValueError("no certificate in printer_cert response")
    return x509.load_pem_x509_certificate(blocks[0].encode())


def fetch_device_cert(client: "X2DClient", *,
                      cn: Optional[str] = None,
                      out_path: Path | str = DEFAULT_DEVICE_CERT_PATH,
                      cert_id_path: Path | str = DEFAULT_CERTID_PATH,
                      wait: float = 8.0) -> Path:
    """Elicit + cache the printer's device cert via `security.app_cert_install`.

    `client` must be a connected `X2DClient` (LAN broker). Publishes the unsigned
    install with a throwaway self-signed cert, captures the `printer_cert` chain
    from the report, writes the leaf to `out_path` (chmod 600). Returns the Path.
    Raises TimeoutError if the printer doesn't return the chain within `wait`."""
    from cryptography.hazmat.primitives import serialization

    cert_pem, crl_pem = _throwaway_app_cert(_glof_cn(cert_id_path, cn))
    serial = client.creds.serial
    got: dict = {}

    def _on(c, d, m):
        try:
            j = json.loads(m.payload)
        except Exception:                                  # noqa: BLE001
            return
        s = j.get("security")
        if (isinstance(s, dict) and s.get("command") == "app_cert_install"
                and "printer_cert" in s):
            got["chain"] = s["printer_cert"]

    prev = client.client.on_message
    client.client.on_message = _on
    client.client.subscribe(f"device/{serial}/report", qos=1)
    time.sleep(1.0)
    client.client.publish(f"device/{serial}/request", json.dumps({
        "security": {"sequence_id": str(int(time.time() * 1000)),
                     "command": "app_cert_install",
                     "app_cert": base64.b64encode(cert_pem).decode(),
                     "crl": base64.b64encode(crl_pem).decode()}}), qos=1)
    deadline = time.time() + wait
    while time.time() < deadline and "chain" not in got:
        time.sleep(0.2)
    client.client.on_message = prev
    if "chain" not in got:
        raise TimeoutError("printer did not return its device cert "
                           "(app_cert_install) within the timeout")
    leaf = _leaf_from_chain(got["chain"])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    try:
        out.chmod(0o600)
    except OSError:
        pass
    return out
