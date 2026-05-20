"""beambam.printer — high-level Printer class for stateful library use.

Wraps the lower-level X2DClient + module-level helpers from x2d_bridge
into a single object that holds one MQTT connection across multiple
calls. Replaces the typical "open MQTT, sign, publish, close" loop that
every cmd_* in the CLI does.

Quick example:

    from beambam import Printer, Creds

    creds = Creds(ip="192.168.1.42", code="XXXXXXXX", serial="20P9...")
    with Printer(creds) as p:
        s = p.state()                                    # full pushall state
        print(s["print"]["gcode_state"])                 # RUNNING/IDLE/FINISH
        p.upload("model.gcode.3mf")                      # FTPS upload
        p.start_print("model.gcode.3mf", ams=[1, 5])     # signed MQTT start
        p.pause()
        # ...
        p.resume()

If you don't pass `Creds`, the Printer resolves them from the same
sources the CLI uses: $X2D_IP / $X2D_CODE / $X2D_SERIAL, or
~/.x2d/credentials [printer:<name>] sections. `Printer.from_env()` is
an explicit helper that mirrors `Creds.resolve()`.

Thread safety: one Printer object → one MQTT connection. paho's loop
runs in its own thread, so concurrent method calls on a single Printer
are safe (paho serializes publishes internally). Concurrent Printer
objects targeting the same IP+serial work too — each holds its own
MQTT session.

NOTE on v1.1.0: methods delegate to x2d_bridge.* functions for now.
v1.2.0's bridge split will lift those impls into beambam.{mqtt,ftps,
cloud}.* and Printer becomes the canonical implementation.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

# Delayed imports — x2d_bridge is heavy and pulls paho+crypto at module
# load. Defer until Printer() is actually constructed.


class Printer(AbstractContextManager):
    """High-level facade over X2DClient + FTPS helpers + cloud client.

    Lazy MQTT connection: the first method that needs MQTT (state,
    publish, pause, etc.) opens it. FTPS-only methods (upload, download,
    list_files) don't touch MQTT.
    """

    def __init__(self, creds=None) -> None:
        from x2d_bridge import Creds  # noqa: E402
        if creds is None:
            creds = Creds.resolve(argparse.Namespace(
                ip=None, code=None, serial=None, printer=None,
            ))
        self.creds = creds
        self._client = None                    # X2DClient — lazy
        self._cloud = None                     # CloudClient — lazy

    # ----- factories -------------------------------------------------------

    @classmethod
    def from_env(cls) -> "Printer":
        """Resolve creds from $X2D_IP / $X2D_CODE / $X2D_SERIAL or
        ~/.x2d/credentials. Raises SystemExit if no creds available."""
        return cls(creds=None)

    @classmethod
    def from_credentials_section(cls, name: str) -> "Printer":
        """Pick the named [printer:NAME] section from ~/.x2d/credentials."""
        from x2d_bridge import Creds
        creds = Creds.resolve(argparse.Namespace(
            ip=None, code=None, serial=None, printer=name,
        ))
        return cls(creds=creds)

    # ----- MQTT (lazy) -----------------------------------------------------

    @property
    def mqtt(self):
        """X2DClient — opens MQTT on first access. The connection stays
        live until disconnect() / close() / __exit__."""
        if self._client is None:
            from x2d_bridge import X2DClient
            self._client = X2DClient(self.creds)
            self._client.connect()
        return self._client

    def disconnect(self) -> None:
        """Tear down the MQTT connection. FTPS calls still work
        afterwards; subsequent MQTT calls will reopen lazily."""
        if self._client is not None:
            try:
                self._client.disconnect()
            finally:
                self._client = None

    close = disconnect  # alias for symmetry with file-like classes

    # ----- Context manager -------------------------------------------------

    def __enter__(self) -> "Printer":
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.disconnect()

    # ----- High-level methods ---------------------------------------------

    def state(self, timeout: float = 8.0) -> dict[str, Any]:
        """Pull the printer's full pushall state. Returns the parsed
        JSON the firmware emits; key shape is `{"print": {...}, "info":
        {...}}`. Raises TimeoutError if the printer doesn't respond
        within `timeout` seconds."""
        return self.mqtt.request_state(timeout=timeout)

    def publish(self, payload: dict[str, Any]) -> None:
        """Sign and publish a raw payload on device/<serial>/request.
        For escape hatches when the typed methods don't cover what you
        need. The payload is signed exactly like every cmd_* publishes."""
        self.mqtt.publish(payload)

    # --- printing ---------------------------------------------------------

    def start_print(self, gcode_filename: str, *,
                    ams: int | list[int] | None = None,
                    use_ams: bool | None = None,
                    bed_type: str | None = None,
                    bed_temp: int | None = None,
                    local_path: str | Path | None = None,
                    timelapse: bool = False,
                    flow_cali: bool = False,
                    bed_levelling: bool = True,
                    vibration_cali: bool = False) -> dict[str, Any]:
        """Send the signed start_print MQTT. `ams` accepts an int (single
        slot 0..15) or a list of ints (multi-filament). `use_ams=False`
        with `ams=None` switches to external spool. `local_path`
        auto-derives bed_type+bed_temp from the .gcode.3mf — pass it
        when uploading + printing in one shot. Returns the wire payload
        that was published (useful for logging/diff in simulate mode)."""
        from x2d_bridge import start_print
        if use_ams is None:
            use_ams = ams is not None
        if ams is None:
            ams_slot: int | list[int] = 0
        else:
            ams_slot = ams
        start_print(
            self.mqtt, gcode_filename,
            use_ams=use_ams, ams_slot=ams_slot,
            bed_type=bed_type, bed_temp=bed_temp,
            local_path=Path(local_path) if local_path else None,
            timelapse=timelapse, flow_cali=flow_cali,
            bed_levelling=bed_levelling, vibration_cali=vibration_cali,
        )
        return getattr(self.mqtt, "published", {}) or {}

    def pause(self) -> None:
        self.mqtt.publish({"print": {"sequence_id": "0", "command": "pause"}})

    def resume(self) -> None:
        self.mqtt.publish({"print": {"sequence_id": "0", "command": "resume"}})

    def stop(self) -> None:
        self.mqtt.publish({"print": {"sequence_id": "0", "command": "stop"}})

    # --- direct control ---------------------------------------------------

    def gcode(self, line: str) -> None:
        """Send a single raw G-code line. The printer accepts most
        Marlin-ish commands (G/M/T) and a smattering of Bambu-specific
        M codes — see docs/LOCAL_CONTROL_PATHS.md."""
        self.mqtt.publish({
            "print": {"sequence_id": "0", "command": "gcode_line",
                      "param": line + ("\n" if not line.endswith("\n") else "")}
        })

    def set_temp(self, *, nozzle: int | None = None,
                 bed: int | None = None,
                 chamber: int | None = None) -> None:
        """Set any combination of nozzle / bed / chamber temperatures.
        Any param left None is unchanged."""
        gcodes = []
        if nozzle is not None:
            gcodes.append(f"M104 S{int(nozzle)}")
        if bed is not None:
            gcodes.append(f"M140 S{int(bed)}")
        if chamber is not None:
            gcodes.append(f"M141 S{int(chamber)}")
        for g in gcodes:
            self.gcode(g)

    def chamber_light(self, on: bool, *, pwm: int = 255) -> None:
        """Toggle the chamber light. `pwm` overrides brightness 0-255
        (only honored when `on=True`)."""
        self.mqtt.publish({
            "system": {"sequence_id": "0", "command": "ledctrl",
                       "led_node": "chamber_light",
                       "led_mode": "on" if on else "off",
                       "led_on_time": 500, "led_off_time": 500,
                       "loop_times": 0, "interval_time": 0}
        })

    def ams_load(self, slot: int) -> None:
        """Load filament from AMS global slot (0..15) into the active
        extruder."""
        self.mqtt.publish({
            "print": {"sequence_id": "0", "command": "ams_change_filament",
                      "target": int(slot), "curr_temp": 215, "tar_temp": 215}
        })

    def ams_unload(self) -> None:
        """Unload the currently-loaded filament back to its AMS slot."""
        self.mqtt.publish({
            "print": {"sequence_id": "0", "command": "ams_change_filament",
                      "target": 255, "curr_temp": 215, "tar_temp": 215}
        })

    def home(self, *, axes: str = "XYZ") -> None:
        """Home one or more axes. `axes='Z'` for Z-only, 'XY' for the
        bed, 'XYZ' (default) for everything."""
        self.gcode(f"G28 {' '.join(axes)}")

    # --- FTPS (no MQTT) ---------------------------------------------------

    def upload(self, local_path: str | Path,
               remote_name: str | None = None) -> None:
        """FTPS-implicit-TLS upload to the printer's SD card. No MQTT;
        the file appears on the Files screen for manual or scripted
        start_print() later."""
        from x2d_bridge import upload_file
        upload_file(self.creds, Path(local_path), remote_name=remote_name)

    def download(self, remote_name: str, local_path: str | Path) -> int:
        """FTPS download of a file from the printer's SD card. Returns
        bytes written. Works while the printer is mid-print (uses the
        fixed TLS context — see x2d_bridge.download_file)."""
        from x2d_bridge import download_file
        return download_file(self.creds, remote_name, Path(local_path))

    def list_files(self, path: str = "") -> list[str]:
        """List the printer's SD card. Empty path returns root entries;
        pass `'sdcard'` for the actual SD mount."""
        from x2d_bridge import list_files
        return list_files(self.creds, path)

    # --- Cloud (lazy) ------------------------------------------------------

    @property
    def cloud(self):
        """CloudClient — loads OAuth session from ~/.x2d/cloud_session.json
        on first access. Raises CloudError if not logged in."""
        if self._cloud is None:
            from cloud_client import CloudClient
            self._cloud = CloudClient.load_or_anonymous()
        return self._cloud

    # --- introspection ----------------------------------------------------

    def __repr__(self) -> str:
        return (f"Printer(ip={self.creds.ip}, "
                f"serial={self.creds.serial[:8] if self.creds.serial else '?'}…, "
                f"mqtt={'open' if self._client is not None else 'closed'})")
