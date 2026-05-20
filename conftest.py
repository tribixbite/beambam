"""Root conftest — shared fixtures + marker handling for beambam.

Run unit tests only (CI default):
    pytest -m "not live"

Run live tests against a real printer:
    BEAMBAM_TEST_IP=192.168.1.42 BEAMBAM_TEST_CODE=XXXXXXXX \
    BEAMBAM_TEST_SERIAL=20P9AJ... pytest -m live

Both `live` and (legacy) `X2D_TEST_*` env vars are accepted — the
former is the canonical name going forward.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

# Make the repo root importable for tests that do `import x2d_bridge` etc.
import sys
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclass(frozen=True)
class LivePrinter:
    ip: str
    code: str
    serial: str


def _read_live_env() -> Optional[LivePrinter]:
    """Return live-printer credentials if all three env vars are set.

    Accepts both `BEAMBAM_TEST_*` (canonical) and `X2D_TEST_*` (legacy).
    """
    ip = os.environ.get("BEAMBAM_TEST_IP") or os.environ.get("X2D_TEST_IP")
    code = os.environ.get("BEAMBAM_TEST_CODE") or os.environ.get("X2D_TEST_CODE")
    serial = os.environ.get("BEAMBAM_TEST_SERIAL") or os.environ.get("X2D_TEST_SERIAL")
    if ip and code and serial:
        return LivePrinter(ip=ip, code=code, serial=serial)
    return None


@pytest.fixture(scope="session")
def live_printer() -> LivePrinter:
    """Fixture that yields LivePrinter creds or skips the test if the
    BEAMBAM_TEST_{IP,CODE,SERIAL} env vars aren't all set.

    Tests that need a real printer should depend on this fixture AND
    carry @pytest.mark.live so `pytest -m "not live"` filters them
    out without invoking the fixture at all (which would skip rather
    than deselect)."""
    creds = _read_live_env()
    if creds is None:
        pytest.skip(
            "live printer not configured — set BEAMBAM_TEST_IP, "
            "BEAMBAM_TEST_CODE, BEAMBAM_TEST_SERIAL to enable"
        )
    return creds


def pytest_collection_modifyitems(config: pytest.Config,
                                  items: list[pytest.Item]) -> None:
    """Auto-tag tests that depend on `live_printer` with the `live`
    marker — saves having to remember `@pytest.mark.live` on every
    such test."""
    for item in items:
        if "live_printer" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.live)
