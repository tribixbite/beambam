"""beambam.cli.control — LAN control-verb cmd_* handlers.

Phase 5a of `docs/BRIDGE_SPLIT_PLAN.md` (incremental). Each handler
shapes an MQTT request payload and delegates the wire publish to
`x2d_bridge._publish_one`, which still owns the LAN connect / sign /
ack-wait state machine. We thunk into x2d_bridge lazily so the bridge
module doesn't appear in this module's import graph at load time
(reciprocal import would deadlock — x2d_bridge re-exports the handlers
defined here).

Currently homed:
  cmd_pause / cmd_resume / cmd_stop   — print state machine verbs
  cmd_gcode                            — arbitrary G-code line
  cmd_home / cmd_level                 — canonical G28 / G29 shortcuts
  cmd_set_temp                         — bed / nozzle / chamber set-point
  cmd_chamber_light                    — chamber LED ledctrl payload

The cloud counterparts of these verbs live in `beambam/cli/cloud.py`
and follow the same lazy-thunk pattern through `_cloud_publish_payload`.

Source-of-truth payload shapes from BambuStudio:
  DeviceManager.cpp:1316 / 1337 / 1347 / 1474 / 1509 / 3645
  DeviceCore/DevLampCtrl.cpp:36
"""
from __future__ import annotations

import argparse
import json
import sys

# Import the MODULES (not the symbols) so the Creds + X2DClient lookups
# inside `_publish_one` go through `beambam.config.Creds` and
# `beambam.mqtt.X2DClient` attribute access at call time. That way
# `monkeypatch.setattr("beambam.mqtt.X2DClient", _Cli)` actually
# reaches the publish path — vs. a `from ... import X2DClient` here
# which would freeze the binding at module load and dodge the patch.
from beambam import config as _config
from beambam import mqtt as _mqtt


def _publish_one(args: argparse.Namespace, payload: dict) -> int:
    """Connect, publish one signed-MQTT payload, disconnect, echo JSON.

    The wire workhorse for ~17 LAN-control verbs (pause / resume /
    stop / gcode / home / level / set-temp / chamber-light / reboot /
    jog / record / timelapse / resolution / fod-check / ams-load /
    ams-unload).

    Tests can monkeypatch either `beambam.mqtt.X2DClient` (canonical)
    or `beambam.config.Creds.resolve` (for credential override) —
    both reach the publish path because we resolve the attributes at
    call time, not at module-load."""
    creds = _config.Creds.resolve(args)
    cli = _mqtt.X2DClient(creds)
    cli.connect()
    try:
        cli.publish(payload)
    finally:
        cli.disconnect()
    print(json.dumps(payload, indent=2))
    return 0


def _signing_key_path():
    """Path to the recovered printer-control RSA key (overridable for tests)."""
    from pathlib import Path
    return Path.home() / ".x2d" / "printer_sign_key.pem"


def _try_cloud_signed(args: argparse.Namespace, payload: dict):
    """If a recovered RSA signing key + a cloud session exist, publish `payload`
    as a SIGNED command over the Bambu cloud broker — X-series firmware rejects
    unsigned LAN `print.*` (`mqtt message verify failed`). Returns 0/1 when used,
    or None to fall back to the LAN path. Forced off with X2D_FORCE_LAN=1 or
    `--lan`, and skipped automatically when key/session/serial are absent."""
    import os
    if getattr(args, "lan", False) or os.environ.get("X2D_FORCE_LAN"):
        return None
    if not _signing_key_path().is_file():
        return None
    try:
        import cloud_client
        from beambam.cloud_control import CloudPrinter
    except Exception:
        return None
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return None
    creds = _config.Creds.resolve(args)
    serial = creds.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        return None
    family = next(iter(payload))
    command = {k: v for k, v in payload[family].items()
               if k not in ("sequence_id", "timestamp")}
    try:
        cp = CloudPrinter.from_config(cli, serial, key_path=_signing_key_path())
        ack = cp.command(family, command)
    except Exception as e:                                # noqa: BLE001
        print(f"[cloud-signed] failed ({e}); falling back to LAN", file=sys.stderr)
        return None
    print(json.dumps({"transport": "cloud-signed",
                      "command": f"{family}.{command.get('command')}",
                      "ack": ack}, indent=2))
    return 0


def _publish(args: argparse.Namespace, payload: dict) -> int:
    """Publish one control payload. Prefers the cloud-signed broker path when a
    recovered RSA key + cloud session are present (LAN print.* is signature-
    blocked on X-series firmware); otherwise the LAN `_publish_one` path."""
    rc = _try_cloud_signed(args, payload)
    if rc is not None:
        return rc
    return _publish_one(args, payload)


def cmd_pause(args: argparse.Namespace) -> int:
    # MachineObject::command_task_pause — DeviceManager.cpp:1337
    from beambam.cli._helpers import _print_cmd
    return _publish(args, _print_cmd("pause", param=""))


def cmd_resume(args: argparse.Namespace) -> int:
    # MachineObject::command_task_resume — DeviceManager.cpp:1347
    from beambam.cli._helpers import _print_cmd
    return _publish(args, _print_cmd("resume", param=""))


def cmd_stop(args: argparse.Namespace) -> int:
    # MachineObject::command_task_abort — DeviceManager.cpp:1316
    from beambam.cli._helpers import _print_cmd
    return _publish(args, _print_cmd("stop", param=""))


def cmd_skip(args: argparse.Namespace) -> int:
    """Skip one or more objects on the RUNNING print (`print.skip_objects`).
    OBJ_IDS are the per-plate object ids (from the sliced .gcode.3mf). Routes
    cloud-signed on X-series firmware; `--lan` forces the LAN publish."""
    from beambam.cli._helpers import _print_cmd
    obj_list = [int(x) for x in args.obj_ids]
    return _publish(args, _print_cmd("skip_objects", obj_list=obj_list))


def cmd_gcode(args: argparse.Namespace) -> int:
    # MachineObject::publish_gcode — DeviceManager.cpp:3645
    from beambam.cli._helpers import _print_cmd
    gcode = args.gcode if args.gcode.endswith("\n") else args.gcode + "\n"
    return _publish(args, _print_cmd("gcode_line", param=gcode))


def cmd_home(args: argparse.Namespace) -> int:
    from beambam.cli._helpers import _print_cmd
    return _publish(args, _print_cmd("gcode_line", param="G28\n"))


def cmd_level(args: argparse.Namespace) -> int:
    """G29 = auto bed leveling on most G-code dialects; the X-series
    firmwares accept it as the canonical "level the bed now" command."""
    from beambam.cli._helpers import _print_cmd
    return _publish(args, _print_cmd("gcode_line", param="G29\n"))


def cmd_set_temp(args: argparse.Namespace) -> int:
    from beambam.cli._helpers import _print_cmd
    if args.target == "bed":
        # MachineObject::command_set_bed (mqtt path) — DeviceManager.cpp:1474
        return _publish(args, _print_cmd("set_bed_temp",
                                          temp=int(args.value)))
    elif args.target == "nozzle":
        # MachineObject::command_set_nozzle_new — DeviceManager.cpp:1509
        return _publish(args, _print_cmd(
            "set_nozzle_temp",
            extruder_index=int(args.idx),
            target_temp=int(args.value),
        ))
    elif args.target == "chamber":
        # No mqtt verb in the source — fall back to gcode M141.
        return _publish(args, _print_cmd(
            "gcode_line", param=f"M141 S{int(args.value)}\n"
        ))
    else:
        sys.exit(f"unknown set-temp target: {args.target}")


def cmd_chamber_light(args: argparse.Namespace) -> int:
    # DevLamp::command_set_chamber_light — DeviceCore/DevLampCtrl.cpp:36
    from beambam.cli._helpers import _system_cmd
    state = args.state.lower()
    if state not in ("on", "off", "flashing"):
        sys.exit(f"chamber-light state must be on/off/flashing, "
                  f"got: {state}")
    payload = _system_cmd(
        "ledctrl",
        led_node="chamber_light",
        led_mode=state,
        led_on_time=int(args.on_time),
        led_off_time=int(args.off_time),
        loop_times=int(args.loops),
        interval_time=int(args.interval),
    )
    return _publish(args, payload)


# Constant + helper used by cmd_reboot and its unit tests so the wire
# payload is reachable without instantiating an argparse.Namespace.
_REBOOT_GCODE = "M999"


def _reboot_payload() -> dict:
    """The wire payload `beambam reboot --confirm` sends.

    M999 is the Marlin "restart from emergency stop" gcode. On Bambu
    firmware it clears the printer's halt/error flags and re-arms the
    motion system; it does NOT power-cycle the SoC, the MQTT broker,
    or the network stack. There is no documented MQTT verb for a true
    soft reboot on current X-series firmware — the only paths are the
    physical power button or an OTA firmware update."""
    from beambam.cli._helpers import _print_cmd
    return _print_cmd("gcode_line", param=f"{_REBOOT_GCODE}\n")


def cmd_reboot(args: argparse.Namespace) -> int:
    """Send `M999` to the printer (gcode error-clear).

    Defaults to dry-run because the wording "reboot" is broader than
    what the firmware actually exposes: M999 clears the halt/error
    flag set, but it does NOT power-cycle the SoC, restart MQTT, or
    flush the network plugin. Pass --confirm to actually send."""
    import json
    payload = _reboot_payload()
    if not args.confirm:
        print("[reboot] DRY-RUN — pass --confirm to actually send.",
              file=sys.stderr)
        print(f"[reboot] would publish: {json.dumps(payload)}",
              file=sys.stderr)
        print("[reboot] note: M999 clears the printer's "
              "emergency-stop / error flags. It does NOT power-cycle "
              "the printer; the MQTT broker, network stack, AMS state, "
              "and chamber heater all keep their current values. For "
              "a real power-cycle, use the physical power button on "
              "the back of the printer or wait for the next OTA "
              "firmware update.", file=sys.stderr)
        return 0
    return _publish(args, payload)


def cmd_jog(args: argparse.Namespace) -> int:
    """Relative move via standard G91/G1/G90 sequence — works on every
    firmware that accepts arbitrary gcode."""
    from beambam.cli._helpers import _print_cmd
    axis = args.axis.upper()
    if axis not in ("X", "Y", "Z", "E"):
        sys.exit(f"jog axis must be one of X/Y/Z/E, got: {args.axis}")
    feed = int(args.feed)
    distance = float(args.distance)
    gcode = (
        "G91\n"
        f"G1 {axis}{distance:g} F{feed}\n"
        "G90\n"
    )
    return _publish(args, _print_cmd("gcode_line", param=gcode))


# IPCAM verbs (BambuStudio DeviceManager.cpp:2027–2080). Plain MQTT
# publish to device/<sn>/request, no Bambu Connect signing.


def cmd_record(args: argparse.Namespace) -> int:
    """Toggle the chamber camera's SD-card recording. Mirrors BS
    DeviceManager::command_ipcam_record (DeviceManager.cpp:2027)."""
    from beambam.cli._helpers import _camera_cmd
    state = args.state.lower()
    if state not in ("on", "off"):
        sys.exit(f"record state must be on/off, got: {state}")
    payload = _camera_cmd("ipcam_record_set",
                          control="enable" if state == "on" else "disable")
    return _publish(args, payload)


def cmd_timelapse(args: argparse.Namespace) -> int:
    """Toggle chamber-camera timelapse capture. BS DeviceManager
    ::command_ipcam_timelapse (DeviceManager.cpp:2038)."""
    from beambam.cli._helpers import _camera_cmd
    state = args.state.lower()
    if state not in ("on", "off"):
        sys.exit(f"timelapse state must be on/off, got: {state}")
    payload = _camera_cmd("ipcam_timelapse",
                          control="enable" if state == "on" else "disable")
    return _publish(args, payload)


def cmd_resolution(args: argparse.Namespace) -> int:
    """Set chamber-camera resolution. BS DeviceManager
    ::command_ipcam_resolution_set (DeviceManager.cpp:2049)."""
    from beambam.cli._helpers import _camera_cmd
    res = args.resolution.lower()
    if res not in ("low", "medium", "high", "full"):
        sys.exit(f"resolution must be low/medium/high/full, got: {res}")
    payload = _camera_cmd("ipcam_resolution_set", resolution=res)
    return _publish(args, payload)


def cmd_fod_check(args: argparse.Namespace) -> int:
    """Toggle the X2D's Foreign Object Detection on the build plate.

    Mechanism: BambuStudio's xcam_control_set MQTT publish with
    module_name=fod_check (DeviceCore/DevPrintOptions.cpp:544). When on,
    the firmware runs Stage 73 (build-plate alignment) → Stage 74
    (heatbed surface foreign object detection) → Stage 75 (heatbed
    underside detection) before every print start. If junk is detected
    on the plate the firmware halts the print start (no leftover from
    the previous job is allowed onto the new run).

    The full stage table is at BambuStudio/src/slic3r/GUI/DeviceManager.cpp:86.
    Print-options feature flag: support_build_plate_marker_detect=true with
    type 2 on X2D / N7 / H2D (resources/printers/N7.json:44)."""
    from beambam.cli._helpers import _xcam_cmd
    state = args.state.lower()
    if state not in ("on", "off"):
        sys.exit(f"fod-check state must be on/off, got: {state}")
    payload = _xcam_cmd("fod_check", state == "on")
    return _publish(args, payload)


def cmd_ams_unload(args: argparse.Namespace) -> int:
    """MachineObject::command_ams_change_filament with !load —
    DeviceManager.cpp:1537. `target=255` is the unload sentinel."""
    from beambam.cli._helpers import _print_cmd
    payload = _print_cmd(
        "ams_change_filament",
        curr_temp=int(args.curr_temp),
        tar_temp=int(args.tar_temp),
        ams_id=int(args.ams),
        target=255,
        slot_id=255,
    )
    return _publish(args, payload)


def cmd_ams_load(args: argparse.Namespace) -> int:
    """MachineObject::command_ams_change_filament with load —
    DeviceManager.cpp:1537. `target` is the global tray id
    (ams_id*4 + slot_id), with a special case for ams_id=0 / slot=0
    where the firmware expects bare ams_id, not 0."""
    from beambam.cli._helpers import _print_cmd
    ams_id = int(args.ams)
    slot_id = int(args.slot)
    tray_id = ams_id * 4 + slot_id
    target = ams_id if tray_id == 0 else tray_id
    payload = _print_cmd(
        "ams_change_filament",
        curr_temp=int(args.curr_temp),
        tar_temp=int(args.tar_temp),
        ams_id=ams_id,
        target=target,
        slot_id=slot_id,
    )
    return _publish(args, payload)


# --- start (smart) + key (recover signer) --------------------------------

def _cloud_printer_state(args: argparse.Namespace) -> dict:
    """Best-effort current printer state via a cloud `pushall` (read-only,
    unsigned). Returns the full merged `print` dict (incl. `ams`), or {} if no
    cloud session / serial. Delegates to `cloud_pull_state`, which merges the
    multi-message pushall — the full snapshot (with AMS) rides in a later
    message than `gcode_state`, so the old first-message read missed it."""
    import os
    try:
        import cloud_client
        from beambam.cli.cloud import cloud_pull_state
    except Exception:                                      # noqa: BLE001
        return {}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return {}
    serial = _config.Creds.resolve(args).serial or os.environ.get("X2D_SERIAL")
    if not serial:
        return {}
    try:
        return cloud_pull_state(cli, serial)
    except Exception:                                      # noqa: BLE001
        return {}


def _start_next_in_queue(args: argparse.Namespace):
    """Print-next-in-queue for `cmd_start`: if ~/.x2d/queue.json has a pending
    job for the default printer, mark it running and start it as a cloud print
    (upload its .gcode.3mf to Bambu OSS + signed print.project_file, via
    `cmd_cloud_print`). Returns the dispatch rc (0/1) when a job was started,
    or None when the queue is empty (so the caller falls through to
    print-again)."""
    import os
    from pathlib import Path
    try:
        from runtime.queue.manager import QueueManager
    except Exception as e:                                # noqa: BLE001
        print(f"[start] queue subsystem unavailable: {e}", file=sys.stderr)
        return None
    qm = QueueManager(dispatch_cb=lambda _j: True)
    printer = getattr(args, "printer", "") or ""
    job = qm.start_next(printer)
    if job is None:
        return None
    src = Path(job.gcode)
    if not src.is_file():
        qm.mark_failed(job.id, f"file missing: {src}")
        print(f"[start] queued job {job.id[:8]} .3mf missing: {src}",
              file=sys.stderr)
        return 1
    print(f"[start] printer idle → print next in queue: {job.label} "
          f"({src.name})", file=sys.stderr)
    # DRY: a queued job is just a cloud-print of its .3mf. Reuse cmd_cloud_print
    # (upload→compose→sign→publish). Resolve the serial the same way the signed
    # control path does so the cloud topic matches.
    from beambam.cli.cloud import cmd_cloud_print
    serial = _config.Creds.resolve(args).serial or os.environ.get("X2D_SERIAL")
    ns = argparse.Namespace(
        file=str(src), serial=serial, slot=int(job.slot), no_ams=False,
        plate=1, bed_type="textured_plate", bed_temp=65, no_level=False,
        flow_cali=False, vibration_cali=False, timelapse=False,
        dry_run=False, timeout=30.0)
    try:
        rc = cmd_cloud_print(ns)
    except Exception as e:                                # noqa: BLE001
        qm.mark_failed(job.id, str(e))
        print(f"[start] queue dispatch failed: {e}", file=sys.stderr)
        return 1
    if rc != 0:
        qm.mark_failed(job.id, f"cloud-print rc={rc}")
    else:
        # Started → drop it from the queue. Leaving it "running" would be
        # demoted back to "pending" on the next QueueManager load (crash
        # recovery), causing a re-dispatch / double-print the next time the
        # printer is idle. There's no daemon here to reconcile completion.
        qm.remove(job.id)
    return rc


def cmd_start(args: argparse.Namespace) -> int:
    """Smart start, ranked: **resume** a paused print → **print next in queue**
    (~/.x2d/queue.json, via `beambam queue add`) → **print again** (re-run the
    last task from ~/.x2d/printer_project_file.json). Use `--lan` to force the
    LAN publish path for the resume/print-again commands."""
    from beambam.cli._helpers import _print_cmd
    state = _cloud_printer_state(args)
    gs = (state.get("gcode_state") or "").upper()
    if gs in ("PAUSE", "PAUSED"):
        print(f"[start] printer is {gs} → resume", file=sys.stderr)
        return _publish(args, _print_cmd("resume", param=""))
    if gs in ("RUNNING", "PREPARE", "SLICING"):
        print(f"[start] printer already {gs}; nothing to start", file=sys.stderr)
        return 0
    # idle / finished / failed → (2) print next in queue, else (3) print again
    rc = _start_next_in_queue(args)
    if rc is not None:
        return rc
    # (3) print again (re-run the last task)
    from pathlib import Path
    pf = Path.home() / ".x2d" / "printer_project_file.json"
    if not pf.is_file():
        last = Path.home() / ".x2d" / "printer_last_task.json"
        hint = (f"        Last print's params are recorded at {last} "
                f"(run `beambam capture-params`).\n" if last.is_file()
                else f"        Run `beambam capture-params` to record the last "
                     f"print's params first.\n")
        # TODO: reconstruct a cloud-slice reprint project_file from
        # printer_last_task.json (context.prefix/configs URLs) — needs the
        # reprint body verified against a real printer before enabling.
        print(f"[start] printer is {gs or 'idle'}: no paused job to resume, no "
              f"queued job, and print-again needs a captured project_file at "
              f"{pf}.\n{hint}"
              f"        (Or queue a .gcode.3mf: `beambam queue add FILE`.)",
              file=sys.stderr)
        return 2
    try:
        body = json.loads(pf.read_text())
    except Exception as e:                                # noqa: BLE001
        print(f"[start] bad {pf}: {e}", file=sys.stderr)
        return 1
    print("[start] printer idle → print again (re-run last task)", file=sys.stderr)
    return _publish(args, {"print": body})


def cmd_key(args: argparse.Namespace) -> int:
    """Recover the printer-control RSA signing key from the RUNNING Handy app's
    Dart heap (no Frida / no hooking) and save it to ~/.x2d/ for signed cloud
    control. Needs: adb access to the phone (`--adb ip:port`), Handy running with
    the Devices tab opened once (so the key is loaded), and the app cert
    (`--cert`, default ~/.x2d/printer_app_cert.pem) for the modulus. Also writes
    cert_id. This is the optional setup step that unlocks `pause/resume/stop/…`."""
    import os
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime" / "handy_extract"))
    try:
        import extract_signing_key as esk
    except Exception as e:                                # noqa: BLE001
        print(f"[key] cannot load extractor: {e}", file=sys.stderr)
        return 1
    adb = getattr(args, "adb", None) or os.environ.get("X2D_ADB")
    if not adb:
        print("[key] --adb <ip:port> (or X2D_ADB) required: the phone's adb "
              "serial", file=sys.stderr)
        return 1
    cert = getattr(args, "cert", None) or str(Path.home() / ".x2d" / "printer_app_cert.pem")
    if not Path(cert).is_file():
        print(f"[key] app cert not found at {cert} (capture security."
              f"app_cert_install, or pass --cert)", file=sys.stderr)
        return 1
    out = str(Path.home() / ".x2d" / "printer_sign_key.pem")
    rc = os.system(
        f"cd {Path.home()} && python3 {esk.__file__} --serial {adb} "
        f"--cert {cert} --out {out}")
    if rc != 0 or not Path(out).is_file():
        print("[key] extraction failed (is Handy running + Devices tab opened so "
              "the key is in the heap?)", file=sys.stderr)
        return 1
    # write cert_id = <app-cert serial hex> + CN=GLOF<printerSerial>.bambulab.com
    try:
        from cryptography import x509
        from beambam.cloud_control import mqtt_sign
        serial_hex = format(
            x509.load_pem_x509_certificate(Path(cert).read_bytes()).serial_number, "x")
        printer_serial = _config.Creds.resolve(args).serial or os.environ.get("X2D_SERIAL", "")
        cid = mqtt_sign.make_cert_id(serial_hex, printer_serial.replace("GLOF", ""))
        (Path.home() / ".x2d" / "printer_cert_id.txt").write_text(cid)
        print(f"[key] OK → {out}\n[key] cert_id → ~/.x2d/printer_cert_id.txt")
    except Exception as e:                                # noqa: BLE001
        print(f"[key] key saved, but cert_id write failed: {e}", file=sys.stderr)
    return 0


def cmd_device_cert(args: argparse.Namespace) -> int:
    """Fetch + cache the printer's RSA device cert — required for X2D/H2D LAN
    start-print (the project_file `url_enc` encrypts the file URL to this cert's
    public key). Connects to the LAN broker and sends the unsigned
    `security.app_cert_install` (gated only by the access code), then caches the
    leaf at ~/.x2d/printer_device_cert.pem. One-time setup, like `beambam key`."""
    from beambam.device_cert import fetch_device_cert
    creds = _config.Creds.resolve(args)
    cli = _mqtt.X2DClient(creds)
    cli.connect()
    try:
        out = fetch_device_cert(cli, cn=getattr(args, "cn", None))
    except Exception as e:                                # noqa: BLE001
        print(f"[device-cert] failed: {e}", file=sys.stderr)
        return 1
    finally:
        cli.disconnect()
    print(f"[device-cert] cached printer device cert → {out}")
    return 0
