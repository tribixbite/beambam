"""beambam/cloud_control.py — command a Bambu printer over the CLOUD MQTT broker
with RSA-signed messages.

This is the path that works on X-series firmware which rejects unsigned control
(see runtime/handy_extract/SIGNER_PLAN.md): connect to `{us,cn}.mqtt.bambulab.com:8883`
as `u_<uid>` / cloud access_token, publish RSA-SHA256-signed JSON to
`device/<serial>/request`, read `device/<serial>/report`. The signing scheme +
private key are handled by `beambam.mqtt_sign` + the key recovered with
`runtime/handy_extract/extract_signing_key.py` (saved at ~/.x2d/printer_sign_key.pem).

Verified live: a signed `print.pause` is accepted by the printer (it reaches the
state machine — `reason:"ERROR STATE"` when idle — instead of being rejected with
`"mqtt message verify failed"` like the unsigned one).

Example:
    from cloud_client import CloudClient
    from beambam.cloud_control import CloudPrinter
    cp = CloudPrinter.from_config(CloudClient.load_or_anonymous(), serial="…")
    cp.pause(); cp.resume()
    cp.command("print", {"command": "gcode_line", "param": "M104 S0\\n"})
"""
from __future__ import annotations

import json
import ssl
import time
from pathlib import Path
from typing import Any, Optional

from beambam import mqtt_sign

DEFAULT_KEY_PATH = Path.home() / ".x2d" / "printer_sign_key.pem"
DEFAULT_CERTID_PATH = Path.home() / ".x2d" / "printer_cert_id.txt"


class CloudPrinter:
    """Send signed control commands to one printer over the cloud broker."""

    def __init__(self, cloud_client, serial: str, *, signer: mqtt_sign.Signer,
                 cert_id: str, region_broker: Optional[str] = None):
        self._cc = cloud_client
        self.serial = serial
        self._signer = signer
        self.cert_id = cert_id
        self._broker = region_broker or cloud_client.mqtt_broker()
        self.req = f"device/{serial}/request"
        self.rep = f"device/{serial}/report"

    # --- construction -----------------------------------------------------

    @classmethod
    def from_config(cls, cloud_client, serial: str, *,
                    key_path: Path | str = DEFAULT_KEY_PATH,
                    cert_id: Optional[str] = None,
                    certid_path: Path | str = DEFAULT_CERTID_PATH) -> "CloudPrinter":
        """Load the RSA signing key (PKCS#8 PEM) + cert_id from ~/.x2d."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
        if cert_id is None:
            cert_id = Path(certid_path).read_text().strip()
        return cls(cloud_client, serial,
                   signer=mqtt_sign.key_signer(key), cert_id=cert_id)

    # --- generic signed command ------------------------------------------

    def build(self, family: str, command: dict, *,
              sequence_id: Optional[str] = None) -> bytes:
        """Build the wire bytes for a signed command (no network). `timestamp` is
        auto-filled (ms) and `sequence_id` defaults to that timestamp; the printer
        checks freshness, so don't reuse stale ones."""
        ts = int(time.time() * 1000)
        cmd = {**command, "sequence_id": sequence_id or str(ts), "timestamp": ts}
        uid = self._cc.session.user_id
        return mqtt_sign.signed_message(family, cmd, uid,
                                        signer=self._signer, cert_id=self.cert_id)

    def command(self, family: str, command: dict, *,
                wait: float = 3.0) -> Optional[dict[str, Any]]:
        """Sign + publish a command and return the printer's matching ack
        (the report whose `<family>.sequence_id` echoes ours), or None on timeout.
        Requires `paho-mqtt`."""
        import paho.mqtt.client as mqtt

        self._cc._ensure_fresh()
        user, password = self._cc.mqtt_credentials()
        payload = self.build(family, command)
        seq = json.loads(payload)[family]["sequence_id"]
        got: dict[str, Any] = {}

        def on_connect(c, d, f, rc, props=None):
            c.subscribe(self.rep, qos=1)

        def on_message(c, d, m):
            try:
                j = json.loads(m.payload)
            except Exception:
                return
            sub = j.get(family)
            if isinstance(sub, dict) and str(sub.get("sequence_id")) == seq:
                got.update(sub)

        c = mqtt.Client(client_id=f"beambam-{seq[-6:]}", protocol=mqtt.MQTTv311)
        c.username_pw_set(user, password)
        c.tls_set_context(ssl.create_default_context())
        c.on_connect = on_connect
        c.on_message = on_message
        c.connect(self._broker, 8883, 30)
        c.loop_start()
        try:
            time.sleep(1.5)                       # let the subscribe settle
            c.publish(self.req, payload, qos=1)
            deadline = time.time() + wait
            while time.time() < deadline and not got:
                time.sleep(0.1)
        finally:
            c.loop_stop()
            c.disconnect()
        return got or None

    # --- typed conveniences ----------------------------------------------

    def pause(self) -> Optional[dict]:
        return self.command("print", {"command": "pause"})

    def resume(self) -> Optional[dict]:
        return self.command("print", {"command": "resume"})

    def stop(self) -> Optional[dict]:
        return self.command("print", {"command": "stop"})

    def skip_objects(self, obj_ids: list[int]) -> Optional[dict]:
        """Skip the given plate object ids on the running print."""
        return self.command("print", {"command": "skip_objects",
                                       "obj_list": list(obj_ids)})

    def gcode(self, line: str) -> Optional[dict]:
        if not line.endswith("\n"):
            line += "\n"
        return self.command("print", {"command": "gcode_line", "param": line})

    def get_access_code(self) -> Optional[dict]:
        return self.command("system", {"command": "get_access_code"})
