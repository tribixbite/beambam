"""beambam.cli.cloud — cloud-* CLI handlers.

Phase 5b migration target. Handlers move out of x2d_bridge.py one
batch at a time; x2d_bridge.py re-exports each name so tests + external
callers (`from x2d_bridge import cmd_cloud_*`) keep working.

Currently owns:
  cmd_cloud_ttcode          P2P TUTK creds (gated 403 surface)
  cmd_cloud_logout          clear ~/.x2d/cloud_session.json
  cmd_cloud_search_suggest  personalized search-bar suggestions
  cmd_cloud_app_config      global feature-flag manifest
"""
from __future__ import annotations

import argparse
import json
import sys


def cmd_cloud_logout(_args: argparse.Namespace) -> int:
    """Clear the persisted Bambu Cloud session."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    cli.logout()
    print("session cleared")
    return 0


def cmd_cloud_search_suggest(_args: argparse.Namespace) -> int:
    """Personalized search-bar suggestions (reflects what the user has
    searched for + popular terms). Exposes your search interests."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    sugs = cli.get_search_suggestions()
    print("\n".join(sugs))
    return 0


def cmd_cloud_app_config(args: argparse.Namespace) -> int:
    """Global app feature-flag manifest."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_app_configuration()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    for k, v in r.items():
        if isinstance(v, list):
            print(f"  {k} ({len(v)} entries)")
            for e in v[:5]:
                if isinstance(e, dict):
                    label = e.get("name") or e.get("key") or str(e)[:60]
                    print(f"    - {label}")
        else:
            print(f"  {k}: {str(v)[:80]}")
    return 0


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
    """Register the cloud-* subparsers this module owns. Currently:
    cloud-logout / cloud-search-suggest / cloud-app-config /
    cloud-ttcode. The rest of the `cmd_cloud_*` family migrates here
    as Phase 5b progresses."""
    cli_logout = sub.add_parser(
        "cloud-logout",
        help="Clear ~/.x2d/cloud_session.json (forces re-login).")
    cli_logout.set_defaults(fn=cmd_cloud_logout)

    cli_sug = sub.add_parser(
        "cloud-search-suggest",
        help="Personalized MakerWorld search-bar suggestions.")
    cli_sug.set_defaults(fn=cmd_cloud_search_suggest)

    cli_cfg = sub.add_parser(
        "cloud-app-config",
        help="Global app feature-flag manifest — exposes pre-release "
             "feature flags.")
    cli_cfg.add_argument("--json", action="store_true")
    cli_cfg.set_defaults(fn=cmd_cloud_app_config)

    cli_ttc = sub.add_parser(
        "cloud-ttcode",
        help="Throughtek P2P NAT-traversal codes for cloud camera "
             "streaming (gated 403 for non-Handy/Connect sessions — "
             "surfaces the gate cleanly).")
    cli_ttc.add_argument("serial", help="Printer serial / device ID")
    cli_ttc.add_argument("--json", action="store_true")
    cli_ttc.set_defaults(fn=cmd_cloud_ttcode)
