"""beambam — pure-Python signed-MQTT bridge for Bambu Lab printers
(Jan-2025+ firmware), plus a daemon stack (Home Assistant, MCP, web UI,
timelapse, queue, slicing helpers).

Supported printers: X1 / X1C / X1E, P1P / P1S, A1 / A1 mini, H2D / H2S /
H2C, P2S, X2D — anything that requires RSA-SHA256 signed MQTT.

Quickstart
----------

    pip install beambam

    # Configure once in ~/.x2d/credentials (or env vars X2D_IP, X2D_CODE,
    # X2D_SERIAL).
    beambam status
    beambam print mymodel.gcode.3mf

Library API
-----------

    from beambam import Printer, Creds

    p = Printer(Creds(ip="192.168.1.42", code="XXXXXXXX", serial="..."))
    state = p.state()                            # full pushall state
    p.upload("model.gcode.3mf")                  # FTPS upload
    p.start_print("model.gcode.3mf", ams=[0,1,2])  # signed MQTT

NOTE on layout (v1.2.0): `Creds` lives in beambam.config (canonical
home). The signed-MQTT helpers + FTPS file transfer + cli main are
re-exported lazily via `__getattr__` to avoid an import cycle with
x2d_bridge.py (which itself imports `Creds` from beambam.config).
v1.3.0 will move sign_payload + X2DClient + upload_file / download_file
inline as well — the import paths stay stable.
"""
from __future__ import annotations

import os
from importlib import metadata as _metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from beambam.config import Creds
from beambam.printer import Printer

try:
    __version__ = _metadata.version("beambam")
except _metadata.PackageNotFoundError:
    # Editable / source checkout without `pip install -e .`
    __version__ = "1.5.0+source"


# Repo / install root. Drives things like the web/ dir + bs-bionic
# slicer path. Overridable via the X2D_ROOT env var (handy for tests).
# Resolves to the parent of beambam/, which is the repo root in dev
# checkouts and the wheel-install site-packages dir in pip installs.
X2D_ROOT_PATH = Path(os.environ.get(
    "X2D_ROOT", str(Path(__file__).resolve().parent.parent)))

# Path to the bundled `web/` static asset dir, used by the bridge's
# HTTP server when the caller doesn't pass an explicit `web_dir`.
# `web/` is force-included alongside the package in the wheel build
# (see pyproject.toml's [tool.hatch.build.targets.wheel.force-include]).
_WEB_DIR_DEFAULT = X2D_ROOT_PATH / "web"

if TYPE_CHECKING:
    from cloud_client import CloudClient, CloudError


# Lazy re-exports from the canonical homes the bridge-split moved them to
# (x2d_bridge.py was removed in v1.5.0). The package-level `__getattr__`
# hook defers the import until the symbol is actually accessed, which also
# avoids an import cycle (beambam.mqtt imports `Creds` from beambam.config).
_LAZY_REEXPORTS = {
    "BAMBU_CERT_ID":  "beambam.mqtt",
    "X2DClient":      "beambam.mqtt",
    "sign_payload":   "beambam.mqtt",
    "upload_file":    "beambam.ftps",
    "download_file":  "beambam.ftps",
    "list_files":     "beambam.ftps",
    "cli":            ("beambam.cli", "main"),
}
_LAZY_FROM_CLOUD = {
    "CloudClient":    "cloud_client",
    "CloudError":     "cloud_client",
}


def __getattr__(name: str) -> Any:
    """Lazy load expensive / cycle-prone symbols on first access."""
    spec = _LAZY_REEXPORTS.get(name) or _LAZY_FROM_CLOUD.get(name)
    if spec is None:
        raise AttributeError(f"module 'beambam' has no attribute {name!r}")
    if isinstance(spec, tuple):
        module_name, attr_name = spec
    else:
        module_name, attr_name = spec, name
    module = __import__(module_name, fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value                                # cache on hit
    return value


# NOTE: __all__ intentionally lists only names defined at module level
# (Creds, Printer). The lazy re-exports above (X2DClient, sign_payload,
# upload_file, etc.) work via __getattr__ but ruff F822 flags them as
# "undefined in __all__". Keep both: __all__ is the "from beambam import *"
# surface (Creds + Printer); the lazy names are addressable as `beambam.X`
# directly but skip the * import expansion.
__all__ = ["Creds", "Printer", "X2D_ROOT_PATH", "_WEB_DIR_DEFAULT",
           "__version__"]
