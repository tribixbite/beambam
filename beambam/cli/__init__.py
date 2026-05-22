"""beambam.cli — CLI handlers being migrated out of x2d_bridge.py.

Phase 5 of `docs/BRIDGE_SPLIT_PLAN.md`. Each sub-package groups related
handlers:

  beambam.cli.cloud   — every `cmd_cloud_*` handler + Printables /
                        MakerWorld search variants
  beambam.cli.control — LAN print-control verbs (pause/resume/stop/gcode
                        /home/level/set-temp/chamber-light/jog/record/
                        timelapse/resolution/fod-check/ams-{load,unload}
                        /reboot)
  beambam.cli.info    — read-only status + utility (status/health/watch
                        /tail/notify/printers/fetch/analyze/fcm-harvest
                        /help)
  beambam.cli.lan     — LAN file-mover (upload/files)
  beambam.cli.daemon  — long-running services (webrtc, ha-publish)

Handlers in this package expose two things to x2d_bridge's `main()`:

  add_subparser(subparsers, root_parser=None) -> None
      Register the subparser(s) the module owns.
  (each `cmd_*` is also importable directly for tests.)

x2d_bridge.py imports `add_subparser` from each module and calls it
inside `main()`. The bridge stays the single argparse-orchestration
point until Phase 5e collapses `main()` into `beambam/cli/__init__.py`
too.
"""
from __future__ import annotations


# Top-level command catalog. argparse only supports ONE add_subparsers
# block so all subcommands appear together in the default help — we
# append this grouped TOC as the epilog so users can scan by topic.
# Group entries are advisory: the actual command set comes from the
# add_parser() calls in x2d_bridge.main(), so a new command shows up in
# the auto help even if we forget to list it here.
_COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("LAN control", [
        ("status",         "One-shot pushall — current bed/nozzle/print state"),
        ("print",          "Upload .gcode.3mf + start print over LAN"),
        ("pause",          "Pause the active print"),
        ("resume",         "Resume a paused print"),
        ("stop",           "Stop the active print"),
        ("reboot",         "M999 clear-error / restart-from-halt (NOT a real power-cycle)"),
        ("gcode",          "Send a raw gcode line to the printer"),
        ("home",           "Home the toolhead (G28)"),
        ("level",          "Run auto bed leveling"),
        ("set-temp",       "Set bed / nozzle / chamber temp"),
        ("led",            "Set chamber LED (on/off/flashing) — was: chamber-light"),
        ("fod-check",      "Toggle firmware foreign-object detection"),
        ("ams-load",       "Load a filament from an AMS slot"),
        ("ams-unload",     "Unload the current filament"),
        ("jog",            "Manual X/Y/Z jog moves"),
    ]),
    ("Slicing", [
        ("slice-print",    "STL → slice → upload + print (one shot)"),
        ("push",           "FTPS push .gcode.3mf — was: upload"),
        ("frame",          "Generate a frame STL (parametric)"),
        ("simulate",       "Estimate print time + filament from a sliced 3mf"),
    ]),
    ("Cloud read", [
        ("cloud-login",    "Authenticate to Bambu cloud (+ --code-only)"),
        ("cloud-status",   "Show current session state"),
        ("cloud-logout",   "Drop the saved session"),
        ("cloud-printers", "List bound printers on this account"),
        ("cloud-state",    "Cloud-MQTT pushall on a printer"),
        ("cloud-history",  "Full server-side print task history"),
        ("cloud-task",     "Full task record incl. signed S3 URLs"),
        ("cloud-messages", "Notification inbox counts + list"),
        ("cloud-tickets",  "Customer-support ticket history"),
        ("cloud-firmware", "FW versions + available updates"),
        ("cloud-filaments","Spool inventory (AMS RFID + manual)"),
        ("cloud-presets",  "Cloud-synced slicer presets"),
        ("cloud-app-config","Global app feature-flag manifest"),
        ("cloud-profile",  "Logged-in user's MakerWorld profile"),
        ("cloud-points",   "Bambu gamification points / progress"),
        ("cloud-unread",   "Unread aftersale + MakerWorld counts"),
    ]),
    ("Cloud control", [
        ("cloud-print",    "Cloud-route start-print"),
        ("cloud-pause",    "Cloud-route pause"),
        ("cloud-resume",   "Cloud-route resume"),
        ("cloud-stop",     "Cloud-route stop"),
        ("cloud-gcode",    "Cloud-route raw gcode"),
        ("cloud-chamber-light", "Cloud-route LED control"),
        ("cloud-publish",  "Cloud-route raw JSON publish"),
        ("cloud-get-access-code", "Resolve LAN access code over cloud MQTT"),
    ]),
    ("MakerWorld", [
        ("cloud-search",   "Full-text search MakerWorld designs"),
        ("cloud-browse",   "Browse by nav (Trending / Foryou / ...)"),
        ("cloud-design",   "Show design details by id"),
        ("cloud-design-remixes", "Remix tree of a design"),
        ("cloud-favorites","List user's favorites lists"),
        ("cloud-liked",    "Designs the user has liked"),
        ("cloud-comments", "Comments + ratings on a design"),
        ("cloud-like",     "Toggle like on a design"),
        ("cloud-feed",     "For-You recommendation feed"),
        ("cloud-search-suggest", "Personalised search-bar terms"),
        ("cloud-pull-design",  "Download a design's .3mf bundle"),
        ("cloud-print-design", "Design id → download → slice → print"),
        ("print-search",   "Interactive: search → pick → slice → print"),
        ("printables-search", "Search Printables.com (anonymous GraphQL)"),
        ("fetch",          "Download from MW/Printables/Thingiverse URL"),
    ]),
    ("Daemon", [
        ("serve",          "HTTP daemon — per-printer multi-tenant"),
        ("boo",            "Single-printer state daemon — was: daemon"),
        ("ha",             "Home Assistant MQTT publisher — was: ha-publish"),
        ("webrtc",         "WebRTC signaling helper"),
    ]),
    ("Diagnostics", [
        ("doctor",         "Check prerequisites + suggest fixes"),
        ("init",           "First-run wizard (LAN discovery)"),
        ("config",         "Edit ~/.x2d/credentials"),
        ("health",         "Latency + reachability probes"),
        ("watch",          "Live-stream state changes"),
        ("tail",           "Stream events (state + HMS + progress) push-driven"),
        ("notify",         "Test push-notification path"),
        ("find",           "Discover printers via SSDP"),
        ("whoami",         "Show current user / region"),
        ("mqtt",           "MQTT broker probe"),
        ("history",        "Local print-history dir"),
        ("queue",          "Print queue management"),
        ("analyze",        "Pre-print 3mf safety check"),
        ("fcm-harvest",    "Pull finish snapshots from rooted Handy"),
    ]),
    ("Media", [
        ("cam",            "Snapshot (bare); subs: start, stop, watch, snap"),
        ("record",         "Record/snapshot from printer cam"),
        ("timelapse",      "Toggle timelapse capture"),
        ("resolution",     "Get/set camera resolution"),
        ("files",          "List SD-card files"),
        ("pull",           "Pull file off printer SD — was: download"),
        ("ams",            "AMS-specific commands"),
        ("slice",          "Slicer-specific commands"),
        ("cloud-fetch",    "Cloud-side file fetch"),
    ]),
]


def _build_epilog() -> str:
    """Format _COMMAND_GROUPS into a `beambam --help` epilog.

    argparse only supports ONE add_subparsers block, so all subcommands
    appear together in the default help. We append this grouped TOC as
    the epilog so users can scan by topic. Group entries are advisory:
    the actual command set comes from add_parser() calls in
    x2d_bridge.main(), so a new command shows up in the auto help even
    if we forget to list it here."""
    lines = ["", "Commands by topic:"]
    for group, cmds in _COMMAND_GROUPS:
        lines.append(f"\n  {group}")
        for name, blurb in cmds:
            lines.append(f"    {name:<24} {blurb}")
    lines.append("")
    lines.append("Run `beambam <command> --help` for full per-command flags.")
    return "\n".join(lines)
