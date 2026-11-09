"""Every MCP tool must build an argv the real `beambam` CLI parser accepts.

Audit finding P1: `--printer/--ip/--code/--serial` were appended AFTER
the subcommand, but they only exist on the top-level parser, so 18 of 22
tools failed whenever a printer was named; `jog` passed --axis/--distance
where the CLI takes positionals, `ams_load` passed --slot instead of the
`ams slot` positionals and `ams_unload` omitted its required `ams`.

Plus the handshake fixes: protocol-version negotiation, no `logging`
capability without a handler, server name `beambam`, and
`beambam-mcp --help` printing help instead of starting the server.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.mcp import server as S  # noqa: E402
from tests.test_subparser_coverage import _build_parser  # noqa: E402

# Tools answered over HTTP by the MCP server itself (no CLI argv).
_HTTP_TOOLS = {"camera_snapshot", "healthz", "metrics"}

# Sample values for named properties; anything else is derived from the
# JSON-schema type.
_SAMPLES: dict[str, Any] = {
    "line": "G1 X-5 F600",
    "path": "part.gcode.3mf",
    "file": "part.gcode.3mf",
    "remote_path": "/cache/part.gcode.3mf",
    "local_path": "part.gcode.3mf",
    "distance": -2.5,
    "slot": 5,
    "ams": 1,
    "timeout": 3,
}


def _sample(name: str, prop: dict) -> Any:
    if name in _SAMPLES:
        return _SAMPLES[name]
    if "enum" in prop:
        return prop["enum"][-1]
    t = prop.get("type")
    if t == "integer":
        return 1
    if t == "number":
        return 1.5
    return "x"


def _arguments(tool: dict, *, with_optional: bool) -> dict:
    schema = tool["inputSchema"]
    props = schema["properties"]
    keys = list(props) if with_optional else list(schema["required"])
    args = {k: _sample(k, props[k]) for k in keys}
    if with_optional:
        args["printer"] = "lab"
    return args


@pytest.fixture(scope="module")
def parser() -> argparse.ArgumentParser:
    return _build_parser()


def _parse(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    err = io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            return parser.parse_args(argv)
    except SystemExit as e:
        raise AssertionError(f"CLI parser rejected {argv}: exit {e.code}\n"
                             f"{err.getvalue()}") from None


_CLI_TOOLS = [t for t in S.TOOLS if t["name"] not in _HTTP_TOOLS]


@pytest.mark.parametrize("tool", _CLI_TOOLS, ids=lambda t: t["name"])
@pytest.mark.parametrize("with_optional", [False, True],
                         ids=["required", "all+printer"])
def test_tool_argv_parses(parser, tool, with_optional):
    args = _arguments(tool, with_optional=with_optional)
    argv = tool["argv"](args)
    ns = _parse(parser, argv)
    assert callable(getattr(ns, "fn", None)), argv
    if with_optional and "printer" in tool["inputSchema"]["properties"] \
            and argv[:1] == ["--printer"]:
        assert ns.printer == "lab"


def test_every_printer_targeted_tool_forwards_printer(parser):
    """Tools that act on a printer must pass the chosen printer."""
    untargeted = {"list_printers", "analyze", "queue_list", "find_printers"}
    for tool in _CLI_TOOLS:
        if tool["name"] in untargeted:
            continue
        argv = tool["argv"](_arguments(tool, with_optional=True))
        assert argv[:2] == ["--printer", "lab"], (tool["name"], argv)
        assert _parse(parser, argv).printer == "lab"


def test_jog_maps_to_positionals(parser):
    ns = _parse(parser, S.TOOLS_BY_NAME["jog"]["argv"](
        {"axis": "z", "distance": -0.4, "printer": "lab"}))
    assert ns.axis == "Z"
    assert float(ns.distance) == pytest.approx(-0.4)


def test_ams_load_splits_global_slot(parser):
    ns = _parse(parser, S.TOOLS_BY_NAME["ams_load"]["argv"]({"slot": 13}))
    assert (int(ns.ams), int(ns.slot)) == (3, 1)
    with pytest.raises(S._ToolError):
        S.TOOLS_BY_NAME["ams_load"]["argv"]({"slot": 16})


def test_ams_unload_passes_unit(parser):
    ns = _parse(parser, S.TOOLS_BY_NAME["ams_unload"]["argv"]({}))
    assert int(ns.ams) == 0
    ns = _parse(parser, S.TOOLS_BY_NAME["ams_unload"]["argv"]({"ams": 2}))
    assert int(ns.ams) == 2


def test_print_slot_forms(parser):
    argv = S.TOOLS_BY_NAME["print"]["argv"]({"path": "a.gcode.3mf",
                                              "slot": "4,0,12"})
    assert argv[-2:] == ["--slot", "4,0,12"]
    _parse(parser, argv)
    argv = S.TOOLS_BY_NAME["print"]["argv"]({"path": "a.gcode.3mf", "slot": 3})
    assert argv[-2:] == ["--slot", "3"]
    argv = S.TOOLS_BY_NAME["print"]["argv"]({"path": "a.gcode.3mf"})
    assert "--slot" not in argv


# --- handshake ------------------------------------------------------------------

@pytest.mark.parametrize("requested,expected", [
    ("2025-06-18", "2025-06-18"),
    ("2025-03-26", "2025-03-26"),
    ("2024-11-05", "2024-11-05"),
    ("2099-01-01", S.PROTOCOL_VERSION),
    (None, S.PROTOCOL_VERSION),
])
def test_initialize_negotiates_version(requested, expected):
    params = {"protocolVersion": requested} if requested else {}
    r = S._initialize(params)
    assert r["protocolVersion"] == expected
    assert r["serverInfo"]["name"] == "beambam"
    assert "logging" not in r["capabilities"]
    assert set(r["capabilities"]) == {"tools", "resources"}


def test_non_object_messages_are_invalid_requests():
    assert S._handle([1, 2])["error"]["code"] == -32600          # type: ignore[arg-type]
    resp = S._handle({"jsonrpc": "2.0", "id": 4, "method": "ping",
                      "params": [1]})
    assert resp is not None and resp["error"]["code"] == -32602


def test_help_prints_usage_and_does_not_serve(monkeypatch):
    served: list[bool] = []
    monkeypatch.setattr(S, "serve_stdio", lambda *a, **k: served.append(True) or 0)
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(SystemExit) as ei:
        S.main(["--help"])
    assert ei.value.code == 0
    assert "beambam-mcp" in out.getvalue()
    assert served == []


def test_main_without_args_serves(monkeypatch):
    monkeypatch.setattr(S, "serve_stdio", lambda *a, **k: 0)
    assert S.main([]) == 0


def test_stdio_roundtrip_initialize():
    stdin = io.StringIO(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"}}) + "\n")
    stdout = io.StringIO()
    with redirect_stderr(io.StringIO()):
        S.serve_stdio(stdin, stdout)
    resp = json.loads(stdout.getvalue())
    assert resp["result"]["protocolVersion"] == "2025-03-26"
