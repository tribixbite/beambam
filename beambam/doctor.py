"""beambam.doctor — comprehensive printer health diagnostic.

Sister of `beambam health` (which checks TCP/MQTT/FTPS reachability
+ basic state). `doctor` adds:

  * AMS humidity warnings (>3 of 4 = filament needs drying)
  * Active HMS error scanning (machine-readable codes + friendly text)
  * Filament-vs-print compatibility flags
  * Hotend / bed thermistor sanity (not unplugged / shorted)
  * Wifi signal strength (RSSI thresholds)
  * Camera + RTSP state for the cam subcommand
  * Recent print error patterns

Output: colored pass/warn/fail report grouped by category. Returns
exit code 0 (all green), 1 (warnings present), 2 (failures present).

Use `beambam health` for raw connectivity diagnostics; use `doctor`
for a richer "is everything OK?" overview before unattended prints.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Literal


Severity = Literal["pass", "warn", "fail", "info"]


@dataclass
class Check:
    category: str
    name: str
    severity: Severity
    detail: str = ""


_GLYPHS = {
    "pass": "\033[32m✓\033[0m",
    "warn": "\033[33m⚠\033[0m",
    "fail": "\033[31m✗\033[0m",
    "info": "\033[36m·\033[0m",
}


# ----- HMS error catalog --------------------------------------------------


# Common HMS codes seen on Bambu printers. The first 4 hex chars are the
# module, last 4 are the error within that module. This is a hand-curated
# subset of the ~400-entry catalog — full mapping would belong in a
# separate data file.
HMS_DESCRIPTIONS: dict[str, str] = {
    "0C00_0100_0001_0001": "Heatbed overload — heater drawing too much current",
    "0500_0100_0003_0001": "Toolhead fan stalled or disconnected",
    "0700_2000_0002_0006": "Nozzle clogged or filament not feeding",
    "0700_C001_0000_0000": "Filament runout sensor triggered",
    "1200_0100_0001_0001": "Camera disconnected or unresponsive",
    "0500_0300_0002_0002": "Chamber fan stalled",
    "1000_C001_0000_0000": "Bed temperature higher than filament max — risk of clog",
    "0300_0100_0001_0009": "Z-axis homing failed — check limit switch",
    "0300_0200_0002_0001": "X-axis stall detected — belt tension or bearing",
}


def decode_hms(code: str) -> str:
    return HMS_DESCRIPTIONS.get(code, f"unknown HMS code (look up: {code})")


# ----- check producers ----------------------------------------------------


def check_ams_humidity(state: dict[str, Any]) -> list[Check]:
    out = []
    units = state.get("print", {}).get("ams", {}).get("ams", []) or []
    for unit in units:
        uid = unit.get("id", "?")
        humidity = unit.get("humidity", "0")
        try:
            level = int(humidity)
        except (TypeError, ValueError):
            level = 0
        if level >= 4:
            out.append(Check("AMS", f"unit {uid} humidity",
                             "fail",
                             f"level {level}/4 — very wet; dry filament "
                             f"before printing"))
        elif level == 3:
            out.append(Check("AMS", f"unit {uid} humidity",
                             "warn",
                             f"level {level}/4 — getting damp; consider drying"))
        else:
            out.append(Check("AMS", f"unit {uid} humidity",
                             "pass", f"level {level}/4"))
    return out


def check_hms_errors(state: dict[str, Any]) -> list[Check]:
    hms = state.get("print", {}).get("hms", []) or []
    if not hms:
        return [Check("Errors", "active HMS codes", "pass",
                      "no active errors")]
    out = []
    for h in hms:
        # HMS code is split into 4 16-bit hex chunks: attr/code/...
        # Different firmware revs use different keys; try the common ones.
        code_str = "_".join(str(h.get(k, "")) for k in ("attr", "code"))
        if not code_str.strip("_"):
            # Newer firmware: HMS dict has 'a','b','c','d' fields
            code_str = "_".join(f"{int(h.get(k, 0)):04X}"
                                 for k in ("a", "b", "c", "d"))
        sev: Severity = "fail" if h.get("p") in (1, 4) else "warn"
        out.append(Check("Errors", f"HMS {code_str}", sev,
                         decode_hms(code_str)))
    return out


def check_thermistor_sanity(state: dict[str, Any]) -> list[Check]:
    out = []
    p = state.get("print", {})
    bed = p.get("bed_temper")
    if isinstance(bed, (int, float)):
        if bed < -10 or bed > 200:
            out.append(Check("Sensors", "bed thermistor", "fail",
                              f"reading {bed:.1f}°C — likely "
                              f"disconnected or shorted"))
        else:
            out.append(Check("Sensors", "bed thermistor", "pass",
                              f"{bed:.1f}°C"))
    nz = p.get("nozzle_temper")
    if isinstance(nz, (int, float)):
        if nz < -10 or nz > 350:
            out.append(Check("Sensors", "nozzle thermistor", "fail",
                              f"reading {nz:.1f}°C — likely "
                              f"disconnected or shorted"))
        else:
            out.append(Check("Sensors", "nozzle thermistor", "pass",
                              f"{nz:.1f}°C"))
    return out


def check_wifi_signal(state: dict[str, Any]) -> list[Check]:
    sig = state.get("print", {}).get("wifi_signal", "")
    if not sig:
        return [Check("Network", "wifi signal", "info", "not reported")]
    # Bambu reports as "-XXdBm". Threshold: <-70 weak, <-80 unreliable.
    m = sig.rstrip("dBm")
    try:
        rssi = int(m)
    except ValueError:
        return [Check("Network", "wifi signal", "info", sig)]
    if rssi <= -80:
        return [Check("Network", "wifi signal", "fail",
                       f"{rssi}dBm — unreliable; expect MQTT drops")]
    if rssi <= -70:
        return [Check("Network", "wifi signal", "warn",
                       f"{rssi}dBm — weak; may drop under load")]
    return [Check("Network", "wifi signal", "pass", f"{rssi}dBm")]


def check_camera(state: dict[str, Any]) -> list[Check]:
    cam = state.get("print", {}).get("ipcam", {}) or {}
    rec = cam.get("ipcam_record", "")
    res = cam.get("resolution", "")
    parts = []
    if res:
        parts.append(res)
    if rec:
        parts.append(f"record={rec}")
    if not parts:
        return [Check("Camera", "ipcam state", "info", "not reported")]
    return [Check("Camera", "ipcam", "pass", " ".join(parts))]


def check_print_state(state: dict[str, Any]) -> list[Check]:
    p = state.get("print", {})
    gs = (p.get("gcode_state") or "").upper()
    out = []
    if gs in ("RUNNING", "PAUSE"):
        pct = p.get("mc_percent", 0)
        layer = p.get("layer_num", 0)
        total = p.get("total_layer_num", 0)
        eta = p.get("mc_remaining_time", 0)
        out.append(Check("Job", "print state", "info",
                          f"{gs} {pct}% layer {layer}/{total or '?'} "
                          f"ETA {eta}min"))
    elif gs == "FAILED":
        reason = p.get("fail_reason", "?")
        out.append(Check("Job", "print state", "fail",
                          f"FAILED (reason {reason})"))
    elif gs == "FINISH":
        out.append(Check("Job", "print state", "pass", "FINISH (idle)"))
    elif gs == "IDLE" or not gs:
        out.append(Check("Job", "print state", "pass", "IDLE"))
    else:
        out.append(Check("Job", "print state", "info", gs))
    err = p.get("print_error", 0)
    if err:
        out.append(Check("Job", "print_error", "fail", str(err)))
    return out


# ----- aggregation -------------------------------------------------------


def run_all_checks(state: dict[str, Any]) -> list[Check]:
    """Run every checker on a state dict and return the full check list."""
    return (
        check_print_state(state)
        + check_hms_errors(state)
        + check_thermistor_sanity(state)
        + check_ams_humidity(state)
        + check_wifi_signal(state)
        + check_camera(state)
    )


def format_report(checks: list[Check]) -> str:
    """Render checks grouped by category, sorted within category by severity."""
    by_cat: dict[str, list[Check]] = {}
    for c in checks:
        by_cat.setdefault(c.category, []).append(c)
    sev_order = {"fail": 0, "warn": 1, "pass": 2, "info": 3}
    lines = []
    for cat in sorted(by_cat.keys()):
        items = sorted(by_cat[cat], key=lambda c: sev_order.get(c.severity, 9))
        lines.append(f"\n{cat}")
        for c in items:
            glyph = _GLYPHS[c.severity]
            lines.append(f"  {glyph} {c.name:<28} {c.detail}")
    # Summary
    n_fail = sum(1 for c in checks if c.severity == "fail")
    n_warn = sum(1 for c in checks if c.severity == "warn")
    n_pass = sum(1 for c in checks if c.severity == "pass")
    lines.append(f"\nSummary: {n_pass} pass, {n_warn} warn, {n_fail} fail")
    return "\n".join(lines)


# ----- environment checks (fresh-OS / FRE prerequisites) ------------------


def check_environment() -> list[Check]:
    """Check the host environment for beambam prerequisites.

    Audits things that block first-run usage on a freshly-installed OS:
      - ~/.x2d/credentials   (LAN access codes; needed for LAN commands)
      - ~/.x2d/cloud_session.json (cloud token; needed for cloud-*)
      - bambu-studio binary in PATH (needed for slicing)
      - adb in PATH (needed for fcm-harvest)
      - python-cryptography importable (needed for cloud + signed-MQTT)

    Each missing prereq surfaces a `Check` with a `detail` that includes
    the suggested fix command. Pair with --fix to walk the user through
    resolving them interactively."""
    import shutil
    import importlib.util
    from pathlib import Path

    out: list[Check] = []
    home = Path.home()

    creds = home / ".x2d" / "credentials"
    if creds.is_file():
        out.append(Check("Environment", "LAN credentials", "pass",
                         f"{creds} present"))
    else:
        out.append(Check("Environment", "LAN credentials", "warn",
                         f"{creds} missing — run `beambam init` for the "
                         "first-run wizard (LAN discovery + access-code prompt)"))

    session = home / ".x2d" / "cloud_session.json"
    if session.is_file():
        try:
            import json as _json
            j = _json.loads(session.read_text())
            if j.get("access_token"):
                out.append(Check("Environment", "Cloud session", "pass",
                                 f"logged in as {j.get('user_id','?')} "
                                 f"(region={j.get('region','us')})"))
            else:
                out.append(Check("Environment", "Cloud session", "warn",
                                 f"{session} has no access_token — re-run "
                                 "`beambam cloud-login --code-only`"))
        except Exception as e:                              # noqa: BLE001
            out.append(Check("Environment", "Cloud session", "warn",
                             f"{session} unparseable ({e}) — re-run "
                             "`beambam cloud-login --code-only`"))
    else:
        out.append(Check("Environment", "Cloud session", "warn",
                         "no cloud_session.json — run `beambam cloud-login "
                         "--code-only` for the passwordless device-code flow"))

    bs = shutil.which("bambu-studio") or shutil.which("BambuStudio")
    if not bs:
        local = Path(__file__).resolve().parent.parent / "bs-bionic" / "build" / "src" / "bambu-studio"
        if local.is_file():
            bs = str(local)
    if bs:
        out.append(Check("Environment", "Bambu Studio CLI", "pass",
                         f"found at {bs}"))
    else:
        out.append(Check("Environment", "Bambu Studio CLI", "warn",
                         "no `bambu-studio` in PATH — slicing commands "
                         "(slice / slice-print / cloud-print-design) will "
                         "fail. Install from https://bambulab.com/en/download "
                         "or build under bs-bionic/."))

    adb = shutil.which("adb")
    if adb:
        out.append(Check("Environment", "adb (FCM harvester)", "pass",
                         f"found at {adb}"))
    else:
        out.append(Check("Environment", "adb (FCM harvester)", "info",
                         "no `adb` in PATH — only matters for `fcm-harvest`. "
                         "Termux: `pkg install android-tools`."))

    if importlib.util.find_spec("cryptography"):
        out.append(Check("Environment", "python-cryptography", "pass", "importable"))
    else:
        out.append(Check("Environment", "python-cryptography", "fail",
                         "not installed — cloud + signed-MQTT will not work. "
                         "Install: `pip install cryptography`"))

    # Printer-control signing key (optional). X-series firmware verifies an
    # RSA-SHA256 signature on print.* control commands; without the recovered
    # key, pause/resume/stop/start/skip are rejected (`mqtt message verify
    # failed`). Reading (status / camera / pushall) works without it.
    sign_key = home / ".x2d" / "printer_sign_key.pem"
    cert_id = home / ".x2d" / "printer_cert_id.txt"
    if sign_key.is_file() and cert_id.is_file():
        out.append(Check("Environment", "Printer control key", "pass",
                         "RSA signing key + cert_id present — signed control "
                         "(pause/resume/stop/start/skip) enabled"))
    elif sign_key.is_file():
        out.append(Check("Environment", "Printer control key", "warn",
                         f"{sign_key} present but {cert_id} missing — re-run "
                         "`beambam key --adb <ip:port>` to (re)write cert_id"))
    else:
        out.append(Check("Environment", "Printer control key", "info",
                         "no RSA signing key — printer CONTROL (pause/resume/"
                         "stop/start/skip) is gated on X-series firmware. "
                         "Optional: `beambam key --adb <ip:port>` recovers it "
                         "from a captured Bambu Handy (see "
                         "runtime/handy_extract/SIGNER_HANDOFF.md)."))

    # Device cert (X2D/H2D LAN print only). The signed `project_file` carries the
    # FTP file location as `url_enc` = RSA-encrypted to the printer's own device
    # cert; without it, X2D/H2D LAN print can't build a body the firmware accepts.
    # P1/A1/X1 use a plaintext `url` and don't need this.
    device_cert = home / ".x2d" / "printer_device_cert.pem"
    if device_cert.is_file():
        out.append(Check("Environment", "Printer device cert", "pass",
                         "device cert present — X2D/H2D LAN print (url_enc) enabled"))
    else:
        out.append(Check("Environment", "Printer device cert", "info",
                         "no device cert — only needed for X2D/H2D LAN print "
                         "(P1/A1/X1 don't use url_enc). Fetch with "
                         "`beambam device-cert`."))
    return out


def _interactive_fix(checks: list[Check]) -> int:
    """Walk the user through resolving each warn/fail environment check.

    For each actionable check, prompts [y/N/q]. Returns 0 when every
    actionable check is either fixed or skipped, 1 if the user quit."""
    actionable = [c for c in checks
                  if c.category == "Environment" and c.severity in ("warn", "fail")]
    if not actionable:
        print("\nNothing to fix — every environment check passed.")
        return 0
    print(f"\n{len(actionable)} item(s) actionable:")
    import subprocess
    import sys as _sys
    for c in actionable:
        print(f"\n  {c.name}: {c.detail}")
        try:
            choice = input("    Fix this now? [y/N/q] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nquit"); return 1
        if choice == "q":
            print("quit early"); return 1
        if choice != "y":
            continue
        if c.name == "LAN credentials":
            subprocess.call([_sys.executable, "-m", "beambam", "init"])
        elif c.name == "Cloud session":
            subprocess.call([_sys.executable, "-m", "beambam", "cloud-login", "--code-only"])
        elif c.name == "Bambu Studio CLI":
            print("    Install instructions:")
            print("      Linux:  https://github.com/bambulab/BambuStudio/releases")
            print("      Termux: build under bs-bionic/ via cmake (see docs)")
            print("    No automated fix; install manually + re-run `beambam doctor --fix`.")
        elif c.name == "adb (FCM harvester)":
            print("    Termux:  `pkg install android-tools`")
            print("    Linux:   `apt install android-tools-adb` (or distro equivalent)")
        elif c.name == "python-cryptography":
            print("    → pip install cryptography")
            subprocess.call([_sys.executable, "-m", "pip", "install", "cryptography"])
    return 0


# ----- CLI ----------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "doctor",
        help="Comprehensive printer + environment diagnostic. Default mode "
             "checks both host prereqs (credentials, cloud session, "
             "bambu-studio) and printer state (AMS humidity, HMS errors, "
             "sensor sanity, wifi, camera). --env-only skips the printer "
             "checks; --fix walks the user through resolving each "
             "actionable warn/fail interactively. Returns exit 0/1/2 "
             "for pass/warn/fail.",
    )
    p.add_argument("--no-color", action="store_true",
                   help="Disable ANSI color in output")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Machine output: list of check dicts")
    p.add_argument("--env", action="store_true",
                   help="Include host environment / fresh-OS prereq checks "
                        "alongside the printer state checks. (env checks "
                        "are also run automatically if the printer is "
                        "unreachable, so fresh-OS users get actionable "
                        "next steps.)")
    p.add_argument("--env-only", action="store_true",
                   help="Only check host environment / fresh-OS prereqs; "
                        "skip the printer state entirely.")
    p.add_argument("--fix", action="store_true",
                   help="After the report, walk the user through resolving "
                        "each actionable warn/fail (interactive). Implies "
                        "--env-only.")
    p.set_defaults(fn=cmd_doctor)
    return p


def cmd_doctor(args: argparse.Namespace) -> int:
    env_only = getattr(args, "env_only", False) or getattr(args, "fix", False)
    want_env = env_only or getattr(args, "env", False)

    checks: list[Check] = []
    if env_only:
        # Explicit env-only mode (or --fix); skip the printer entirely.
        checks.extend(check_environment())
    else:
        # Default: printer checks. Env checks only run if (a) the user
        # passed --env explicitly, or (b) the printer is unreachable and
        # we want to surface what's mis-configured locally.
        if want_env:
            checks.extend(check_environment())
        from beambam import Printer
        try:
            with Printer() as printer:
                state = printer.state(timeout=10.0)
            checks.extend(run_all_checks(state))
        except Exception as e:                              # noqa: BLE001
            # Preserve the legacy contract: tests + callers may grep
            # stderr for the unreachable-printer error before deciding
            # whether to fall back to env-only mode.
            print(f"can't reach printer: {e}", file=sys.stderr)
            # Record the failure as a Check so the report + exit code
            # surface it (return 2 when any check is `fail`).
            checks.append(Check("Connectivity", "Reach printer", "fail",
                                f"can't reach printer: {e}"))
            # Auto-fall-through to env checks so fresh-OS users see
            # actionable next steps alongside the reachability failure.
            if not want_env:
                checks.extend(check_environment())

    if args.json_out:
        import dataclasses
        import json as _json
        print(_json.dumps([dataclasses.asdict(c) for c in checks], indent=2))
    else:
        text = format_report(checks)
        if args.no_color:
            import re
            text = re.sub(r"\033\[[0-9;]*m", "", text)
        print(text)

    if getattr(args, "fix", False):
        _interactive_fix(checks)

    if any(c.severity == "fail" for c in checks):
        return 2
    if any(c.severity == "warn" for c in checks):
        return 1
    return 0
