"""Shared test fixtures.

The dev machine has a recovered printer-control RSA signing key + a cloud session
(~/.x2d/), so the control verbs would auto-route over the cloud broker. Pin LAN
for the suite so the LAN-control tests still assert the LAN publish path; tests
that exercise the cloud-signed routing opt back in by deleting X2D_FORCE_LAN.
"""
import pytest


@pytest.fixture(autouse=True)
def _pin_lan_control(monkeypatch):
    monkeypatch.setenv("X2D_FORCE_LAN", "1")


@pytest.fixture(autouse=True)
def _default_legacy_project_file(monkeypatch, tmp_path_factory):
    """Default tests to the legacy (pre-auth-control) project_file shape by
    pointing start_print's device-cert probe at a missing file. The X2D/H2D
    `url_enc` shape (which needs the device cert) is covered by dedicated tests
    that override this. Without it, the dev machine's real
    ~/.x2d/printer_device_cert.pem would flip every start_print to the X2D shape."""
    import beambam.print_job as pj
    monkeypatch.setattr(
        pj, "_DEVICE_CERT_PATH",
        tmp_path_factory.mktemp("nocert") / "absent.pem")
