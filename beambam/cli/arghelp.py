"""beambam.cli.arghelp — help text for arguments registered without `help=`.

Many subparsers share the same flags (`--json`, `--limit`, `design_id`,
the start-print calibration toggles, the bambu-studio pass-throughs …).
Rather than repeat identical strings at ~130 `add_argument` sites, the
text lives here once and `fill_missing_help()` assigns it to every action
whose `help` is still None after the parser tree is built.

Lookup order for an action:
  1. SPECIFIC[(command path, dest)]  — one-off meanings ("cloud-task", "task_id")
  2. GENERIC[dest]                   — same meaning everywhere it appears

An explicit `help=` at the call site always wins; this module never
overwrites it. `tests/test_cli_manhelp.py` asserts that after filling,
no visible argument anywhere in the tree is left without help, so a new
flag without text fails CI instead of shipping a blank line.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterator

# Flags whose meaning is identical in every command that defines them.
GENERIC: dict[str, str] = {
    "json": "Print machine-readable JSON instead of the human-readable summary.",
    "json_out": "Print machine-readable JSON instead of the human-readable summary.",
    "limit": "Maximum number of results to return.",
    "offset": "Number of results to skip, for paging through long lists.",
    "design_id": "Numeric MakerWorld design id (the number in the design's URL, "
                 "makerworld.com/…/models/<id>).",
    "verbose": "Print extra progress / diagnostic output.",
    "no_sign": "Print the inner unsigned payload instead of the RSA-signed "
               "envelope the printer would receive.",
    "no_color": "Disable ANSI colour in the output.",
    # Start-print options (print, cloud-print, cloud-start-print, simulate …).
    "remote": "File name to use on the printer's storage (default: the local "
              "file's basename).",
    "no_ams": "Print from the external spool holder instead of an AMS slot.",
    "no_bed_level": "Skip automatic bed levelling before the print starts.",
    "flow_cali": "Run flow-dynamics (pressure-advance) calibration before printing.",
    "vib_cali": "Run vibration (resonance) compensation calibration before printing.",
    "vibration_cali": "Run vibration (resonance) compensation calibration before printing.",
    "timelapse": "Record a timelapse of the print on the printer's camera.",
    # Model scaling / placement (print-search, cloud-print-design → slice-print).
    "scale": "Uniform scale factor applied to the model (1.0 = original size).",
    "scale_pct": "Scale as a percentage (75 = 0.75×); easier to type than --scale.",
    "mm": "Auto-scale the model so its Z height equals this many millimetres.",
    "copies": "How many copies of the model to tile on the plate.",
    "color": "Primary filament colour as #RRGGBB.",
    "slot": "AMS global slot to print from: AMS_index*4 + tray (0..15).",
    "instance_index": "Which print profile (instance) of the design to use; "
                      "0 = the design's default.",
    # bambu-studio --slice pass-throughs (3MF re-slicing).
    "load_filaments": "Semicolon-joined filament JSON paths. Mirrors bambu-studio "
                      "--load-filaments. (3MF input only)",
    "load_settings": "Semicolon-joined process + printer JSON paths. Mirrors "
                     "bambu-studio --load-settings. (3MF input only)",
    "load_filament_ids": "Comma-joined 1-based filament ids per object (e.g. "
                         "'1,2,3,1'). Mirrors bambu-studio --load-filament-ids. "
                         "(3MF input only)",
    "load_defaultfila": "Use the first filament for objects with no explicit "
                        "filament id. (3MF input only)",
    "uptodate": "Update the configs embedded in the 3MF to the latest from "
                "--load-filaments / --load-settings. (3MF input only)",
    "allow_newer_3mf": "Allow re-slicing a 3MF saved by a newer slicer version. "
                       "(3MF input only)",
    "allow_multicolor_oneplate": "Allow multiple colours on one plate during "
                                 "auto-arrange. (3MF input only)",
    # Per-command printer selection (same meaning as the global options).
    "printer": "Printer name — a [printer:NAME] section in ~/.x2d/credentials.",
    "ip": "Printer LAN IP (overrides the credentials file).",
    "code": "Printer 8-character LAN access code (overrides the credentials file).",
    "serial": "Printer serial number (overrides the credentials file).",
    # Chamber LED pattern (led, cloud-chamber-light).
    "on_time": "Flashing mode: milliseconds the LED stays on per blink.",
    "off_time": "Flashing mode: milliseconds the LED stays off per blink.",
    "loops": "Flashing mode: number of blink cycles (0 = repeat forever).",
    "interval": "Flashing mode: pause in milliseconds between blink cycles.",
    # AMS load/unload heat soak.
    "curr_temp": "Current nozzle temperature in °C used for the heat soak.",
    "tar_temp": "Nozzle temperature in °C to reach before the filament moves.",
}

# Arguments whose meaning depends on the command. Key: (space-joined
# command path as typed after `beambam`, dest).
SPECIFIC: dict[tuple[str, str], str] = {
    ("status", "timeout"): "Seconds to wait for the printer's state report.",
    ("print", "file"): "Sliced .gcode.3mf to upload and print.",
    ("set-temp", "target"): "Which heater to set: bed, nozzle or chamber.",
    ("led", "state"): "on, off, or flashing (pattern set by the --on-time / "
                      "--off-time / --loops / --interval options).",
    ("fod-check", "state"): "on enables the pre-print plate check; off disables it.",
    ("cloud-print", "bed_temp"): "Bed temperature in °C for the first layer.",
    ("cloud-task", "task_id"): "Numeric cloud task id (from `beambam cloud-history`).",
    ("printables-search", "query"): "Search terms, e.g. \"cable clip\" (quote "
                                    "multi-word queries).",
    ("simulate print", "bed_type"): "MQTT bed_type string: cool_plate, eng_plate, "
                                    "hot_plate, textured_plate or supertack_plate.",
    ("simulate print", "bed_temp"): "Bed temperature in °C written into the payload.",
    ("simulate light", "light_on"): "--on builds the light-on payload, --off the "
                                    "light-off payload (one is required).",
    ("ams load", "slot"): "AMS global slot to load: unit*4 + tray (0..15).",
    ("cam", "url"): "JPEG snapshot URL to fetch (default: the local camera daemon; "
                    "or $BEAMBAM_CAM_URL).",
    ("cam snap", "url"): "JPEG snapshot URL to fetch (default: the local camera "
                         "daemon; or $BEAMBAM_CAM_URL).",
    ("cam start", "bind"): "HTTP bind address for the MJPEG proxy.",
    ("cam start", "port"): "Printer's RTSPS port.",
    ("cam start", "skip_check"): "Skip the ipcam.rtsp_url pre-flight (useful when "
                                 "MQTT can't reach the printer but RTSP is open).",
    ("cam start", "proto"): "rtsp = RTSPS:322 via ffmpeg; local = Bambu's "
                            "proprietary LVL_Local TLS:6000 stream.",
    ("cam start", "auth_token"): "Bearer token required when --bind is not "
                                 "loopback (default $X2D_AUTH_TOKEN).",
    ("cam start", "idle_timeout"): "Seconds with no viewer before the upstream "
                                   "ffmpeg pump is stopped to save battery.",
    ("config show", "reveal"): "Show the access code in clear instead of masked.",
    ("config add", "name"): "Section name; creates [printer:NAME] (use with "
                            "`--printer NAME` later).",
    ("config remove", "name"): "Name of the [printer:NAME] section to delete.",
    ("config rename", "old"): "Current section name.",
    ("config rename", "new"): "New section name.",
    ("mqtt pub", "qos"): "MQTT quality of service: 0 = at most once, 1 = at least "
                         "once, 2 = exactly once.",
    ("queue remove", "job_id"): "Job id, or any unique prefix of it (see "
                                "`beambam queue list`).",
    ("queue cancel", "job_id"): "Job id, or any unique prefix of it (see "
                                "`beambam queue list`).",
}


def iter_commands(parser: argparse.ArgumentParser,
                  path: tuple[str, ...] = ()
                  ) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Yield (path, subparser) for every command in the tree, depth-first.

    Aliases map to the same parser object; each parser is yielded once,
    under its canonical (first registered) name."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        seen: set[int] = set()
        for name, sub in action.choices.items():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            yield path + (name,), sub
            yield from iter_commands(sub, path + (name,))


def fill_missing_help(root: argparse.ArgumentParser) -> None:
    """Assign help text to every action in the tree that has none."""
    for path, sub in iter_commands(root):
        key = " ".join(path)
        for action in sub._actions:
            if action.help is not None or isinstance(action, argparse._SubParsersAction):
                continue
            text = SPECIFIC.get((key, action.dest)) or GENERIC.get(action.dest)
            if text is not None:
                action.help = text
