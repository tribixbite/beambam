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
