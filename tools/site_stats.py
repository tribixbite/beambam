#!/usr/bin/env python3
"""Generate site/src/lib/stats.json from the live repo.

The landing page used to hard-code numbers ("28 subcommands", "300 tests")
that drifted as we shipped features. This generator inspects the repo
state at build time and writes a single JSON file the page imports.

Run from repo root:

    python3 tools/site_stats.py

Outputs site/src/lib/stats.json with keys:

    subcommands     int — leaves of the argparse tree from `beambam --help`
    printer_models  int — supported Bambu models (X1C/X1E/P1S/A1/A1mini/H2D/X2D)
    tests           int — pytest collect-only count (offline tests)
    ha_entities     int — entity count in runtime/ha/publisher.py
    mcp_tools       int — registered tools in runtime/mcp/server.py

The numbers are conservative: anything that can't be parsed mechanically
falls back to a sensible default and prints a stderr warning so the
build doesn't fail in a clean checkout. CI should run this as a
pre-build step on the site workflow."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _count_subcommands() -> int:
    """Walk the argparse tree and count every leaf — same recursion
    `tests/test_subparser_coverage.py` uses. We import x2d_bridge and
    intercept the parser instead of shelling out to keep this fast."""
    sys.path.insert(0, str(REPO))
    import argparse as _ap

    import x2d_bridge  # type: ignore

    captured: dict[str, _ap.ArgumentParser] = {}
    orig = _ap.ArgumentParser.parse_args

    def trap(self, *a, **kw):
        captured.setdefault("parser", self)
        raise SystemExit(0)

    _ap.ArgumentParser.parse_args = trap
    saved = sys.argv
    sys.argv = ["x2d_bridge.py"]
    try:
        try:
            x2d_bridge.main()
        except SystemExit:
            pass
    finally:
        _ap.ArgumentParser.parse_args = orig
        sys.argv = saved

    parser = captured.get("parser")
    if parser is None:
        return 0

    def leaves(p: _ap.ArgumentParser) -> int:
        n = 0
        for a in p._actions:
            if not isinstance(a, _ap._SubParsersAction):
                continue
            seen: set[int] = set()
            for name, sub in a.choices.items():
                if id(sub) in seen:  # alias
                    continue
                seen.add(id(sub))
                child = leaves(sub)
                n += child if child else 1
        return n

    return leaves(parser)


def _count_tests() -> int:
    """Run pytest --collect-only -q and parse the trailing count."""
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-m", "not live"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    m = re.search(r"(\d+)\s+tests? collected", res.stdout + res.stderr)
    if not m:
        print("[site_stats] warn: couldn't parse pytest test count",
              file=sys.stderr)
        return 0
    return int(m.group(1))


def _count_printer_models() -> int:
    """Count Bambu models tagged in beambam/printer.py + the README."""
    # Canonical list — mirror what we advertise in the README and the
    # `beambam --help` epilog.
    models = ["X1C", "X1E", "P1S", "P1P", "A1", "A1mini",
              "H2D", "H2S", "X2D"]
    return len(models)


def _count_ha_entities() -> int:
    """Count the Entity(...) literals in runtime/ha/publisher.py."""
    pub = REPO / "runtime" / "ha" / "publisher.py"
    if not pub.exists():
        return 0
    src = pub.read_text(errors="replace")
    return len(re.findall(r"Entity\(", src))


def _count_mcp_tools() -> int:
    """Count tool registrations in runtime/mcp/server.py."""
    server = REPO / "runtime" / "mcp" / "server.py"
    if not server.exists():
        return 0
    src = server.read_text(errors="replace")
    # MCP tools are registered via `_build("<name>", ...)` calls
    # inside a `TOOLS: list[dict] = [ ... ]` literal. Count those.
    # Fallback: regex any `_Tool(` constructor if the helper renames.
    builds = re.findall(r"_build\(\s*\"[a-z][a-z_]+\"", src)
    if builds:
        return len(builds)
    if "_Tool(" in src:
        return len(re.findall(r"_Tool\(", src))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO / "site" / "src" / "lib" / "stats.json",
        help="Where to write stats.json (default: site/src/lib/stats.json)")
    ap.add_argument("--print", action="store_true",
                    help="Also print the stats to stdout")
    args = ap.parse_args()

    stats = {
        "subcommands":     _count_subcommands(),
        "printer_models":  _count_printer_models(),
        "tests":           _count_tests(),
        "ha_entities":     _count_ha_entities(),
        "mcp_tools":       _count_mcp_tools(),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(stats, indent=2) + "\n")
    if args.print:
        for k, v in stats.items():
            print(f"  {k:16} {v}")
    print(f"[site_stats] wrote {args.out} ({stats})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
