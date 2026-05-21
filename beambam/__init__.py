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

NOTE on transitional layout (v1.1.0): this package re-exports the public
surface from the historical top-level modules (x2d_bridge, cloud_client,
lan_print, etc.). Subsequent releases will move the implementations under
src/beambam/ proper — the re-exports here will remain stable.
"""

from __future__ import annotations

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("beambam")
except _metadata.PackageNotFoundError:
    # Editable / source checkout without `pip install -e .`
    __version__ = "1.1.0"

# Public re-exports. Implementations live in the top-level modules until
# the src/ layout refactor lands (task #3).
from beambam.config import Creds  # noqa: E402
from beambam.mqtt import BAMBU_CERT_ID, X2DClient, sign_payload  # noqa: E402
from beambam.ftps import download_file, list_files, upload_file  # noqa: E402
from beambam.printer import Printer  # noqa: E402
from x2d_bridge import main as cli  # noqa: E402

from cloud_client import CloudClient, CloudError  # noqa: E402

__all__ = [
    "__version__",
    "Printer",
    "Creds",
    "X2DClient",
    "CloudClient",
    "CloudError",
    "BAMBU_CERT_ID",
    "sign_payload",
    "upload_file",
    "download_file",
    "list_files",
    "cli",
]
