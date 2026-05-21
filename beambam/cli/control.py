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
import sys


def _publish(args: argparse.Namespace, payload: dict) -> int:
    """Lazy proxy to x2d_bridge._publish_one (LAN connect + sign + ack-wait
    state machine still lives in the monolith)."""
    from x2d_bridge import _publish_one
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


def cmd_reboot(args: argparse.Namespace) -> int:
    """Send `M999` to the printer (gcode error-clear).

    Defaults to dry-run because the wording "reboot" is broader than
    what the firmware actually exposes: M999 clears the halt/error
    flag set, but it does NOT power-cycle the SoC, restart MQTT, or
    flush the network plugin. Pass --confirm to actually send. The
    `_reboot_payload` helper + `_REBOOT_GCODE` constant stay in
    x2d_bridge.py so existing test imports keep working."""
    import json
    from x2d_bridge import _reboot_payload
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
