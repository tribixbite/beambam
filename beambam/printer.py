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
        from beambam.config import Creds
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
        from beambam.config import Creds
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
            from beambam.mqtt import X2DClient
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

    def publish(self, payload: dict[str, Any], *, timeout: float = 8.0) -> Any:
        """Publish a raw command and return the printer's reply
        (beambam.state.CommandReply) or None if none arrived in `timeout`
        seconds. Signed exactly like the CLI verbs (beambam.commands):
        print.* with the recovered per-install key. Raises
        beambam.commands.CommandRefused when the printer refuses it."""
        from beambam.commands import send_command
        return send_command(self.mqtt, payload, timeout=timeout, raise_on_refusal=True)

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
                    vibration_cali: bool = False,
                    confirm_timeout: float = 45.0) -> dict[str, Any]:
        """Send the signed start_print MQTT. `ams` accepts an int (single
        slot 0..15) or a list of ints (multi-filament). `use_ams=False`
        with `ams=None` switches to external spool. `local_path`
        auto-derives bed_type+bed_temp from the .gcode.3mf — pass it
        when uploading + printing in one shot. The file must be in the
        printer's cache/ (`upload()` puts it there).

        Returns ``{"verdict": "started"|"refused"|"unconfirmed", "detail"}``
        after waiting up to `confirm_timeout` s for the printer's answer
        (print_job.await_print_start)."""
        from beambam.print_job import start_print
        if use_ams is None:
            use_ams = ams is not None
        if ams is None:
            ams_slot: int | list[int] = 0
        else:
            ams_slot = ams
        from beambam.print_job import BUSY_STATES, await_print_start
        since = self.mqtt.message_seq if hasattr(self.mqtt, "message_seq") else 0
        # If the printer was already busy, a RUNNING report is the previous
        # job's, not proof this one started: then only the reply counts.
        was_busy = False
        if hasattr(self.mqtt, "merged_state"):
            was_busy = ((self.mqtt.merged_state().get("print") or {})
                        .get("gcode_state") in BUSY_STATES)
        start_print(
            self.mqtt, gcode_filename,
            use_ams=use_ams, ams_slot=ams_slot,
            bed_type=bed_type, bed_temp=bed_temp,
            local_path=Path(local_path) if local_path else None,
            timelapse=timelapse, flow_cali=flow_cali,
            bed_levelling=bed_levelling, vibration_cali=vibration_cali,
        )
        if not hasattr(self.mqtt, "wait_for"):           # test / simulate clients
            return {"verdict": "sent", "detail": ""}
        verdict, detail = await_print_start(self.mqtt, since, confirm_timeout,
                                            trust_state=not was_busy)
        return {"verdict": verdict, "detail": detail}

    def pause(self) -> Any:
        from beambam.commands import command_payload
        return self.publish(command_payload("print", "pause", param=""))

    def resume(self) -> Any:
        from beambam.commands import command_payload
        return self.publish(command_payload("print", "resume", param=""))

    def stop(self) -> Any:
        from beambam.commands import command_payload
        return self.publish(command_payload("print", "stop", param=""))

    # --- direct control ---------------------------------------------------

    def gcode(self, line: str) -> None:
        """Send a single raw G-code line. The printer accepts most
        Marlin-ish commands (G/M/T) and a smattering of Bambu-specific
        M codes — see docs/LOCAL_CONTROL_PATHS.md."""
        from beambam.commands import command_payload
        self.publish(command_payload("print", "gcode_line",
                                     param=line + ("\n" if not line.endswith("\n") else "")))

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

    def ams_load(self, slot: int, *, extruder: int | None = None) -> Any:
        """Load filament from AMS global slot (0..15; 254/255 = external
        spools) — into `extruder` (0 right, 1 left) on dual-nozzle printers.
        `print.ams_change_filament` is signed and acked via publish()."""
        from beambam.ams import build_change_filament_payload
        return self.publish(build_change_filament_payload(int(slot), extruder=extruder))

    def ams_unload(self) -> Any:
        """Unload the currently-loaded filament back to its AMS slot.
        Signed and acked via publish() (print.ams_change_filament)."""
        from beambam.ams import build_change_filament_payload
        return self.publish(build_change_filament_payload(None))

    def home(self, *, axes: str = "XYZ") -> None:
        """Home one or more axes. `axes='Z'` for Z-only, 'XY' for the
        bed, 'XYZ' (default) for everything."""
        self.gcode(f"G28 {' '.join(axes)}")

    # --- AMS metadata push ------------------------------------------------

    def set_tray_metadata(
        self,
        slot: int,
        *,
        tray_type: str,
        tray_info_idx: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        tray_color: str | None = None,
        setting_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish `print.ams_filament_setting` for a single AMS tray.

        `slot` is global (0..15: unit*4 + tray). `tray_color` is 8-char
        RRGGBBAA hex (alpha auto-appended `FF` if 6 chars passed). When
        `tray_color` is None the firmware keeps the existing color.

        Returns the payload that was published, for caller logging.
        For dry-run payload inspection (no MQTT connection), use
        `beambam.ams.build_tray_metadata_payload` directly.
        """
        from beambam.ams import build_tray_metadata_payload
        payload = build_tray_metadata_payload(
            slot,
            tray_type=tray_type,
            tray_info_idx=tray_info_idx,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
            tray_color=tray_color,
            setting_id=setting_id,
        )
        self.mqtt.publish(payload)
        return payload

    # --- FTPS (no MQTT) ---------------------------------------------------

    def upload(self, local_path: str | Path,
               remote_name: str | None = None,
               remote_dir: str | None = "cache") -> None:
        """FTPS-implicit-TLS upload to the printer's SD card. No MQTT.
        Defaults to cache/, the only place the X2D/H2D LAN print start
        (`ftp:///cache/<file>`) finds a file; it used to upload to the root,
        so upload() + start_print() failed file-verify (err 84033544).
        Pass remote_dir=None for the root."""
        from beambam.ftps import upload_file
        upload_file(self.creds, Path(local_path), remote_name=remote_name,
                    remote_dir=remote_dir)

    def download(self, remote_name: str, local_path: str | Path) -> int:
        """FTPS download of a file from the printer's SD card. Returns
        bytes written. Works while the printer is mid-print (uses the
        fixed TLS context — see x2d_bridge.download_file)."""
        from beambam.ftps import download_file
        return download_file(self.creds, remote_name, Path(local_path))

    def list_files(self, path: str = "") -> list[str]:
        """List the printer's SD card. Empty path returns root entries;
        pass `'sdcard'` for the actual SD mount."""
        from beambam.ftps import list_files
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
