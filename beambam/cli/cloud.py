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
            for item in v[:5]:
                if isinstance(item, dict):
                    label = (item.get("name") or item.get("key")
                             or str(item)[:60])
                    print(f"    - {label}")
        else:
            print(f"  {k}: {str(v)[:80]}")
    return 0


def cmd_cloud_history(args: argparse.Namespace) -> int:
    """List Bambu cloud's record of every print task for this account."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    try:
        tasks = cli.get_user_tasks(limit=int(args.limit))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(tasks, indent=2, default=str)); return 0
    if not tasks:
        print("(no tasks)"); return 0
    print(f"{len(tasks)} task(s):\n")
    for t in tasks:
        try:
            start = int(t.get("startTime") or 0)
            end = int(t.get("endTime") or 0)
            dur = (end - start) // 60 if end else 0
        except (TypeError, ValueError):
            dur = 0
        status = {2: "OK", 3: "Cancel", 4: "Failed"}.get(
            t.get("status"), str(t.get("status")))
        print(f"  {str(t.get('id','')):<11} {status:<7} "
              f"{str(t.get('deviceId','')):<18} {dur:>5}m  "
              f"{str(t.get('designTitle',''))[:40]:<40} "
              f"(designId={t.get('designId')})")
    return 0


def cmd_cloud_task(args: argparse.Namespace) -> int:
    """Fetch full metadata for one cloud print task (by numeric ID)."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        t = cli.get_task(args.task_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    print(json.dumps(t, indent=2, default=str))
    return 0


def cmd_cloud_messages(args: argparse.Namespace) -> int:
    """Show Bambu's notification inbox counts + optionally the messages."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    msgs: dict = {}
    try:
        counts = cli.get_message_count()
        if args.list:
            msgs = cli.get_messages(limit=int(args.limit))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        out: dict = {"counts": counts}
        if args.list:
            out["messages"] = msgs
        print(json.dumps(out, indent=2, default=str)); return 0
    nonzero = {k: v for k, v in counts.items()
               if isinstance(v, (int, float)) and v}
    print("Notification counts:")
    for k, v in nonzero.items():
        print(f"  {k:<20} {v}")
    if args.list:
        print(f"\n{len(msgs.get('hits') or [])} recent message(s):")
        for m in (msgs.get("hits") or [])[:int(args.limit)]:
            tm = m.get("taskMessage") or {}
            title = tm.get("title") or m.get("title") or ""
            print(f"  id={m.get('id'):<10} type={m.get('type'):<3}  "
                  f"{title[:60]}")
    return 0


def cmd_cloud_tickets(args: argparse.Namespace) -> int:
    """Show Bambu customer-support ticket history for this account."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_trouble_tickets(limit=int(args.limit))
        unread_t = cli.get_trouble_unread_count()
        unread_mw = cli.get_makerworld_unread_count()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps({
            "tickets": r,
            "unread_trouble": unread_t,
            "unread_makerworld": unread_mw}, indent=2, default=str))
        return 0
    print(f"unread: trouble={unread_t}  makerworld={unread_mw}")
    print(f"\n{r.get('total', 0)} ticket(s):")
    for t in (r.get("hits") or []):
        cls = (t.get("classification") or {}).get("name", "")
        print(f"  {t.get('troubleId'):<18} dev={t.get('deviceId'):<18}  {cls}")
    return 0


def cmd_cloud_firmware(args: argparse.Namespace) -> int:
    """Per-device firmware version + every available upgrade."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        devs = cli.get_device_firmware_versions()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(devs, indent=2, default=str)); return 0
    for d in devs:
        print(f"\n{d.get('dev_id'):<20} current: {d.get('version')}")
        for fw in (d.get("firmware") or []):
            forced = " (force_update)" if fw.get("force_update") else ""
            print(f"   available: {fw.get('version')}{forced}  "
                  f"{fw.get('description', '')[:60]}")
    return 0


def cmd_cloud_filaments(args: argparse.Namespace) -> int:
    """User's spool / filament inventory (AMS-RFID-detected + manual)."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        rows = cli.get_filament_inventory()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(rows, indent=2, default=str)); return 0
    print(f"{len(rows)} spool(s):")
    for f in rows:
        print(f"  {f.get('createType','?'):<6} "
              f"{f.get('filamentVendor',''):<10} "
              f"{f.get('filamentType',''):<10} "
              f"{f.get('filamentName',''):<25} "
              f"id={f.get('filamentId','')}  RFID={f.get('RFID','')[:20]}")
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
    cloud-logout / cloud-search-suggest / cloud-app-config / cloud-ttcode
    / cloud-history / cloud-task / cloud-messages / cloud-tickets /
    cloud-firmware / cloud-filaments. The rest of the `cmd_cloud_*`
    family migrates here as Phase 5b progresses."""
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

    cli_hist = sub.add_parser(
        "cloud-history",
        help="List Bambu's full server-side print-task history for this "
             "account (typically 90+ records vs ~17 days of FCM cache).")
    cli_hist.add_argument("--limit", type=int, default=20)
    cli_hist.add_argument("--json", action="store_true")
    cli_hist.set_defaults(fn=cmd_cloud_history)

    cli_task = sub.add_parser(
        "cloud-task",
        help="Fetch one cloud print task by ID — returns signed S3 URLs "
             "to the .gcode.3mf project file, plate JSONs, configs, and "
             "the finish-snapshot JPG.")
    cli_task.add_argument("task_id", type=int)
    cli_task.set_defaults(fn=cmd_cloud_task)

    cli_msgs = sub.add_parser(
        "cloud-messages",
        help="Notification-inbox counts + optional message list.")
    cli_msgs.add_argument("--list", action="store_true",
                          help="Also pull message list")
    cli_msgs.add_argument("--limit", type=int, default=10)
    cli_msgs.add_argument("--json", action="store_true")
    cli_msgs.set_defaults(fn=cmd_cloud_messages)

    cli_tix = sub.add_parser(
        "cloud-tickets",
        help="Customer-support ticket history + unread counts.")
    cli_tix.add_argument("--limit", type=int, default=10)
    cli_tix.add_argument("--json", action="store_true")
    cli_tix.set_defaults(fn=cmd_cloud_tickets)

    cli_fw = sub.add_parser(
        "cloud-firmware",
        help="Current firmware version + every available upgrade for "
             "each bound device.")
    cli_fw.add_argument("--json", action="store_true")
    cli_fw.set_defaults(fn=cmd_cloud_firmware)

    cli_fil = sub.add_parser(
        "cloud-filaments",
        help="User's spool / filament inventory (AMS-RFID + manual entries).")
    cli_fil.add_argument("--json", action="store_true")
    cli_fil.set_defaults(fn=cmd_cloud_filaments)
