"""Tests for the beambam.{config,mqtt,ftps} module re-exports."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_config_module_reexports_creds():
    from beambam.config import Creds
    assert hasattr(Creds, "resolve_default")
    assert hasattr(Creds, "from_section")
    assert hasattr(Creds, "list_names")


def test_creds_resolve_default_returns_beambam_subclass():
    """resolve_default() must return the beambam.config.Creds subclass,
    not the bare x2d_bridge.Creds — so .resolve_default / .from_section
    chain on the returned instance for downstream callers."""
    from beambam.config import Creds
    # Use direct kwargs to avoid env dependency in CI.
    c = Creds(ip="1.2.3.4", code="ABCD1234", serial="FAKE12345")
    assert isinstance(c, Creds)
    assert c.ip == "1.2.3.4"


def test_mqtt_module_reexports():
    from beambam.mqtt import BAMBU_CERT_ID, X2DClient, sign_payload
    assert isinstance(BAMBU_CERT_ID, str)
    assert BAMBU_CERT_ID.startswith("GLOF")
    assert callable(sign_payload)
    assert callable(X2DClient)


def test_sign_payload_produces_envelope_with_header():
    from beambam.mqtt import sign_payload
    signed = sign_payload({"print": {"command": "pause"}})
    assert "header" in signed
    h = signed["header"]
    assert h["sign_alg"] == "RSA_SHA256"
    assert h["cert_id"].startswith("GLOF")
    assert "sign_string" in h


def test_ftps_module_reexports():
    from beambam.ftps import upload_file, download_file, list_files
    assert callable(upload_file)
    assert callable(download_file)
    assert callable(list_files)


def test_top_level_reexports_all_modules():
    """The beambam package's public surface must include the re-exported
    symbols from config / mqtt / ftps so `from beambam import X` works
    for the common cases."""
    import beambam
    for name in ("Creds", "X2DClient", "sign_payload", "upload_file",
                 "download_file", "list_files", "BAMBU_CERT_ID",
                 "Printer", "CloudClient", "CloudError"):
        assert hasattr(beambam, name), f"missing from beambam: {name}"
