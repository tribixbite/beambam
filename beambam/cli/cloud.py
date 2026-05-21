"""beambam.cli.cloud — cloud-* CLI handlers.

First module in the Phase 5b migration. Starting small with
`cmd_cloud_ttcode` — the rest of the `cmd_cloud_*` handlers stay in
`x2d_bridge.py` until full Phase 5b lands.

Each handler keeps the same signature and exit-code semantics as its
old monolith form so tests that import `x2d_bridge.cmd_cloud_*` keep
working through a re-export in x2d_bridge.py.
"""
from __future__ import annotations

import argparse
import json
import sys


def cmd_cloud_ttcode(args: argparse.Namespace) -> int:
    """Fetch Throughtek P2P NAT-traversal codes for cloud camera streaming.

    GETs `/v1/iot-service/api/user/ttcode?dev_id=<serial>`. The endpoint
    is gated 403 on regular cloud-login sessions — Bambu restricts it to
    their Connect / Handy clients via additional auth headers we don't
    have. The wrapper surfaces that gate with a clean stderr explanation
    instead of a raw API error.
    """
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_ttcode(args.serial)
    except cloud_client.CloudError as e:
        # The 403 is the documented expected outcome for non-Handy
        # sessions. Surface that more helpfully than a raw API trace.
        if e.status == 403:
            print(
                f"ttcode gated (HTTP 403): this endpoint is restricted "
                f"to Bambu Connect / Handy clients via additional auth "
                f"headers a regular cloud-login session doesn't have. "
                f"See [HANDY_DATA_AUDIT_PART2.md] for the auth-shape "
                f"reverse engineering.",
                file=sys.stderr)
            return 1
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    # Pretty-print the timed credentials.
    print(f"ttcode for {args.serial}:")
    for k, v in r.items():
        print(f"  {k:<14} {v}")
    return 0


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register the cloud-* subparsers this module owns.

    For now: just `cloud-ttcode`. As Phase 5b progresses, the remaining
    `cmd_cloud_*` handlers move into this module and their subparsers
    register here too."""
    cli_ttc = sub.add_parser(
        "cloud-ttcode",
        help="Throughtek P2P NAT-traversal codes for cloud camera "
             "streaming (gated 403 for non-Handy/Connect sessions — "
             "surfaces the gate cleanly).")
    cli_ttc.add_argument("serial", help="Printer serial / device ID")
    cli_ttc.add_argument("--json", action="store_true")
    cli_ttc.set_defaults(fn=cmd_cloud_ttcode)
