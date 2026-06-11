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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt


def cmd_cloud_logout(_args: argparse.Namespace) -> int:
    """Clear the persisted Bambu Cloud session."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    cli.logout()
    print("session cleared")
    return 0


def cmd_cloud_login(args: argparse.Namespace) -> int:
    """Authenticate to Bambu Cloud (`/v1/user-service/user/login`).

    Three flows:
      * `--dry-run` — connectivity probe only, no creds sent.
      * `--code-only` — device-code path: skip password, prove inbox
        possession via an emailed 6-digit code. Right path for users
        who don't want to paste a Bambu password into a terminal.
      * standard email + password — supports interactive 2FA + email
        verification prompts when the account requires them.

    After successful login (in any of the non-dry-run flows) the
    handler auto-bootstraps `~/.x2d/credentials` with one
    `[printer:<name>]` section per bound printer, populated with the
    LAN access code resolved via cmd_cloud_get_access_code (skip with
    `--no-bootstrap`).
    """
    import getpass
    import os
    import time
    import cloud_client
    from pathlib import Path

    if args.dry_run:
        # Probe-only mode: confirm the cloud endpoint is reachable
        # without sending credentials. Useful for CI and install-time
        # smoke tests against networks that may block Bambu's API.
        region = args.region or "us"
        result = cloud_client.CloudClient.dry_run_check(region=region)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    # Allow email/password from CLI, env, or interactive stdin (handy
    # when the user doesn't want creds in shell history).
    email = args.email or os.environ.get("BAMBU_EMAIL", "")
    password = args.password or os.environ.get("BAMBU_PASSWORD", "")
    if not email:
        try:
            email = input("Bambu account email: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 1

    # --code-only ("device code" style) flow: skip the password
    # entirely. Bambu emails a 6-digit code that proves possession
    # of the inbox. Right path for fresh-OS uvx users who don't want
    # to paste a Bambu password into a terminal they may not fully
    # trust.
    if getattr(args, "code_only", False):
        def _prompt_for_code(addr: str) -> str:
            if getattr(args, "email_code", None):
                return args.email_code.strip()
            print(f"\nA verification code was emailed to {addr}. "
                  f"Enter it:")
            return input("Email code: ").strip()
        cli = cloud_client.CloudClient.load_or_anonymous()
        try:
            cli.login_code_only(email, region=args.region,
                                 code_resolver=_prompt_for_code)
        except cloud_client.CloudError as e:
            print(f"cloud-login (code-only) failed: {e}",
                  file=sys.stderr)
            return 1
        print(f"logged in (code-only) as user {cli.session.user_id} "
              f"(region={cli.session.region}); session saved.")
        return 0

    if not password:
        try:
            password = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 1
    if not email or not password:
        print("email and password are required "
              "(or use --code-only)", file=sys.stderr)
        return 2

    def prompt_tfa(_email: str) -> str:
        if getattr(args, "tfa_code", None):
            return args.tfa_code.strip()
        print(f"\nThis account requires 2FA. Open your authenticator "
              f"app, then enter the 6-digit code:")
        return input("2FA code: ").strip()

    def prompt_email_code(_email: str) -> str:
        if getattr(args, "email_code", None):
            return args.email_code.strip()
        print(f"\nA verification code was emailed to {_email}. "
              f"Enter it:")
        return input("Email code: ").strip()

    cli = cloud_client.CloudClient.load_or_anonymous()
    try:
        cli.login(email, password, region=args.region,
                  two_factor_resolver=prompt_tfa,
                  email_code_resolver=prompt_email_code)
    except cloud_client.CloudError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    expires_in = int(max(0, cli.session.expires_at - time.time()))
    expires_str = time.strftime(
        '%Y-%m-%d %H:%M:%S',
        time.localtime(cli.session.expires_at))
    print(f"logged in as user_id={cli.session.user_id or '?'} "
          f"(region={cli.session.region}, "
          f"expires_at={expires_str}, "
          f"valid for {expires_in // 86400}d "
          f"{(expires_in % 86400) // 3600}h)")
    print(f"session saved to {cloud_client.SESSION_PATH}")

    # Auto-bootstrap ~/.x2d/credentials with every bound printer's
    # LAN access code unless explicitly disabled. Mirrors what
    # BambuStudio does after first cloud-bind: pulls dev_id+ip from
    # the bound-devices REST endpoint and the LAN code via the
    # `system.get_access_code` cloud-MQTT roundtrip. End state:
    # subsequent `lan_print.py` / `x2d_bridge.py print` commands
    # work with no extra flags.
    if getattr(args, "no_bootstrap", False):
        return 0
    try:
        devices = cli.get_bound_devices() or []
    except Exception as e:
        print(f"[bootstrap] couldn't list bound printers: {e} "
              "— skipping credential auto-write", file=sys.stderr)
        return 0
    if not devices:
        print("[bootstrap] no printers bound to this account — "
              "nothing to write")
        return 0
    print(f"[bootstrap] found {len(devices)} printer(s) — pulling "
          f"LAN access codes")
    # cmd_cloud_get_access_code lives in the same module (Phase 5b
    # batch 9 brought it over from x2d_bridge), so this is a direct
    # call now — the earlier lazy `from x2d_bridge import ...` thunk
    # is gone.
    bootstrap_count = 0
    for dev in devices:
        serial = dev.get("dev_id") or dev.get("device_id") or ""
        ip = dev.get("dev_ip") or dev.get("ip") or ""
        if not serial:
            continue
        # Reuse cmd_cloud_get_access_code's logic by faking an args
        # namespace. `argparse.Namespace` accepts arbitrary kwargs as
        # attributes — same effect as the older empty-class trick,
        # but typed (mypy can see the fields).
        gac_args = argparse.Namespace(
            serial=serial,
            timeout=10.0,
            persist=True,
            ip=ip,
            section="",  # default to printer:<serial>
        )
        try:
            rc = cmd_cloud_get_access_code(gac_args)
            if rc == 0:
                bootstrap_count += 1
        except Exception as e:
            print(f"[bootstrap] {serial}: {e}", file=sys.stderr)
    print(f"[bootstrap] wrote {bootstrap_count}/{len(devices)} "
          f"printer section(s) to "
          f"{Path.home() / '.x2d' / 'credentials'}")
    return 0


def cmd_cloud_printers(args: argparse.Namespace) -> int:
    """List the printers bound to the logged-in Bambu account."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    try:
        devices = cli.get_bound_devices()
    except cloud_client.CloudError as e:
        print(f"cloud API call failed: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(devices, indent=2))
    else:
        if not devices:
            print("(no printers bound to this account)")
            return 0
        print(f"{len(devices)} printer(s) bound to user "
              f"{cli.session.user_id}:")
        for d in devices:
            online = "online " if d.get("online") else "offline"
            name = d.get("name") or d.get("dev_name") or "?"
            dev_id = d.get("dev_id") or d.get("device_id") or "?"
            model = (d.get("dev_product_name")
                     or d.get("dev_model_name") or "?")
            access_code = (d.get("dev_access_code") or "").strip()
            print(f"  [{online}] {name}  serial={dev_id}  "
                  f"model={model}  "
                  f"access_code={access_code or '(hidden)'}")
    return 0


def cmd_cloud_status(_args: argparse.Namespace) -> int:
    """Dump the cached Bambu Cloud session (user_id, region, JWT
    expiry). Useful for scripting + login-status checks. Output
    always JSON — same shape as the live-printer cmd_status."""
    import time
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in (no ~/.x2d/cloud_session.json)")
        return 0
    age_s = max(0, cli.session.expires_at - time.time())
    print(json.dumps({
        "logged_in":    True,
        "user_id":      cli.session.user_id,
        "region":       cli.session.region,
        "expired":      cli.session.expired,
        "expires_at":   cli.session.expires_at,
        "expires_in_s": int(age_s),
    }, indent=2))
    return 0


# ----- cloud-spool CRUD ------------------------------------------------


def _spool_body_from_args(args: argparse.Namespace) -> dict:
    """Distil --vendor/--type/--name/--id/--color/--weight into the
    spool-record dict Bambu's API expects. Skips None fields so the
    server only sees what the user explicitly passed (matters for
    UPDATE where you might want to change just one attribute)."""
    raw = {
        "filamentVendor": args.vendor,
        "filamentType":   args.type,
        "filamentName":   args.name,
        "filamentId":     args.filament_id,
        "color":          args.color,
        "weight":         args.weight,
        # WRITE-side defaults to manual entries
        "createType":     "manual",
    }
    return {k: v for k, v in raw.items() if v is not None}


def _require_allow_write(args: argparse.Namespace, what: str) -> int | None:
    """Guard that flips write-side cloud ops behind `--allow-write`.
    Returns an exit code to bubble up, or None if the caller should
    proceed."""
    if not getattr(args, "allow_write", False):
        print(
            f"refusing to {what} without --allow-write\n"
            f"This mutates account-side state on Bambu Cloud — re-run "
            f"with --allow-write to confirm.",
            file=sys.stderr)
        return 1
    return None


def cmd_cloud_spool_add(args: argparse.Namespace) -> int:
    """`cloud-spool add` — POST a new spool entry."""
    import cloud_client
    rc = _require_allow_write(args, "add a spool")
    if rc is not None:
        return rc
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr)
        return 1
    body = _spool_body_from_args(args)
    try:
        r = cli.add_spool(body)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    print(f"added spool: {body}")
    return 0


def cmd_cloud_spool_update(args: argparse.Namespace) -> int:
    """`cloud-spool update <filamentId>` — PUT a partial update."""
    import cloud_client
    rc = _require_allow_write(args, f"update spool {args.filament_id}")
    if rc is not None:
        return rc
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr)
        return 1
    body = _spool_body_from_args(args)
    # path-segment, not body field for PUT
    body.pop("filamentId", None)
    # leave the existing createType alone
    body.pop("createType", None)
    if not body:
        print("nothing to update — pass at least one of "
              "--vendor / --type / --name / --color / --weight",
              file=sys.stderr)
        return 2
    try:
        r = cli.update_spool(args.filament_id, body)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    print(f"updated spool {args.filament_id}: {body}")
    return 0


def cmd_cloud_spool_delete(args: argparse.Namespace) -> int:
    """`cloud-spool delete <filamentId>` — DELETE one entry."""
    import cloud_client
    rc = _require_allow_write(args, f"delete spool {args.filament_id}")
    if rc is not None:
        return rc
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr)
        return 1
    try:
        cli.delete_spool(args.filament_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr)
        return 1
    print(f"deleted spool {args.filament_id}")
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


def cmd_cloud_profile(args: argparse.Namespace) -> int:
    """Show the logged-in user's MakerWorld profile — uid, handle, name,
    avatar, bio, follow + design counts."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        prof = cli.get_my_profile()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(prof, indent=2, default=str)); return 0
    # Pretty-print the common fields, gracefully degrading when Bambu
    # renames keys (they have before).
    fields = [
        ("uid",          "uid"),
        ("handle",       "handle"),
        ("name",         "name"),
        ("bio",          "bio"),
        ("designs",      "designCount"),
        ("followers",    "fanCount"),
        ("following",    "followCount"),
        ("likes",        "likeCount"),
    ]
    for label, key in fields:
        if key in prof:
            print(f"  {label:<10} {prof[key]}")
    return 0


def cmd_cloud_points(args: argparse.Namespace) -> int:
    """Show Bambu gamification points / progress breakdown."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        progress = cli.get_points_progress()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(progress, indent=2, default=str)); return 0
    # Bambu's payload shape isn't fully documented; dump every leaf.
    for k, v in progress.items():
        if isinstance(v, (dict, list)):
            print(f"  {k}: {json.dumps(v, default=str)[:120]}")
        else:
            print(f"  {k:<24} {v}")
    return 0


def cmd_cloud_unread(args: argparse.Namespace) -> int:
    """Show unread-message counts (aftersale tickets + MakerWorld
    notifications)."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        trouble = cli.get_trouble_unread_count()
        makerworld = cli.get_makerworld_unread_count()
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    counts = {"aftersale_tickets": trouble, "makerworld": makerworld,
              "total": trouble + makerworld}
    if args.json:
        print(json.dumps(counts, indent=2)); return 0
    for k, v in counts.items():
        print(f"  {k:<20} {v}")
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


# ----- Cloud-MQTT helpers (Phase 5b batch 7) --------------------------
#
# Cloud-mediated MQTT (#67) uses the logged-in JWT to talk to Bambu's
# cloud broker (us.mqtt.bambulab.com:8883). Sidesteps the LAN-direct
# `print.*` verify-failure (#65/#66/#68) entirely because the cloud
# broker accepts plain JWT-authed sessions; per-installation cert is
# never invoked.


def _cloud_mqtt_connect(serial: str, cli) -> mqtt.Client:
    """Connect to Bambu's cloud broker using the logged-in JWT.
    Returns a paho.mqtt.client.Client connected + ready to subscribe
    or publish. Caller is responsible for client.loop_stop() +
    disconnect() on exit. The paho dependency is imported lazily in
    the function body to keep cloud.py importable without paho on
    the path; the module-level TYPE_CHECKING block lets mypy see the
    real `mqtt.Client` type."""
    import os
    import ssl
    import time
    import cloud_client
    import paho.mqtt.client as mqtt
    user, pwd = cli.mqtt_credentials()
    host = cli.mqtt_broker()
    client_id = f"x2d-bridge-{os.getpid()}-{int(time.time())}"
    c = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
    )
    c.username_pw_set(user, pwd)
    # Standard TLS — Bambu's brokers serve Let's-Encrypt-rooted certs,
    # so the system trust store is sufficient. No per-installation cert.
    c.tls_set_context(ssl.create_default_context())
    c.connect(host, cloud_client.MQTT_PORT, keepalive=60)
    return c


def _cloud_publish_payload(serial: str, payload: dict,
                            timeout: float = 10.0) -> int:
    """Internal helper used by every cloud-side print-control CLI.
    Connects to Bambu's cloud broker, publishes one message, exits.
    Returns 0 on broker ack, 1 on error."""
    import threading
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = threading.Event()
    def on_publish(client, userdata, mid,
                   reason_code=None, properties=None):
        published.set()
    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=timeout)
        if not published.wait(timeout=timeout):
            print(f"[cloud] no broker ack in {timeout}s",
                  file=sys.stderr)
            return 1
        print(json.dumps({"published": True, "topic": topic_request,
                          "payload": payload}, indent=2))
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def cloud_start_print(
    serial: str,
    *,
    gcode_filename: str,
    md5_hex: str = "",
    ams_slots: list[int] | None = None,
    bed_type: str = "auto",
    bed_temp: int = 0,
    bed_levelling: bool = True,
    flow_cali: bool = False,
    timelapse: bool = False,
    vibration_cali: bool = False,
    use_ams: bool = True,
    timeout: float = 12.0,
) -> int:
    """Publish a `print.project_file` start-print command via Bambu's
    CLOUD broker (account-JWT-authenticated) instead of LAN MQTT
    (which the X2D firmware allowlist rejects with "mqtt message verify
    failed" for the leaked Bambu Connect cert as of fw 01.01.00.65).

    Payload shape mirrors `beambam.print_job.start_print` (per the
    captured BS-Windows wire format). Differences for cloud route:
      * `job_type = 1` (not 0)
      * Published via Bambu's cloud broker with username = user JWT
      * No `header.sign_string` needed (cloud broker authorises by JWT)
    """
    import time as _time
    if ams_slots is None:
        ams_slots = [0] if use_ams else []
    ams_mapping_legacy = list(ams_slots) if use_ams else []
    ams_mapping_v2 = (
        [{"ams_id": s // 4, "slot_id": s % 4} for s in ams_slots]
        if use_ams else []
    )
    job_id_int = int(_time.time()) * 10
    job_id_str = str(job_id_int)
    name_no_3mf = gcode_filename
    if name_no_3mf.endswith(".gcode.3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")]
    elif name_no_3mf.endswith(".3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")] + ".gcode"
    payload = {
        "print": {
            "sequence_id":              str(int(_time.time())),
            "command":                  "project_file",
            "param":                    "Metadata/plate_1.gcode",
            "file":                     gcode_filename,
            "url":                      f"ftp:///{gcode_filename}",
            "md5":                      md5_hex,
            "task_id":                  job_id_str,
            "subtask_id":               job_id_str,
            "subtask_name":             name_no_3mf,
            "job_id":                   job_id_int,
            "project_id":               job_id_str,
            "profile_id":               "0",
            "design_id":                "0",
            "model_id":                 "0",
            "plate_idx":                1,
            "dev_id":                   serial,
            "job_type":                 1,                 # cloud
            "timestamp":                int(_time.time()),
            "bed_type":                 bed_type,
            "bed_temp":                 int(bed_temp),
            "auto_bed_leveling":        1 if bed_levelling else 0,
            "extrude_cali_flag":        1 if flow_cali else 0,
            "nozzle_offset_cali":       0,
            "extrude_cali_manual_mode": 0,
            "flow_cali":                bool(flow_cali),
            "bed_leveling":             bool(bed_levelling),
            "vibration_cali":           bool(vibration_cali),
            "timelapse":                bool(timelapse),
            "layer_inspect":            False,
            "use_ams":                  bool(use_ams),
            "ams_mapping":              ams_mapping_legacy,
            "ams_mapping2":             ams_mapping_v2,
            "skip_objects":             None,
            "cfg":                      "0",
        }
    }
    return _cloud_publish_payload(serial, payload, timeout=timeout)


def cmd_cloud_start_print(args: argparse.Namespace) -> int:
    """`beambam cloud-start-print <remote_filename>` — start a print of
    a file that's already on the printer's SD via the CLOUD broker.
    Use when the LAN `project_file` MQTT is firmware-gated (X2D / H2D
    on Jan-2025+ fw)."""
    serial = _resolve_serial_or_exit(args)
    slots: list[int] | None = None
    if args.ams_slots:
        try:
            slots = [int(s) for s in args.ams_slots.split(",")]
        except ValueError:
            print("--ams-slots must be CSV ints (e.g. '7,10,7')",
                  file=sys.stderr)
            return 1
    return cloud_start_print(
        serial,
        gcode_filename=args.filename,
        md5_hex=args.md5 or "",
        ams_slots=slots,
        bed_type=args.bed_type,
        bed_temp=int(args.bed_temp) if args.bed_temp else 0,
        bed_levelling=not args.no_bed_level,
        flow_cali=args.flow_cali,
        timelapse=args.timelapse,
        vibration_cali=args.vib_cali,
        use_ams=not args.no_ams,
    )


def _resolve_cloud_serial(args: argparse.Namespace) -> str | None:
    """Mirror of cmd_cloud_state's auto-discovery: --serial wins, else
    X2D_SERIAL env, else if exactly one printer is bound to the
    account use that; else None."""
    import os
    serial = (getattr(args, "serial", None)
              or os.environ.get("X2D_SERIAL"))
    if serial:
        return serial
    try:
        import cloud_client
        cli = cloud_client.CloudClient.load_or_anonymous()
        if cli.session.empty:
            return None
        devs = cli.get_bound_devices()
        if len(devs) == 1:
            return devs[0].get("dev_id") or devs[0].get("device_id")
    except Exception:
        pass
    return None


def _resolve_serial_or_exit(args: argparse.Namespace) -> str:
    """`_resolve_cloud_serial` but sys.exits when nothing is found —
    used by every cloud-control verb whose printer target is required."""
    serial = _resolve_cloud_serial(args)
    if not serial:
        sys.exit("--serial required")
    return serial


def _cloud_publish(serial: str, payload: dict,
                   timeout: float = 10.0) -> int:
    """Alias kept for handlers below (they were authored against the
    short name). Same semantics as `_cloud_publish_payload`."""
    return _cloud_publish_payload(serial, payload, timeout)


def cmd_cloud_get_access_code(args: argparse.Namespace) -> int:
    """Fetch a printer's LAN access code over cloud MQTT (no LAN needed).

    Mirrors what BambuStudio does on first cloud-bind (see
    `MachineObject::command_get_access_code` in DeviceManager.cpp:1219):
    publish `system.get_access_code` to the printer's cloud request topic
    and wait for the report that comes back with `system.access_code`
    set. Lets a fresh `cloud-login` finish setting up `~/.x2d/credentials`
    automatically — no need to copy the code off the printer's screen.

    Use --persist to also write the discovered code (and IP if --ip
    given, or whatever was already in the section) into
    ~/.x2d/credentials so subsequent LAN-direct commands work without
    flags. The serial is the section key; missing sections get created
    as `[printer:<serial>]`.
    """
    import configparser
    import threading
    from pathlib import Path
    import cloud_client
    from beambam.cli._helpers import _next_seq

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    serial = _resolve_cloud_serial(args)
    if not serial:
        print("--serial required (or bind exactly one printer to "
              "the account)", file=sys.stderr)
        return 1

    topic_report  = f"device/{serial}/report"
    topic_request = f"device/{serial}/request"
    seq = _next_seq()
    payload = {"system": {"sequence_id": seq,
                          "command": "get_access_code"}}

    got_code: dict[str, str | None] = {"value": None}
    done = threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc != 0:
            print(f"[cloud-get-access-code] MQTT connect failed "
                  f"rc={rc}", file=sys.stderr)
            return
        c.subscribe(topic_report, qos=0)
        c.publish(topic_request,
                  json.dumps(payload, separators=(",", ":")))

    def on_message(c, userdata, msg):
        try:
            j = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        sysblock = (j or {}).get("system") or {}
        # Reply payload from the firmware:
        # system.command == "get_access_code" and system.access_code
        # populated. Some firmwares also re-publish the field under
        # top-level `info` — accept both.
        code = (sysblock.get("access_code")
                or (j.get("info") or {}).get("access_code"))
        if code:
            got_code["value"] = code
            done.set()

    c = _cloud_mqtt_connect(serial, cli)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    try:
        if not done.wait(timeout=args.timeout):
            print(f"[cloud-get-access-code] timeout waiting "
                  f"{args.timeout}s for response (printer offline?)",
                  file=sys.stderr)
            return 1
    finally:
        c.loop_stop()
        c.disconnect()

    code = got_code["value"]
    print(code)

    if args.persist:
        ini_path = Path.home() / ".x2d" / "credentials"
        ini_path.parent.mkdir(parents=True, exist_ok=True)
        cp = configparser.ConfigParser()
        if ini_path.exists():
            cp.read(ini_path)
        # Section name precedence: --section > printer:<serial> > printer.
        target = args.section or f"printer:{serial}"
        if not cp.has_section(target):
            cp.add_section(target)
        cp.set(target, "code", code or "")
        cp.set(target, "serial", serial)
        if args.ip:
            cp.set(target, "ip", args.ip)
        elif not cp.has_option(target, "ip"):
            print(f"[cloud-get-access-code] no --ip given and "
                  f"{target} has no ip set — re-run with --ip "
                  f"<printer-ip> to make this section usable for "
                  f"LAN commands.", file=sys.stderr)
        with ini_path.open("w") as f:
            cp.write(f)
        print(f"[cloud-get-access-code] wrote {target} -> "
              f"{ini_path}", file=sys.stderr)
    return 0


def build_cloud_project_file(
        serial: str, upload: dict, *, now: int, slot: int = 0,
        plate: int = 1, bed_type: str = "textured_plate", bed_temp: int = 65,
        use_ams: bool = True, flow_cali: bool = False, bed_leveling: bool = True,
        vibration_cali: bool = False, timelapse: bool = False) -> dict:
    """Compose the inner ``print`` body of a cloud ``print.project_file``
    start-print command from an OSS ``upload`` dict (``url``/``md5``/
    ``remote_name``). ``now`` (epoch seconds) is injected so the output is
    deterministic/testable; it drives ``sequence_id``/``timestamp``/``job_id``.

    Used by ``cmd_cloud_print`` (CLI start), which ``cmd_start``'s
    print-next-in-queue path also routes through. The envelope
    ``sequence_id``/``timestamp`` are present for the legacy unsigned publish;
    the cloud-signed path (``CloudPrinter.build``) overwrites them with the
    ms-resolution envelope values it signs.

    NOTE: this schema is reverse-engineered from the issue tracker (#65/#66),
    not yet byte-verified against a captured Handy ``project_file`` — validate
    against a real capture before trusting unattended starts."""
    job_id_int = now * 10
    job_id_str = str(job_id_int)
    name = upload["remote_name"]
    name_no_3mf = name
    if name_no_3mf.endswith(".gcode.3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")]
    elif name_no_3mf.endswith(".3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")] + ".gcode"
    ams_slot = int(slot)
    return {
        "sequence_id":              str(now),
        "command":                  "project_file",
        "param":                    "Metadata/plate_1.gcode",
        "file":                     name,
        "url":                      upload["url"],
        "md5":                      upload["md5"],
        "task_id":                  job_id_str,
        "subtask_id":               job_id_str,
        "subtask_name":             name_no_3mf,
        "job_id":                   job_id_int,
        "project_id":               job_id_str,
        "profile_id":               "0",
        "design_id":                "0",
        "model_id":                 "0",
        "plate_idx":                int(plate),
        "dev_id":                   serial,
        # 1 = CLOUD (vs 0 LAN)
        "job_type":                 1,
        "timestamp":                now,
        "bed_type":                 bed_type,
        "bed_temp":                 int(bed_temp),
        "auto_bed_leveling":        1 if bed_leveling else 0,
        "extrude_cali_flag":        1 if flow_cali else 0,
        "nozzle_offset_cali":       0,
        "extrude_cali_manual_mode": 0,
        "flow_cali":                bool(flow_cali),
        "bed_leveling":             bool(bed_leveling),
        "vibration_cali":           bool(vibration_cali),
        "timelapse":                bool(timelapse),
        "layer_inspect":            False,
        "use_ams":                  use_ams,
        "ams_mapping":              [ams_slot] if use_ams else [],
        "ams_mapping2":             ([{"ams_id": ams_slot // 4,
                                        "slot_id": ams_slot %  4}]
                                      if use_ams else []),
        "skip_objects":             None,
        "cfg":                      "0",
    }


def cmd_cloud_print(args: argparse.Namespace) -> int:
    """Submit a complete cloud-mediated print job:
       1. Upload the .gcode.3mf to Bambu's OSS via the upload-token API.
       2. Publish print.project_file with print_type=cloud + the OSS URL
          to the printer's cloud request topic.
       3. Bambu cloud relays to the bound printer; printer pulls from OSS.
    Sidesteps the LAN-direct verify-failure (#65/#66) entirely."""
    import os
    import threading
    import time
    from pathlib import Path
    import cloud_client

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1

    serial = args.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        try:
            devs = cli.get_bound_devices()
            if len(devs) == 1:
                serial = (devs[0].get("dev_id")
                          or devs[0].get("device_id"))
        except Exception:
            pass
    if not serial:
        print("--serial required (or set X2D_SERIAL, or have a "
              "single bound printer)", file=sys.stderr)
        return 1

    src = Path(args.file)
    if not src.is_file():
        print(f"file not found: {src}", file=sys.stderr)
        return 1

    # 1. Upload to OSS
    try:
        print(f"[cloud-print] uploading {src.name} "
              f"({src.stat().st_size} B) to Bambu OSS…",
              file=sys.stderr)
        upload = cli.cloud_upload_file(src)
        print(f"[cloud-print] uploaded → {upload['url']} "
              f"(md5={upload['md5']})", file=sys.stderr)
    except cloud_client.CloudError as e:
        print(f"[cloud-print] upload failed: {e}", file=sys.stderr)
        return 1

    # 2. Compose the print.project_file payload (cloud variant) via the
    #    shared builder (also used by `beambam start` print-next-in-queue).
    body = build_cloud_project_file(
        serial, upload, now=int(time.time()), slot=int(args.slot),
        plate=int(args.plate), bed_type=args.bed_type, bed_temp=int(args.bed_temp),
        use_ams=not args.no_ams, flow_cali=bool(args.flow_cali),
        bed_leveling=not args.no_level, vibration_cali=bool(args.vibration_cali),
        timelapse=bool(args.timelapse))
    name_no_3mf = body["subtask_name"]
    payload = {"print": body}

    # X-series firmware verifies the signature on print.* — sign the
    # project_file when a recovered key is present (else publish unsigned
    # for older firmware that doesn't enforce it). `signed_wire()` returns
    # the exact bytes to publish.
    wire, signed = _maybe_sign(serial, cli, "print", body, payload)

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    # 3. Publish via cloud broker
    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = threading.Event()
    def on_publish(client, userdata, mid,
                   reason_code=None, properties=None):
        published.set()
    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, wire, qos=1)
        info.wait_for_publish(timeout=args.timeout)
        if not published.wait(timeout=args.timeout):
            print(f"[cloud-print] no broker ack in {args.timeout}s",
                  file=sys.stderr)
            return 1
        print(json.dumps({"published": True, "signed": signed,
                          "topic": topic_request,
                          "url": upload["url"], "md5": upload["md5"],
                          "subtask_name": name_no_3mf}, indent=2))
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def _maybe_sign(serial: str, cli, family: str, command: dict,
                unsigned_payload: dict) -> tuple[bytes, bool]:
    """Return ``(wire_bytes, signed)`` for publishing ``command`` to a printer.

    When a recovered RSA signing key is present at the default path, build the
    cloud-signed message (X-series firmware verifies print.* — unsigned →
    ``mqtt message verify failed``). Otherwise fall back to the unsigned JSON
    (older firmware that doesn't enforce). ``command`` must NOT include the
    envelope ``sequence_id``/``timestamp`` for signing — they're stripped here
    and re-added by ``CloudPrinter.build`` so the signed pre-image matches."""
    try:
        from beambam.cli.control import _signing_key_path
        from beambam.cloud_control import CloudPrinter
    except Exception:
        return json.dumps(unsigned_payload).encode(), False
    if not _signing_key_path().is_file():
        return json.dumps(unsigned_payload).encode(), False
    cmd = {k: v for k, v in command.items()
           if k not in ("sequence_id", "timestamp")}
    cp = CloudPrinter.from_config(cli, serial, key_path=_signing_key_path())
    return cp.build(family, cmd), True


def cmd_cloud_publish(args: argparse.Namespace) -> int:
    """Publish a raw JSON payload to a printer via Bambu's cloud broker.
    Useful for one-shot commands when not on the printer's LAN. Schema
    matches the LAN-direct topic — `pause`, `resume`, `stop`,
    `gcode_line`, `ledctrl` all work the same way the LAN versions do."""
    import os
    import threading
    import cloud_client

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    serial = args.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        print("--serial required (or set X2D_SERIAL)",
              file=sys.stderr)
        return 1
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"--payload is not valid JSON: {e}", file=sys.stderr)
        return 1

    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = threading.Event()

    def on_publish(client, userdata, mid,
                   reason_code=None, properties=None):
        published.set()

    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=args.timeout)
        if not published.wait(timeout=args.timeout):
            print(f"[cloud-publish] no broker ack in {args.timeout}s",
                  file=sys.stderr)
            return 1
        print(json.dumps({"published": True, "topic": topic_request,
                          "payload": payload}, indent=2))
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def cmd_printables_search(args: argparse.Namespace) -> int:
    """Printables (printables.com) full-text search via the public GraphQL.

    Anonymous — no API key needed. Uses `searchPrints2(query, limit)` →
    items with id / name / slug / likesCount / downloadCount / user.
    Each hit's design page is
    `https://www.printables.com/model/<id>-<slug>`, which
    `beambam fetch <url>` already handles for download."""
    import urllib.request as _ur
    import urllib.error as _ue
    body = json.dumps({
        "query": (
            "query($q:String!,$l:Int,$o:Int){"
            "searchPrints2(query:$q,limit:$l,offset:$o)"
            "{items{id name slug likesCount downloadCount "
            "user{publicUsername}}}}"
        ),
        "variables": {"q": args.query, "l": int(args.limit),
                       "o": int(args.offset)},
    }).encode("utf-8")
    req = _ur.Request(
        "https://api.printables.com/graphql/",
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "beambam-cli/1.x"},
        method="POST")
    try:
        with _ur.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except _ue.HTTPError as e:
        print(f"Printables API failed HTTP {e.code}: "
              f"{e.read().decode('utf-8', 'replace')[:200]}",
              file=sys.stderr)
        return 1
    except Exception as e:                                  # noqa: BLE001
        print(f"Printables API failed: {e}", file=sys.stderr)
        return 1
    items = ((resp.get("data") or {})
             .get("searchPrints2", {}).get("items") or [])
    if args.json:
        print(json.dumps({"hits": items}, indent=2, default=str))
        return 0
    print(f"{len(items)} match(es) for {args.query!r} on Printables:\n")
    for it in items:
        u = (it.get("user") or {}).get("publicUsername", "?")
        slug = it.get("slug", "")
        print(f"  https://www.printables.com/model/{it['id']}-{slug}")
        print(f"     likes={it.get('likesCount',0):<5} "
              f"dls={it.get('downloadCount',0):<6}  "
              f"by {u[:20]:<20}  {it.get('name','')[:50]}")
    return 0


def _print_search_printables(args: argparse.Namespace) -> int:
    """Printables backend for `print-search --source printables`.

    Queries Printables' anonymous GraphQL, shows the same numbered
    picker as the MakerWorld flow, then chains: fetch via the existing
    Printables GraphQL path (`beambam fetch`) into a tmpdir, pick the
    first .3mf/.stl/.obj/.step, then forward to `beambam slice-print`
    with the user's --copies / --scale-pct / --mm / --color / --slot
    flags. Mirrors `cmd_cloud_print_design`'s subprocess approach.
    """
    import urllib.request as _ur
    import urllib.error as _ue
    body = json.dumps({
        "query": (
            "query($q:String!,$l:Int,$o:Int){"
            "searchPrints2(query:$q,limit:$l,offset:$o)"
            "{items{id name slug likesCount downloadCount "
            "user{publicUsername}}}}"
        ),
        "variables": {"q": args.query, "l": int(args.limit),
                       "o": int(getattr(args, "offset", 0) or 0)},
    }).encode("utf-8")
    req = _ur.Request(
        "https://api.printables.com/graphql/",
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "beambam-cli/1.x"},
        method="POST")
    try:
        with _ur.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except _ue.HTTPError as e:
        print(f"Printables API failed HTTP {e.code}: "
              f"{e.read().decode('utf-8', 'replace')[:200]}",
              file=sys.stderr)
        return 1
    except Exception as e:                                  # noqa: BLE001
        print(f"Printables API failed: {e}", file=sys.stderr)
        return 1
    items = ((resp.get("data") or {})
             .get("searchPrints2", {}).get("items") or [])
    if not items:
        print(f"no Printables results for {args.query!r}")
        return 1
    print(f"\n{len(items)} match(es) for {args.query!r} on Printables:\n")
    for i, it in enumerate(items, 1):
        likes = it.get("likesCount", 0)
        dls = it.get("downloadCount", 0)
        u = (it.get("user") or {}).get("publicUsername", "?")
        title = it.get("name", "")
        print(f"  {i:>2}. id={it['id']:<8}  likes={likes:<5} "
              f"dls={dls:<6}  by {u[:18]:<18}  {title[:40]}")
    if args.pick:
        idx = int(args.pick) - 1
    else:
        try:
            line = input(
                f"\nPick a number 1..{len(items)} "
                f"(blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1
        if not line:
            print("cancelled")
            return 0
        try:
            idx = int(line) - 1
        except ValueError:
            print(f"invalid pick: {line!r}")
            return 2
    if idx < 0 or idx >= len(items):
        print(f"out of range: {idx+1}")
        return 2
    chosen = items[idx]
    url = (f"https://www.printables.com/model/{chosen['id']}"
           f"-{chosen.get('slug', '')}")
    print(f"\n→ Picked: {chosen.get('name', '')}\n  {url}")
    if args.dry_run_pick:
        return 0

    # Chain: fetch → slice-print. Mirrors cmd_cloud_print_design's
    # subprocess approach so we inherit the entire arg-validation +
    # signing + upload + start_print pipeline from slice-print.
    import subprocess
    import tempfile
    from pathlib import Path
    # Phase 5e.6 removed x2d_bridge.py from the repo; the `x2d_bridge`
    # console-script entry now resolves to `beambam.cli:main`. The most
    # portable spawn target is `python -m beambam.cli` — works from
    # source checkout AND from any installed env, no PATH lookups.
    bridge_argv = [sys.executable, "-m", "beambam.cli"]
    with tempfile.TemporaryDirectory(prefix="print_search_pr_") as td:
        td_p = Path(td)
        # Step 1: fetch the model via the existing Printables GraphQL
        # path in cmd_fetch. --json lets us parse the saved paths back.
        fetch_cmd = bridge_argv + ["fetch", url,
                     "--out-dir", str(td_p), "--json"]
        try:
            res = subprocess.run(fetch_cmd, capture_output=True,
                                 text=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"\n[print-search] fetch failed (exit {e.returncode}):",
                  file=sys.stderr)
            if e.stderr:
                print(e.stderr, file=sys.stderr)
            return 1
        try:
            saved = json.loads(res.stdout) if res.stdout.strip() else []
        except json.JSONDecodeError:
            saved = []
        # Pick the first printable-shaped file. Prefer .3mf > .stl >
        # .obj/.step. Printables typically ships .stl; some packs
        # bundle a .3mf too.
        prio = (".3mf", ".stl", ".obj", ".step", ".stp")
        printable = next(
            (p for ext in prio
                for p in saved
                if str(p).lower().endswith(ext)),
            None,
        )
        if printable is None:
            print(f"\n[print-search] fetch saved no printable files. "
                  f"Got: {saved!r}", file=sys.stderr)
            return 1
        print(f"\n[print-search] fetched {Path(printable).name}",
              file=sys.stderr)

        # Step 2: slice + (maybe) upload + (maybe) print. Same flag
        # forwarding shape as cmd_cloud_print_design.
        cmd = bridge_argv + ["slice-print", str(printable)]
        if args.printer: cmd.extend(["--printer", args.printer])
        if args.ip:      cmd.extend(["--ip", args.ip])
        if args.code:    cmd.extend(["--code", args.code])
        if args.serial:  cmd.extend(["--serial", args.serial])
        if getattr(args, "scale", 1.0) != 1.0:
            cmd.extend(["--scale", str(args.scale)])
        if getattr(args, "scale_pct", None) is not None:
            cmd.extend(["--scale-pct", str(args.scale_pct)])
        if getattr(args, "mm", None) is not None:
            cmd.extend(["--mm", str(args.mm)])
        if int(getattr(args, "copies", 1)) != 1:
            cmd.extend(["--copies", str(args.copies)])
        if args.color:   cmd.extend(["--color", args.color])
        if args.slot:    cmd.extend(["--slot", str(args.slot)])
        if args.no_ams:  cmd.append("--no-ams")
        if args.dry_run: cmd.append("--dry-run")
        # Multi-color / cross-printer-profile re-slice flags — mirror BS
        # CLI's --load-* so a MakerWorld project authored for A1 can be
        # re-sliced for X2D dual-nozzle with per-AMS-slot filaments.
        if getattr(args, "load_filaments", None):
            cmd.extend(["--load-filaments", args.load_filaments])
        if getattr(args, "load_settings", None):
            cmd.extend(["--load-settings", args.load_settings])
        if getattr(args, "load_filament_ids", None):
            cmd.extend(["--load-filament-ids", args.load_filament_ids])
        if getattr(args, "load_defaultfila", False):
            cmd.append("--load-defaultfila")
        if getattr(args, "uptodate", False):
            cmd.append("--uptodate")
        if getattr(args, "allow_newer_3mf", False):
            cmd.append("--allow-newer-3mf")
        if getattr(args, "allow_multicolor_oneplate", False):
            cmd.append("--allow-multicolor-oneplate")
        if getattr(args, "repetitions", None) and int(args.repetitions) > 1:
            cmd.extend(["--repetitions", str(int(args.repetitions))])
        return subprocess.call(cmd)


def cmd_print_search(args: argparse.Namespace) -> int:
    """Interactive: MakerWorld OR Printables search → user picks → action.

    `beambam print-search "pokeball" --copies 4 --scale-pct 75 --color Gold`
    `beambam print-search "pokeball" --source printables`

    Steps:
      1. search the chosen catalogue
      2. show numbered table of top N hits
      3. prompt user for selection (1..N, blank = cancel)
      4. MakerWorld: chain into cloud-print-design (slice+upload+print).
         Printables: print the canonical model URL — user follows up
         with `beambam fetch <url>` then `beambam slice/print` manually
         (Printables files are .stl-in-.zip; the file pipeline differs
         from MW's pre-sliced .3mf).
    """
    import subprocess
    import sys as _sys
    if getattr(args, "source", "makerworld") == "printables":
        return _print_search_printables(args)
    import cloud_client
    # X2D_ROOT_PATH stays in x2d_bridge (single source of truth for the
    # install root; cmd_print_search subprocesses back into the bridge).
    from beambam import X2D_ROOT_PATH
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `cloud-login` first",
              file=sys.stderr)
        return 1
    try:
        r = cli.search_designs(args.query, limit=int(args.limit))
    except cloud_client.CloudError as e:
        print(f"search failed: {e}", file=sys.stderr)
        return 1
    hits = r.get("hits") or []
    if not hits:
        print(f"no results for {args.query!r}")
        return 1
    print(f"\n{r.get('total', 0)} match(es) for {args.query!r}, "
          f"top {len(hits)}:\n")
    for i, h in enumerate(hits, 1):
        d = h.get("design") or h
        did = d.get("id") or d.get("designId")
        title = str(d.get("title") or "")
        creator = (d.get("designCreator") or {}).get("name", "")
        likes = d.get("likeCount") or 0
        downloads = d.get("downloadCount") or 0
        print(f"  {i:>2}. id={did:<8}  likes={likes:<6} "
              f"dls={downloads:<6}  by {creator[:18]:<18}  "
              f"{title[:50]}")

    if args.pick:
        idx = int(args.pick) - 1
    else:
        try:
            line = input(
                f"\nPick a number 1..{len(hits)} "
                f"(blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1
        if not line:
            print("cancelled")
            return 0
        try:
            idx = int(line) - 1
        except ValueError:
            print(f"invalid pick: {line!r}")
            return 2
    if idx < 0 or idx >= len(hits):
        print(f"out of range: {idx+1}")
        return 2
    chosen = hits[idx].get("design") or hits[idx]
    design_id = chosen.get("id") or chosen.get("designId")
    title = chosen.get("title", "")
    print(f"\n→ Picked design {design_id}: {title}")
    if args.dry_run_pick:
        print("(--dry-run-pick: stopped before download/slice/print)")
        return 0

    # Chain into cloud-print-design (Phase 5e.6: x2d_bridge.py gone,
    # use `python -m beambam.cli` for portable spawn).
    cmd = [_sys.executable, "-m", "beambam.cli",
           "cloud-print-design", str(design_id)]
    if args.printer: cmd.extend(["--printer", args.printer])
    if args.ip:      cmd.extend(["--ip", args.ip])
    if args.code:    cmd.extend(["--code", args.code])
    if args.serial:  cmd.extend(["--serial", args.serial])
    if args.scale != 1.0:
        cmd.extend(["--scale", str(args.scale)])
    if args.scale_pct is not None:
        cmd.extend(["--scale-pct", str(args.scale_pct)])
    if args.mm is not None:
        cmd.extend(["--mm", str(args.mm)])
    if int(args.copies) != 1:
        cmd.extend(["--copies", str(args.copies)])
    if args.color:   cmd.extend(["--color", args.color])
    if args.slot:    cmd.extend(["--slot", str(args.slot)])
    if args.no_ams:  cmd.append("--no-ams")
    if args.dry_run: cmd.append("--dry-run")
    return subprocess.call(cmd)


def cmd_cloud_task_export(args: argparse.Namespace) -> int:
    """Export a past print-task's slicer metadata + thumbnails to a local
    directory by following the AWS S3 signed URLs in the task's
    `context` field.

    Why this exists: Bambu's `/api/v1/design-service/instance/<id>/f3mf`
    download endpoint is captcha-rate-limited after ~10 calls per IP
    window. The task-context S3 URLs are NOT — they are pre-signed by
    the backend at print time, valid for 24 hours, and have no anti-bot
    check.

    Limitation: the context only exposes Metadata/* files (configs +
    thumbnails). 3D mesh data and sliced gcode are NOT included. So
    this is useful for:
      * Recovering exact slicer configs from a past print
      * Cross-printer config replay
      * Auditing print history
    NOT for re-printing without re-downloading (the mesh is still on
    Bambu's S3 but not in the signed-URL list).

    Usage:
        beambam cloud-task-export 968321425 [--out-dir /tmp/audit]
    """
    from pathlib import Path
    import cloud_client
    import urllib.request

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr)
        return 1
    try:
        t = cli.get_task(args.task_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr)
        return 1
    ctx = t.get("context") or {}
    if isinstance(ctx, str):
        # Bambu's API sometimes returns context as Python-repr-formatted
        # JSON (single-quoted). Normalise.
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            try:
                ctx = json.loads(ctx.replace("'", '"'))
            except json.JSONDecodeError:
                print(f"task {args.task_id} has no parseable context",
                      file=sys.stderr)
                return 1

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Walk the context tree for every signed-URL we can find.
    def _walk(obj):
        if isinstance(obj, dict):
            url = obj.get("url")
            name = obj.get("name")
            sub = obj.get("dir", "")
            if url and "X-Amz-Signature" in url and name:
                yield (sub, name, url)
            for v in obj.values():
                yield from _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _walk(v)

    seen: dict[tuple[str, str], str] = {}
    for sub, name, url in _walk(ctx):
        seen[(sub, name)] = url

    if not seen:
        print(f"task {args.task_id} has zero signed S3 URLs in context",
              file=sys.stderr)
        return 1

    print(f"[cloud-task-export] task {args.task_id} → {len(seen)} files "
          f"into {out_dir}")
    written = 0
    for (sub, name), url in sorted(seen.items()):
        target_dir = out_dir / sub if sub else out_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        try:
            r = urllib.request.urlopen(url, timeout=15)
            data = r.read()
            target.write_bytes(data)
            print(f"  {sub:12s}/{name:<35s} {len(data):>10,} B")
            written += 1
        except Exception as e:                                  # noqa: BLE001
            print(f"  ! {sub}/{name}: {str(e)[:100]}",
                  file=sys.stderr)

    # Also dump the task record itself for completeness
    (out_dir / "_task.json").write_text(
        json.dumps(t, indent=2, default=str))

    print(f"\ndone: {written}/{len(seen)} files in {out_dir}")
    print("  (3D mesh + sliced gcode NOT included — Bambu's context "
          "endpoint exposes Metadata/* + thumbnails only)")
    return 0


def cmd_cloud_pull_design(args: argparse.Namespace) -> int:
    """Download a MakerWorld design's .3mf bundle to a local directory.

    Resolves the signed bblmw.com URL via the design-service/instance
    f3mf endpoint, then `urlretrieve`s into `--out-dir`. The default
    instance is picked (whichever has `isDefault: true`) unless
    `--instance-id` is given explicitly.

    Companion `cloud-print-design` chains this into slice-print."""
    from pathlib import Path
    import cloud_client

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir).expanduser()
    try:
        out = cli.pull_design_3mf(
            args.design_id, out_dir,
            instance_index=int(args.instance_index or 0))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr)
        return 1
    print(f"[cloud-pull] saved {out}  ({out.stat().st_size:,} B)")
    if args.json:
        print(json.dumps({"path": str(out),
                          "size": out.stat().st_size},
                         default=str))
    return 0


def cmd_cloud_print_design(args: argparse.Namespace) -> int:
    """End-to-end: download a MakerWorld design + slice + upload + print.

    `beambam cloud-print-design 1623016 [--copies 4 --scale-pct 75 --color Gold]`

    Steps:
      1. Resolve the design's default instance + download the .3mf.
      2. (Re-)slice via x2d_slice with the user's --copies / --scale / --color.
      3. Upload + start print on the configured printer.

    This is the FRE win — search the catalogue, pick a design, hit print."""
    import subprocess
    import sys as _sys
    import tempfile
    from pathlib import Path
    import cloud_client
    # X2D_ROOT_PATH stays in x2d_bridge as a single source of truth for
    # the install root (env-overridable). Same lazy-import pattern as
    # cmd_fetch in beambam.cli.info.
    from beambam import X2D_ROOT_PATH

    # Local-file bypass — `--from-3mf <path>` skips the cloud download
    # entirely (the f3mf endpoint is captcha-rate-limited after ~10 calls
    # in a window; for the file-already-on-disk path that's pure overhead).
    # Lets the user grab the .3mf via browser/BS Studio/MW one time and
    # then re-run cloud-print-design with the same flags for iteration.
    from_3mf = getattr(args, "from_3mf", None)
    if from_3mf:
        three_mf_local = Path(from_3mf).expanduser()
        if not three_mf_local.is_file():
            print(f"--from-3mf path does not exist: {three_mf_local}",
                  file=sys.stderr)
            return 1
        # Skip cloud auth + skip pull
        td_ctx = tempfile.TemporaryDirectory(prefix="cloud_print_design_local_")
        td_p = Path(td_ctx.name)
        three_mf = three_mf_local
        print(f"[cloud-print-design] using local 3mf {three_mf}  "
              f"({three_mf.stat().st_size:,} B; cloud download skipped)",
              file=sys.stderr)
    else:
        cli = cloud_client.CloudClient.load_or_anonymous()
        if cli.session.empty:
            print("not logged in (or pass --from-3mf <path> to skip cloud "
                  "download)", file=sys.stderr)
            return 1
        td_ctx = tempfile.TemporaryDirectory(prefix="cloud_print_design_")
        td_p = Path(td_ctx.name)
        try:
            three_mf = cli.pull_design_3mf(
                args.design_id, td_p,
                instance_index=int(args.instance_index or 0))
        except cloud_client.CloudError as e:
            print(f"cloud API failed: {e}", file=sys.stderr)
            if "418" in str(e) or "captcha" in str(e).lower():
                print("\nHINT: the /f3mf endpoint is captcha-rate-limited "
                      "after ~10 API downloads in a short window. "
                      "Workaround: download the .3mf manually (browser / "
                      "BS Studio / Bambu Handy) and re-run with "
                      "`--from-3mf <path>` to skip cloud download.",
                      file=sys.stderr)
            td_ctx.cleanup()
            return 1
        print(f"[cloud-print-design] downloaded {three_mf.name}  "
              f"({three_mf.stat().st_size:,} B)", file=sys.stderr)

    with td_ctx:

        # Build the slice-print invocation. We use the same x2d_bridge
        # but via subprocess so all the existing arg validation +
        # helpers fire.
        # Phase 5e.6: x2d_bridge.py removed; use `python -m beambam.cli`.
        cmd = [_sys.executable, "-m", "beambam.cli", "slice-print",
               str(three_mf)]
        if args.printer: cmd.extend(["--printer", args.printer])
        if args.ip:      cmd.extend(["--ip", args.ip])
        if args.code:    cmd.extend(["--code", args.code])
        if args.serial:  cmd.extend(["--serial", args.serial])
        if args.scale != 1.0:
            cmd.extend(["--scale", str(args.scale)])
        if args.scale_pct is not None:
            cmd.extend(["--scale-pct", str(args.scale_pct)])
        if args.mm is not None:
            cmd.extend(["--mm", str(args.mm)])
        if int(args.copies) != 1:
            cmd.extend(["--copies", str(args.copies)])
        if args.color:   cmd.extend(["--color", args.color])
        if args.slot:    cmd.extend(["--slot", str(args.slot)])
        if args.no_ams:  cmd.append("--no-ams")
        if args.dry_run: cmd.append("--dry-run")
        # Multi-color / cross-printer-profile flags (forwarded to slice-print)
        if getattr(args, "load_filaments", None):
            cmd.extend(["--load-filaments", args.load_filaments])
        if getattr(args, "load_settings", None):
            cmd.extend(["--load-settings", args.load_settings])
        if getattr(args, "load_filament_ids", None):
            cmd.extend(["--load-filament-ids", args.load_filament_ids])
        if getattr(args, "load_defaultfila", False):
            cmd.append("--load-defaultfila")
        if getattr(args, "uptodate", False):
            cmd.append("--uptodate")
        if getattr(args, "allow_newer_3mf", False):
            cmd.append("--allow-newer-3mf")
        if getattr(args, "allow_multicolor_oneplate", False):
            cmd.append("--allow-multicolor-oneplate")
        if getattr(args, "repetitions", None) and int(args.repetitions) > 1:
            cmd.extend(["--repetitions", str(int(args.repetitions))])
        return subprocess.call(cmd)


def cmd_cloud_state(args: argparse.Namespace) -> int:
    """Subscribe to the printer's cloud report topic and dump the first
    (or all, with --follow) state messages received. Useful for remote
    monitoring even when the printer isn't on the same LAN."""
    import os
    import threading
    import time
    import cloud_client
    from beambam.cli._helpers import _next_seq

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    serial = args.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        try:
            devices = cli.get_bound_devices()
        except Exception as e:
            print(f"can't list bound devices: {e}", file=sys.stderr)
            return 1
        if len(devices) == 1:
            serial = (devices[0].get("dev_id")
                      or devices[0].get("device_id"))
        else:
            print("multiple printers bound — pick one with --serial. "
                  "list via `x2d_bridge.py cloud-printers`.",
                  file=sys.stderr)
            return 1
    if not serial:
        print("no printer serial available", file=sys.stderr)
        return 1

    topic_report  = f"device/{serial}/report"
    topic_request = f"device/{serial}/request"

    state_seen: dict = {}
    pushall_done = threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc != 0:
            print(f"[cloud-state] MQTT connect failed rc={rc}",
                  file=sys.stderr)
            return
        c.subscribe(topic_report, qos=0)
        # Trigger a pushall so the printer publishes its full state.
        c.publish(topic_request, json.dumps({
            "pushing": {"command": "pushall",
                        "sequence_id": _next_seq(),
                        "version": 1, "push_target": 1}
        }))

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"_raw": msg.payload.decode(
                "utf-8", errors="replace")}
        if args.follow:
            print(json.dumps(
                {"topic": msg.topic, "payload": payload}, indent=2))
        else:
            state_seen.update(payload)
            if any(k in payload for k in ("print", "system", "info")):
                pushall_done.set()

    c = _cloud_mqtt_connect(serial, cli)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    try:
        if args.follow:
            print(f"[cloud-state] following {topic_report} — "
                  f"Ctrl-C to stop", file=sys.stderr)
            while True:
                time.sleep(1)
        else:
            if not pushall_done.wait(timeout=args.timeout):
                print(f"[cloud-state] timeout — no state in "
                      f"{args.timeout}s", file=sys.stderr)
                return 1
            print(json.dumps(state_seen, indent=2))
    except KeyboardInterrupt:
        pass
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def cmd_cloud_pause(args: argparse.Namespace) -> int:
    from beambam.cli._helpers import _print_cmd
    return _cloud_publish(_resolve_serial_or_exit(args),
                           _print_cmd("pause", param=""), args.timeout)


def cmd_cloud_resume(args: argparse.Namespace) -> int:
    from beambam.cli._helpers import _print_cmd
    return _cloud_publish(_resolve_serial_or_exit(args),
                           _print_cmd("resume", param=""), args.timeout)


def cmd_cloud_stop(args: argparse.Namespace) -> int:
    from beambam.cli._helpers import _print_cmd
    return _cloud_publish(_resolve_serial_or_exit(args),
                           _print_cmd("stop", param=""), args.timeout)


def cmd_cloud_gcode(args: argparse.Namespace) -> int:
    from beambam.cli._helpers import _print_cmd
    gcode = args.gcode if args.gcode.endswith("\n") else args.gcode + "\n"
    return _cloud_publish(_resolve_serial_or_exit(args),
                           _print_cmd("gcode_line", param=gcode),
                           args.timeout)


def cmd_cloud_chamber_light(args: argparse.Namespace) -> int:
    """Cloud equivalent of cmd_chamber_light. Same payload shape."""
    from beambam.cli._helpers import _system_cmd
    state = args.state.lower()
    if state not in ("on", "off", "flashing"):
        sys.exit(f"chamber-light state must be on/off/flashing, "
                  f"got: {state}")
    payload = _system_cmd(
        "ledctrl",
        led_node="chamber_light",
        led_mode=state,
        led_on_time=int(args.on_time),
        led_off_time=int(args.off_time),
        loop_times=int(args.loops),
        interval_time=int(args.interval),
    )
    return _cloud_publish(_resolve_serial_or_exit(args), payload,
                           args.timeout)


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

    cli_prof = sub.add_parser(
        "cloud-profile",
        help="Show the logged-in MakerWorld profile (handle, follower "
             "count, design count, bio).")
    cli_prof.add_argument("--json", action="store_true")
    cli_prof.set_defaults(fn=cmd_cloud_profile)

    cli_pts = sub.add_parser(
        "cloud-points",
        help="Show Bambu gamification points / progress breakdown.")
    cli_pts.add_argument("--json", action="store_true")
    cli_pts.set_defaults(fn=cmd_cloud_points)

    cli_unr = sub.add_parser(
        "cloud-unread",
        help="Count of unread aftersale tickets + MakerWorld "
             "notifications.")
    cli_unr.add_argument("--json", action="store_true")
    cli_unr.set_defaults(fn=cmd_cloud_unread)

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
