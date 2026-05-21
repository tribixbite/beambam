"""beambam.cloud_data — Bambu Cloud query CLIs.

Three subcommands that query Bambu's cloud APIs through the authed
session in ~/.x2d/cloud_session.json:

    beambam history             # recent print tasks
    beambam history --limit 50  # more entries
    beambam history --json      # machine output

    beambam whoami              # logged-in user identity (uid, name,
                                # handle, follower/follow counts, avatar)
    beambam whoami --json

Older versions of this idea included a `beambam profiles` for
filament/process/printer presets, but the `/v1/iot-service/api/user/
preset` endpoint 404s for accounts after the 2026 MakerWorld backend
rewrite. Filament presets ship inside each design's instance metadata
now — use `beambam cloud-fetch --instances <design_id>` to read them.

All cloud calls require `beambam cloud-login` first.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def fetch_history(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent Bambu Cloud print tasks (delegates to cloud_fetch)."""
    from beambam.cloud_fetch import fetch_user_tasks
    return fetch_user_tasks(limit=limit)


def format_history(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return ("no recent cloud tasks for this account (the list may be\n"
                "empty if you only print over LAN, or your account region\n"
                "differs from the one in ~/.x2d/cloud_session.json)")
    lines = [f"{len(tasks)} recent task(s) (most recent first):"]
    lines.append(f"  {'ID':<14} {'STATUS':<10} {'WEIGHT':<8} {'DESIGN':<10} TITLE")
    for t in tasks:
        weight = t.get("weight") or 0
        weight_str = f"{weight:.1f}g" if weight else "—"
        design = t.get("designId") or ""
        title = (t.get("title") or t.get("subtaskName")
                 or t.get("subtask_name") or "<untitled>")[:60]
        lines.append(f"  {str(t.get('id', ''))[:14]:<14} "
                     f"{str(t.get('status', '?'))[:10]:<10} "
                     f"{weight_str:<8} {str(design)[:10]:<10} {title}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


def fetch_whoami() -> dict[str, Any]:
    """Return user identity (uid, name, handle, avatar, fan + follow counts)."""
    from cloud_client import CloudClient
    c = CloudClient.load_or_anonymous()
    return c._authed_get("/v1/design-user-service/my/profile")


def format_whoami(profile: dict[str, Any]) -> str:
    lines = [
        f"uid:        {profile.get('uid')}",
        f"name:       {profile.get('name')}",
        f"handle:     @{profile.get('handle', '')}",
        f"account:    {profile.get('account')}",
        f"avatar:     {profile.get('avatar', '')}",
        f"followers:  {profile.get('fanCount', 0)}",
        f"following:  {profile.get('followCount', 0)}",
    ]
    if profile.get("bio"):
        lines.append(f"bio:        {profile['bio']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register both `beambam history` and `beambam whoami`."""
    h = sub.add_parser(
        "history",
        help="List recent Bambu Cloud print tasks (requires cloud-login).",
    )
    h.add_argument("--limit", type=int, default=20,
                   help="Max entries to fetch (default 20, max 100)")
    h.add_argument("--json", dest="json_out", action="store_true",
                   help="Machine output (JSON)")
    h.set_defaults(fn=cmd_history)

    w = sub.add_parser(
        "whoami",
        help="Show the logged-in Bambu Cloud user (uid, name, handle, ...).",
    )
    w.add_argument("--json", dest="json_out", action="store_true",
                   help="Machine output (JSON)")
    w.set_defaults(fn=cmd_whoami)


def cmd_history(args: argparse.Namespace) -> int:
    try:
        tasks = fetch_history(limit=min(args.limit, 100))
    except Exception as e:                                  # noqa: BLE001
        print(f"history failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(tasks, indent=2))
    else:
        print(format_history(tasks))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    try:
        profile = fetch_whoami()
    except Exception as e:                                  # noqa: BLE001
        print(f"whoami failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(profile, indent=2))
    else:
        print(format_whoami(profile))
    return 0
