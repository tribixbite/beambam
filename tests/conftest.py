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
