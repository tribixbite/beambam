"""MCP stdio server for the X2D bridge.

Wraps every bridge CLI verb as an MCP tool, plus two resources:

* ``x2d://state``           — latest pushall state (JSON)
* ``x2d://camera/snapshot`` — most recent JPEG frame from the camera daemon

The server reads newline-delimited JSON-RPC 2.0 messages from stdin and
writes responses to stdout. Stderr is reserved for human-readable trace
so the host (Claude Desktop, etc.) can show errors without corrupting
the JSON-RPC channel.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Bridge invocation: prefer `python3 -m beambam.cli` (resolves via the
# installed package or PYTHONPATH=repo source checkout). $X2D_BRIDGE
# overrides — historically a `.py` path; if set, we honor it for back-
# compat. $X2D_BRIDGE_PYTHON overrides the interpreter (default:
# current sys.executable).
_REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH_OVERRIDE = os.environ.get("X2D_BRIDGE")          # legacy: path to .py
BRIDGE_PATH = Path(BRIDGE_PATH_OVERRIDE) if BRIDGE_PATH_OVERRIDE else None
BRIDGE_PYTHON = os.environ.get("X2D_BRIDGE_PYTHON", sys.executable)
DAEMON_HTTP = os.environ.get("X2D_DAEMON_HTTP", "http://127.0.0.1:8765")
DAEMON_AUTH = os.environ.get("X2D_DAEMON_TOKEN", "")
CALL_TIMEOUT_S = float(os.environ.get("X2D_MCP_CALL_TIMEOUT", "30"))

# MCP protocol revisions this server speaks, newest first. `initialize`
# echoes the client's requested revision when it is one of these and
# otherwise answers with the newest (the spec's negotiation rule).
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_NAME = "beambam"


def _server_version() -> str:
    try:
        from beambam._version import PACKAGE_VERSION
    except Exception:                    # pragma: no cover - broken install
        return "0.0.0+unknown"
    return str(PACKAGE_VERSION)


SERVER_VERSION = _server_version()


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

def _creds(args: dict) -> list[str]:
    """Top-level `--printer NAME` for the bridge CLI.

    `--printer` (like `--ip/--code/--serial`) is defined on the CLI's
    top-level parser only, so it must come BEFORE the subcommand:
    `beambam --printer lab status`. Placed after the subcommand, argparse
    rejected it and every printer-targeted tool call failed.
    """
    p = args.get("printer")
    return ["--printer", str(p)] if p else []


def _cmd(args: dict, *argv: str) -> list[str]:
    """argv for one printer-targeted CLI call: creds, then subcommand."""
    return _creds(args) + list(argv)


def _global_slot(args: dict, key: str = "slot") -> tuple[str, str]:
    """Split a global AMS slot (unit*4 + tray, 0..15) into the CLI's
    `ams slot` positionals."""
    slot = int(args[key])
    if not 0 <= slot <= 15:
        raise _ToolError(f"{key} must be 0..15 (unit*4 + tray), got {slot}")
    return str(slot // 4), str(slot % 4)


def _print_slots(args: dict) -> list[str]:
    """`--slot` for `beambam print`: global slot(s), one per filament,
    comma-separated (an integer is one slot)."""
    if args.get("slot") is None:
        return []
    raw = args["slot"]
    text = str(int(raw)) if isinstance(raw, (int, float)) else str(raw)
    return ["--slot", text]


_PRINTER_PROP = {
    "printer": {
        "type": "string",
        "description": "Optional printer name from ~/.x2d/credentials "
                       "([printer:NAME] section). If omitted, the default "
                       "[printer] section is used.",
    },
}


def _build(name: str,
           description: str,
           argv: Callable[[dict], list[str]],
           extra_props: dict | None = None,
           required: list[str] | None = None) -> dict:
    schema_props: dict[str, Any] = dict(_PRINTER_PROP)
    if extra_props:
        schema_props.update(extra_props)
    return {
        "name": name,
        "description": description,
        "argv": argv,
        "inputSchema": {
            "type": "object",
            "properties": schema_props,
            "required": list(required or []),
            "additionalProperties": False,
        },
    }


# Each entry's "argv" returns the args[] passed to the bridge CLI; the
# caller prepends BRIDGE_PYTHON + BRIDGE_PATH and forwards stdout/stderr.
TOOLS: list[dict] = [
    _build("status",
           "Fetch the latest printer state JSON (temps, AMS, print job, lights).",
           lambda a: _cmd(a, "status")),

    _build("list_printers",
           "List every [printer] / [printer:NAME] section configured in "
           "~/.x2d/credentials.",
           lambda _a: ["printers"]),

    _build("pause",
           "Pause the current print.",
           lambda a: _cmd(a, "pause")),

    _build("resume",
           "Resume a paused print.",
           lambda a: _cmd(a, "resume")),

    _build("stop",
           "Abort the current print (cannot be resumed).",
           lambda a: _cmd(a, "stop")),

    _build("home",
           "Home all axes (G28).",
           lambda a: _cmd(a, "home")),

    _build("level",
           "Run auto bed-level (G29).",
           lambda a: _cmd(a, "level")),

    _build("gcode",
           "Send a single arbitrary G-code line to the printer.",
           lambda a: _cmd(a, "gcode", str(a["line"])),
           extra_props={
               "line": {"type": "string",
                        "description": "G-code line, e.g. 'M104 S210' or 'G1 X10'"},
           },
           required=["line"]),

    _build("set_temp",
           "Set a heater target temperature.",
           lambda a: (_cmd(a, "set-temp", str(a["target"]),
                           str(int(a["value"])))
                      + (["--idx", str(int(a["idx"]))] if "idx" in a else [])),
           extra_props={
               "target": {"type": "string",
                          "enum": ["bed", "nozzle", "chamber"],
                          "description": "Which heater to set."},
               "value": {"type": "integer",
                         "description": "Target temperature in degrees Celsius."},
               "idx": {"type": "integer",
                       "description": "Extruder index (only for target=nozzle)."},
           },
           required=["target", "value"]),

    _build("chamber_light",
           "Control the chamber LED (on / off / flashing).",
           lambda a: (_cmd(a, "chamber-light", str(a["state"]))
                      + (["--on-time", str(int(a["on_time"]))] if "on_time" in a else [])
                      + (["--off-time", str(int(a["off_time"]))] if "off_time" in a else [])
                      + (["--loops", str(int(a["loops"]))] if "loops" in a else [])),
           extra_props={
               "state": {"type": "string",
                         "enum": ["on", "off", "flashing"]},
               "on_time": {"type": "integer", "description": "ms (flashing only)"},
               "off_time": {"type": "integer", "description": "ms (flashing only)"},
               "loops": {"type": "integer", "description": "flashing cycles, 0=forever"},
           },
           required=["state"]),

    _build("ams_unload",
           "Eject filament from the AMS feed-tube back to the spool.",
           lambda a: _cmd(a, "ams-unload", str(int(a.get("ams", 0)))),
           extra_props={
               "ams": {"type": "integer", "minimum": 0,
                       "description": "AMS unit index (0 = first unit; "
                                      "default 0)."},
           }),

    _build("ams_load",
           "Load filament from a specific AMS slot.",
           lambda a: _cmd(a, "ams-load", *_global_slot(a)),
           extra_props={
               "slot": {"type": "integer", "minimum": 0, "maximum": 15,
                        "description": "Global AMS slot: unit*4 + tray, "
                                       "0..15 (0 = first tray of the "
                                       "first unit)."},
           },
           required=["slot"]),

    _build("jog",
           "Jog one axis by a relative distance (mm).",
           lambda a: _cmd(a, "jog", str(a["axis"]).upper(),
                          f"{float(a['distance']):g}"),
           extra_props={
               "axis": {"type": "string",
                        "enum": ["x", "y", "z", "e"]},
               "distance": {"type": "number",
                            "description": "Millimetres; negative for reverse."},
           },
           required=["axis", "distance"]),

    _build("upload",
           "Upload a sliced .gcode.3mf to the printer's SD card "
           "(does NOT start the print).",
           lambda a: _cmd(a, "upload", str(a["path"])),
           extra_props={
               "path": {"type": "string",
                        "description": "Local path to .gcode.3mf"},
           },
           required=["path"]),

    _build("print",
           "Upload + start a print. The printer must be idle (the CLI "
           "refuses a busy printer).",
           lambda a: _cmd(a, "print", str(a["path"])) + _print_slots(a),
           extra_props={
               "path": {"type": "string",
                        "description": "Local path to a sliced .gcode.3mf"},
               "slot": {"type": ["integer", "string"],
                        "description": "Global AMS slot(s), unit*4 + tray "
                                       "(0..15): one integer, or one per "
                                       "filament the plate uses, comma-"
                                       "separated in filament order "
                                       "(e.g. \"4,0,12\")."},
           },
           required=["path"]),

    _build("camera_snapshot",
           "Fetch the most recent JPEG frame from the running camera "
           "daemon (beambam camera). The frame is returned as a "
           "base64-encoded image content block. Errors if the camera "
           "daemon isn't running.",
           lambda _a: []),  # handled specially in _call_tool

    _build("healthz",
           "Hit the bridge daemon's /healthz endpoint and return the "
           "JSON. 200 + healthy=true means recent state arrived; 503 "
           "+ healthy=false means MQTT silently disconnected.",
           lambda _a: []),  # handled specially in _call_tool

    _build("metrics",
           "Fetch the bridge daemon's Prometheus /metrics body.",
           lambda _a: []),  # handled specially

    # ----- v1.2.0 additions ----------------------------------------------

    _build("analyze",
           "Dissect a local .gcode.3mf file: filament/nozzle assignment, "
           "per-phase toolchanges, real flush volume in mm + grams, AMS-tray "
           "requirements, slicer warnings, and optimization hints. "
           "Returns structured JSON with phases, totals, hints arrays.",
           lambda a: ["analyze", a["file"], "--json"],
           extra_props={
               "file": {"type": "string",
                        "description": "Local path to a .gcode.3mf file"},
           },
           required=["file"]),

    _build("ams_status",
           "Pretty-printed AMS state for every loaded unit: per-tray color, "
           "filament type, tray_info_idx, humidity level, remaining %, and "
           "active-feed marker. Use this to answer 'what's loaded?' questions "
           "without parsing the raw state JSON.",
           lambda a: _cmd(a, "ams", "status", "--json")),

    _build("ams_tray_info",
           "Detailed info for a single AMS tray by global slot (unit*4 + tray, "
           "0..15): color, filament type/sub-brand, tray_info_idx, nozzle "
           "temp range, bed temp + type, drying temp/time, remaining %, RFID "
           "tag UID, current state.",
           lambda a: _cmd(a, "ams", "info", str(int(a["slot"])),
                          "--no-color"),
           extra_props={
               "slot": {"type": "integer", "minimum": 0, "maximum": 15,
                        "description": "Global slot 0..15 (unit*4 + tray)"},
           },
           required=["slot"]),

    _build("doctor",
           "Comprehensive printer health diagnostic — AMS humidity warnings, "
           "active HMS error codes with descriptions, thermistor sanity, "
           "wifi RSSI thresholds, camera state, print state surfacing. "
           "Returns a list of {category, name, severity, detail} dicts. "
           "Severity is one of pass/warn/fail/info.",
           lambda a: _cmd(a, "doctor", "--json")),

    _build("download",
           "Pull a file off the printer's SD card via FTPS. Use this for "
           "/cache/<filename>.gcode.3mf (the just-sent print), /timelapse/... "
           "(stored videos), or any path returned by the `files` tool.",
           lambda a: _cmd(a, "download", str(a["remote_path"]),
                          str(a["local_path"])),
           extra_props={
               "remote_path": {"type": "string",
                               "description": "Path on the printer's SD"},
               "local_path": {"type": "string",
                              "description": "Local destination path"},
           },
           required=["remote_path", "local_path"]),

    _build("queue_list",
           "List the persistent print queue (~/.x2d/queue.json). Each job: "
           "id, printer, gcode path, slot, status (pending/running/done/...), "
           "label, enqueued/started/finished timestamps, any error message.",
           lambda _a: ["queue", "list"]),

    _build("find_printers",
           "SSDP M-SEARCH discovery on the LAN. Returns every Bambu printer "
           "that responds with ip / serial / model / name / signal-strength / "
           "connection state / bound-to-cloud status.",
           lambda a: (["find", "--timeout", str(int(a.get("timeout", 3))),
                        "--json"]),
           extra_props={
               "timeout": {"type": "integer",
                           "description": "Seconds to wait for responses "
                                          "(default 3, max 30)"},
           }),
]

TOOLS_BY_NAME: dict[str, dict] = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

RESOURCES = [
    {
        "uri": "x2d://state",
        "name": "Latest printer state",
        "description": "JSON pushall state from the X2D (temps, AMS, print "
                       "job, lights). Refreshed on every read by reaching "
                       "out to the daemon if running, else by querying the "
                       "printer directly.",
        "mimeType": "application/json",
    },
    {
        "uri": "x2d://camera/snapshot",
        "name": "Latest camera snapshot",
        "description": "Most recent JPEG frame from the camera daemon. "
                       "Requires `beambam camera` to be running.",
        "mimeType": "image/jpeg",
    },
]


# ---------------------------------------------------------------------------
# Helpers — bridge invocation
# ---------------------------------------------------------------------------

def _run_bridge(argv: list[str]) -> tuple[int, str, str]:
    """Spawn the bridge CLI and capture output.

    Default invocation is `BRIDGE_PYTHON -m beambam.cli …argv`. If
    $X2D_BRIDGE is set (legacy: pathname to a .py launcher), we spawn
    `BRIDGE_PYTHON <BRIDGE_PATH> …argv` instead. Returns (returncode,
    stdout, stderr). Times out at CALL_TIMEOUT_S.
    """
    if BRIDGE_PATH is not None:
        if not BRIDGE_PATH.exists():
            return (127, "",
                    f"X2D_BRIDGE points at missing file: {BRIDGE_PATH}")
        cmd = [BRIDGE_PYTHON, str(BRIDGE_PATH), *argv]
    else:
        cmd = [BRIDGE_PYTHON, "-m", "beambam.cli", *argv]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=CALL_TIMEOUT_S, cwd=str(_REPO_ROOT),
        )
    except subprocess.TimeoutExpired as e:
        return (124, e.stdout or "", (e.stderr or "") +
                f"\n[mcp] timed out after {CALL_TIMEOUT_S}s")
    return (proc.returncode, proc.stdout, proc.stderr)


def _http_get(path: str, accept: str = "*/*") -> tuple[int, bytes, str]:
    """GET ${DAEMON_HTTP}{path}. Returns (status, body, content_type).
    A connection refusal returns (0, b"", ""). Bearer token honoured if
    $X2D_DAEMON_TOKEN is set.
    """
    url = f"{DAEMON_HTTP.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Accept": accept})
    if DAEMON_AUTH:
        req.add_header("Authorization", f"Bearer {DAEMON_AUTH}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return (resp.status, resp.read(),
                    resp.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return (e.code, body, e.headers.get("Content-Type", "") if e.headers else "")
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return (0, b"", "")


# ---------------------------------------------------------------------------
# JSON-RPC dispatcher
# ---------------------------------------------------------------------------

def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _negotiate_version(requested: Any) -> str:
    """The spec's rule: echo the client's revision when we support it,
    otherwise answer with our newest and let the client decide."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSION


def _initialize(params: dict) -> dict:
    return {
        "protocolVersion": _negotiate_version(params.get("protocolVersion")),
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
        # Only what this server implements: there is no logging/setLevel
        # handler, so the `logging` capability is not advertised.
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
        },
        "instructions":
            "X2D printer control bridge. Use the `status` tool to read "
            "current temps/AMS/print job; the `pause`/`resume`/`stop` "
            "verbs to control a running print; `print` to upload+start "
            "a sliced .gcode.3mf. Multi-printer setups: pass `printer` "
            "= the [printer:NAME] section name. The `x2d://state` "
            "resource always returns the freshest JSON state.",
    }


def _tools_list(_params: dict) -> dict:
    return {
        "tools": [
            {k: v for k, v in t.items() if k != "argv"}
            for t in TOOLS
        ],
    }


def _resources_list(_params: dict) -> dict:
    return {"resources": list(RESOURCES)}


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise _ToolError(f"arguments must be an object, got {type(arguments).__name__}")
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise _ToolError(f"unknown tool {name!r}; "
                         f"available: {sorted(TOOLS_BY_NAME)}")
    # Special-cased tools that hit the HTTP daemon directly.
    if name == "camera_snapshot":
        return _camera_snapshot()
    if name == "healthz":
        printer = arguments.get("printer")
        query = ("?printer=" + urllib.parse.quote(str(printer))
                 if printer else "")
        return _http_text_tool("/healthz" + query)
    if name == "metrics":
        return _http_text_tool("/metrics", accept="text/plain")
    argv = tool["argv"](arguments)
    code, stdout, stderr = _run_bridge(argv)
    text = stdout if stdout.strip() else stderr
    return {
        "content": [{"type": "text", "text": text or "(no output)"}],
        "isError": code != 0,
    }


def _camera_snapshot() -> dict:
    status, body, ctype = _http_get("/cam.jpg")
    if status == 0:
        return {
            "content": [{"type": "text", "text":
                "Camera daemon unreachable at " + DAEMON_HTTP + "/cam.jpg.\n"
                "Run `beambam camera --bind 127.0.0.1:8765` first.\n"
                "Override the URL via $X2D_DAEMON_HTTP."}],
            "isError": True,
        }
    if status != 200 or not body or "image" not in ctype:
        return {
            "content": [{"type": "text", "text":
                f"camera daemon returned status={status} content-type={ctype!r} "
                f"body-len={len(body)}"}],
            "isError": True,
        }
    return {
        "content": [{
            "type": "image",
            "data": base64.b64encode(body).decode("ascii"),
            "mimeType": "image/jpeg",
        }],
        "isError": False,
    }


def _http_text_tool(path: str, accept: str = "application/json") -> dict:
    status, body, ctype = _http_get(path, accept=accept)
    if status == 0:
        return {
            "content": [{"type": "text", "text":
                f"Bridge daemon unreachable at {DAEMON_HTTP}{path}.\n"
                "Run `beambam daemon --http 127.0.0.1:8765` first.\n"
                "Override the URL via $X2D_DAEMON_HTTP."}],
            "isError": True,
        }
    text = body.decode("utf-8", errors="replace") if body else ""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": status >= 400,
    }


def _read_resource(params: dict) -> dict:
    uri = params.get("uri", "")
    if uri == "x2d://state":
        # Prefer the running daemon's HTTP /state — it's a cheap GET that
        # returns the cached push without re-dialing MQTT.
        status, body, _ctype = _http_get("/state")
        if status == 200 and body:
            return {
                "contents": [{
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": body.decode("utf-8", errors="replace"),
                }],
            }
        # Fallback: shell out to `status` which dials MQTT itself.
        code, stdout, stderr = _run_bridge(["status"])
        if code != 0:
            raise _ToolError(f"x2d://state failed: rc={code} stderr={stderr.strip()[:200]}")
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": stdout,
            }],
        }
    if uri == "x2d://camera/snapshot":
        status, body, ctype = _http_get("/cam.jpg")
        if status != 200 or not body:
            raise _ToolError(
                f"x2d://camera/snapshot unreachable (status={status}, "
                f"daemon={DAEMON_HTTP}); run `beambam camera` first.")
        return {
            "contents": [{
                "uri": uri,
                "mimeType": ctype or "image/jpeg",
                "blob": base64.b64encode(body).decode("ascii"),
            }],
        }
    raise _ToolError(f"unknown resource uri {uri!r}")


def _ping(_params: dict) -> dict:
    return {}


METHODS: dict[str, Callable[[dict], dict]] = {
    "initialize":      _initialize,
    "tools/list":      _tools_list,
    "tools/call":      _call_tool,
    "resources/list":  _resources_list,
    "resources/read":  _read_resource,
    "ping":            _ping,
}


class _ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# Stdio loop
# ---------------------------------------------------------------------------

def _handle(message: dict) -> dict | None:
    """Dispatch one JSON-RPC message; return the response (or None if
    notification)."""
    if not isinstance(message, dict):
        # Batches / scalars are not supported (MCP dropped JSON-RPC
        # batching in 2025-06-18).
        return _err(None, -32600, "request must be a JSON object")
    req_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _err(req_id, -32602, "params must be an object")
    is_notification = "id" not in message

    if not method:
        return None if is_notification else _err(
            req_id, -32600, "missing method")

    # Notifications: handle silently, never reply.
    if is_notification:
        # initialized / cancelled / progress are the common ones; all are
        # no-ops for this stateless server.
        return None

    handler = METHODS.get(method)
    if handler is None:
        return _err(req_id, -32601, f"method not found: {method}")
    try:
        return _ok(req_id, handler(params))
    except _ToolError as e:
        return _err(req_id, -32602, str(e))
    except Exception as e:  # noqa: BLE001 - top-level guard
        traceback.print_exc(file=sys.stderr)
        return _err(req_id, -32603, f"internal error: {e}")


def serve_stdio(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    bridge_label = str(BRIDGE_PATH) if BRIDGE_PATH else "python -m beambam.cli"
    print(f"[mcp] {SERVER_NAME} MCP server {SERVER_VERSION} up "
          f"(bridge={bridge_label}, daemon={DAEMON_HTTP})",
          file=sys.stderr, flush=True)
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            stdout.write(json.dumps(_err(None, -32700, f"parse error: {e}")) + "\n")
            stdout.flush()
            continue
        response = _handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="beambam-mcp",
        description="MCP (Model Context Protocol) stdio server for "
                    "beambam: exposes printer status/control as tools and "
                    "x2d://state + x2d://camera/snapshot as resources. "
                    "Reads newline-delimited JSON-RPC 2.0 on stdin; an MCP "
                    "host (Claude Desktop, ...) launches it.",
        epilog="Environment: X2D_DAEMON_HTTP (default "
               "http://127.0.0.1:8765), X2D_DAEMON_TOKEN, "
               "X2D_MCP_CALL_TIMEOUT (s), X2D_BRIDGE_PYTHON, X2D_BRIDGE.")
    p.add_argument("--version", action="version",
                   version=f"beambam-mcp {SERVER_VERSION} "
                           f"(MCP {', '.join(SUPPORTED_PROTOCOL_VERSIONS)})")
    return p


def main(argv: list[str] | None = None) -> int:
    # `beambam-mcp --help` / `--version` must answer and exit instead of
    # silently starting the server and waiting on stdin.
    _arg_parser().parse_args(argv)
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
