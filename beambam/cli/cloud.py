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


def _format_design_hits(hits: list, header: str = "") -> None:
    """Pretty-print a list of MakerWorld design hits. Used by search,
    browse-by-nav, favorites, liked."""
    if header:
        print(header)
    if not hits:
        print("  (none)"); return
    for h in hits:
        d = h.get("design") or h
        did = str(d.get("id") or d.get("designId") or "")
        title = str(d.get("title") or d.get("designTitle") or "")
        creator = ((d.get("designCreator") or {}).get("name", "")
                   or d.get("creatorName", ""))
        likes = int(d.get("likeCount") or d.get("likes") or 0)
        downloads = int(d.get("downloadCount") or d.get("downloads") or 0)
        print(f"  {did:<8} likes={likes:<5} dls={downloads:<6}  "
              f"by {creator[:18]:<18}  {title[:50]}")


def cmd_cloud_search(args: argparse.Namespace) -> int:
    """MakerWorld full-text search."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.search_designs(args.query, limit=int(args.limit),
                                offset=int(args.offset))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    _format_design_hits(r.get("hits") or [],
                        header=f"{r.get('total', 0)} match(es) for "
                               f"{args.query!r}:")
    return 0


def cmd_cloud_browse(args: argparse.Namespace) -> int:
    """Browse MakerWorld by nav key (Trending / Foryou / Household / …)."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.browse_designs_by_nav(args.nav, limit=int(args.limit),
                                       offset=int(args.offset))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    _format_design_hits(r.get("hits") or [],
                        header=f"{r.get('total', 0)} designs in "
                               f"nav={args.nav!r}:")
    return 0


def cmd_cloud_design(args: argparse.Namespace) -> int:
    """Full design record for a MakerWorld design ID."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_design(args.design_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    print(f"Design ID    : {r.get('id')}")
    print(f"Title        : {r.get('title')}")
    print(f"Slug         : {r.get('slug')}")
    creator = r.get("designCreator") or {}
    print(f"Creator      : {creator.get('name')} (uid {creator.get('uid')})")
    print(f"Stats        : likes={r.get('likeCount', 0)}  "
          f"dls={r.get('downloadCount', 0)}  "
          f"collections={r.get('collectionCount', 0)}  "
          f"comments={r.get('commentCount', 0)}")
    instances = r.get("instances") or []
    print(f"Instances    : {len(instances)}")
    for inst in instances[:5]:
        print(f"  - id={inst.get('id')} title={inst.get('title',''):<40} "
              f"configs={len(inst.get('configs') or [])}")
    return 0


def cmd_cloud_design_remixes(args: argparse.Namespace) -> int:
    """Remix tree of a MakerWorld design."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_design_remixes(args.design_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    _format_design_hits(r.get("hits") or [],
                        header=f"{r.get('total', 0)} remix(es) of "
                               f"design {args.design_id}:")
    return 0


def cmd_cloud_favorites(args: argparse.Namespace) -> int:
    """User's MakerWorld favorites lists."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_favorites()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    lists = r.get("hits") or []
    print(f"{len(lists)} favorite list(s):")
    for fl in lists:
        print(f"  id={fl.get('id'):<10} {str(fl.get('title','')):<30}  "
              f"status={fl.get('status')}  "
              f"covers={len(fl.get('designCover') or [])}")
    return 0


def cmd_cloud_liked(args: argparse.Namespace) -> int:
    """Designs the user has liked."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_liked_designs()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    _format_design_hits(r.get("hits") or [],
                        header=f"{r.get('total', 0)} liked design(s):")
    return 0


def cmd_cloud_presets(args: argparse.Namespace) -> int:
    """User's cloud-synced slicer presets (print / filament / printer)."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_slicer_presets(version=args.version,
                                    public=bool(args.public))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    for kind in ("print", "filament", "printer"):
        block = r.get(kind) or {}
        for visibility in ("private", "public"):
            items = block.get(visibility) or []
            if items:
                print(f"\n{kind} / {visibility}: {len(items)}")
                for it in items[:20]:
                    print(f"  {str(it.get('setting_id',''))[:24]:<24}  "
                          f"v{it.get('version','?')}  "
                          f"{str(it.get('name',''))[:40]}")
    return 0


def cmd_cloud_feed(args: argparse.Namespace) -> int:
    """MakerWorld 'For You' recommendation feed."""
    import random
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    seed = args.seed if args.seed else random.randint(1, 2**31 - 1)
    try:
        r = cli.get_for_you(seed=seed, limit=int(args.limit))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    hits = r.get("hits") or r.get("designs") or []
    print(f"{len(hits)} recommendation(s) (seed={seed}):")
    for h in hits:
        d = h.get("design") or h
        did = str(d.get("id") or d.get("designId") or "")
        title = str(d.get("title") or d.get("designTitle") or "")
        likes = int(d.get("likeCount") or d.get("likes") or 0)
        downloads = int(d.get("downloadCount") or d.get("downloads") or 0)
        creator = (d.get("designCreator") or {}).get("name", "")
        print(f"  {did:<8} likes={likes:<5} dls={downloads:<5}  "
              f"by {creator[:18]:<18}  {title[:50]}")
    return 0


def cmd_cloud_like(args: argparse.Namespace) -> int:
    """Toggle the like-state on a MakerWorld design.

    Endpoint is idempotent-toggle: first POST likes, second un-likes.
    Bambu returns empty 200 on success."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        cli.toggle_design_like(args.design_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    print(f"design {args.design_id}: like toggle accepted by server "
          f"(toggle is idempotent — call again to reverse)")
    return 0


def cmd_cloud_comments(args: argparse.Namespace) -> int:
    """Pull MakerWorld comments + ratings for a design."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.get_design_comments(args.design_id,
                                    limit=int(args.limit),
                                    offset=int(args.offset))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    hits = r.get("hits") or []
    comments = [h for h in hits if h.get("type") == 1 and h.get("comment")]
    print(f"{r.get('total', 0)} interaction(s); showing "
          f"{len(comments)} comment(s):\n")
    for h in comments:
        c = h["comment"]
        user = (c.get("user") or {}).get("name", "?")
        when = (c.get("createTime") or "")[:10]
        likes = c.get("likeCount", 0)
        replies = c.get("replyCount", 0)
        content = (c.get("content") or "").replace("\n", " ")
        print(f"  {when}  likes={likes:<3} replies={replies:<3}  "
              f"{user[:18]:<18}: {content[:80]}")
    return 0


def cmd_cloud_comment_reply(args: argparse.Namespace) -> int:
    """Reply to a MakerWorld comment by its numeric ID."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        r = cli.reply_to_comment(args.comment_id, args.text)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    reply_id = r.get("id") or r.get("commentId") or "?"
    print(f"replied to comment {args.comment_id} (reply id={reply_id})")
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

    cli_search = sub.add_parser(
        "cloud-search",
        help="Full-text search MakerWorld designs by query string.")
    cli_search.add_argument("query", help="search query string")
    cli_search.add_argument("--limit", type=int, default=20)
    cli_search.add_argument("--offset", type=int, default=0)
    cli_search.add_argument("--json", action="store_true")
    cli_search.set_defaults(fn=cmd_cloud_search)

    cli_browse = sub.add_parser(
        "cloud-browse",
        help="Browse MakerWorld designs by nav key (Trending / Foryou / "
             "Household / etc.).")
    cli_browse.add_argument("nav",
                             help="nav key (use `cloud-search-suggest` or "
                                  "`/v1/search-service/homepage/nav` to "
                                  "discover)")
    cli_browse.add_argument("--limit", type=int, default=20)
    cli_browse.add_argument("--offset", type=int, default=0)
    cli_browse.add_argument("--json", action="store_true")
    cli_browse.set_defaults(fn=cmd_cloud_browse)

    cli_design = sub.add_parser(
        "cloud-design",
        help="Show full record for a MakerWorld design ID (title, "
             "creator, instances, like/download counts, signed S3 URL "
             "to its .3mf bundle).")
    cli_design.add_argument("design_id", type=int)
    cli_design.add_argument("--json", action="store_true")
    cli_design.set_defaults(fn=cmd_cloud_design)

    cli_remix = sub.add_parser(
        "cloud-design-remixes",
        help="List remixes (derivative works) of a MakerWorld design.")
    cli_remix.add_argument("design_id", type=int)
    cli_remix.add_argument("--json", action="store_true")
    cli_remix.set_defaults(fn=cmd_cloud_design_remixes)

    cli_fav = sub.add_parser(
        "cloud-favorites",
        help="User's MakerWorld favorites lists.")
    cli_fav.add_argument("--json", action="store_true")
    cli_fav.set_defaults(fn=cmd_cloud_favorites)

    cli_lk = sub.add_parser(
        "cloud-liked",
        help="Designs the user has liked on MakerWorld.")
    cli_lk.add_argument("--json", action="store_true")
    cli_lk.set_defaults(fn=cmd_cloud_liked)

    cli_pre = sub.add_parser(
        "cloud-presets",
        help="User's cloud-synced slicer presets (print / filament / "
             "printer profiles).")
    cli_pre.add_argument("--version", default="01.10.00.69",
                          help="Slicer version to use as the version filter")
    cli_pre.add_argument("--public", action="store_true",
                          help="Include Bambu's shipped public presets too")
    cli_pre.add_argument("--json", action="store_true")
    cli_pre.set_defaults(fn=cmd_cloud_presets)

    cli_feed = sub.add_parser(
        "cloud-feed",
        help="MakerWorld 'For You' recommendations for this user.")
    cli_feed.add_argument("--seed", type=int, default=0,
                           help="Pagination seed (0 = random new page).")
    cli_feed.add_argument("--limit", type=int, default=10)
    cli_feed.add_argument("--json", action="store_true")
    cli_feed.set_defaults(fn=cmd_cloud_feed)

    cli_like = sub.add_parser(
        "cloud-like",
        help="Toggle like-state on a MakerWorld design.")
    cli_like.add_argument("design_id", type=int)
    cli_like.set_defaults(fn=cmd_cloud_like)

    cli_com = sub.add_parser(
        "cloud-comments",
        help="MakerWorld comments + ratings for a design.")
    cli_com.add_argument("design_id", type=int)
    cli_com.add_argument("--limit", type=int, default=20)
    cli_com.add_argument("--offset", type=int, default=0)
    cli_com.add_argument("--json", action="store_true")
    cli_com.set_defaults(fn=cmd_cloud_comments)

    cli_reply = sub.add_parser(
        "cloud-comment-reply",
        help="Reply to a MakerWorld comment (POST "
             "/v1/comment-service/comment/<id>/reply).")
    cli_reply.add_argument("comment_id", type=int,
                            help="Numeric ID of the comment to reply to")
    cli_reply.add_argument("text",
                            help="Reply body. Pass via shell-quoted string.")
    cli_reply.add_argument("--json", action="store_true",
                            help="Emit the new reply record as JSON")
    cli_reply.set_defaults(fn=cmd_cloud_comment_reply)
