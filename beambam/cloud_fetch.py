"""beambam.cloud_fetch — query MakerWorld + Bambu Cloud for design info
and user print history.

What this DOES:
  * `--info <design_id>` — full metadata for a MakerWorld design via the
    public `/api/v1/design-service/design/<id>` endpoint (replaces the
    stale `/api/v1/design/design-detail` path the legacy `fetch`
    command uses, which 404s since the 2026 MakerWorld backend rewrite).
  * `--instances <design_id>` — list all sliced profiles for a design,
    showing compatible printers + downloadable thumbnails + prediction
    time + weight per profile.
  * `--user-tasks [--limit N]` — list the user's recent Bambu Cloud
    print tasks (requires `beambam cloud-login` first).
  * `--bound-devices` — list printers bound to the Bambu Cloud account.
  * `--design-cover <design_id> [--out path]` — download the cover
    image (the only un-gated CDN asset).

What this DOES NOT (yet):
  * Direct .gcode.3mf download from MakerWorld. The CDN URL pattern
    `https://makerworld.bblmw.com/makerworld/model/<modelId>/<profileId>
    /instance/<filename>.gcode.3mf` requires MakerWorld web auth
    cookies — the Bambu cloud Bearer doesn't authenticate against
    that bucket. The "Download" button on the MakerWorld web UI uses
    a session cookie set during login. Without scraping cookies (out
    of scope), the only download paths are:
      (a) interactive: open the URL in a browser to get the 3mf,
      (b) self-cloud-printed: files you've already printed land in
          ~/.x2d/cloud/ (see `beambam files cache` after a print).

This module is intentionally honest about that limitation rather than
shipping a half-broken downloader that surprises users at print time.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


MAKERWORLD_API = "https://makerworld.com/api/v1/design-service"


def _http_get_json(url: str, *, headers: dict[str, str] | None = None,
                   timeout: float = 15.0) -> dict[str, Any]:
    """GET + decode JSON. Raises on HTTPError, ValueError on non-JSON."""
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _http_get_bytes(url: str, *, headers: dict[str, str] | None = None,
                    timeout: float = 30.0) -> bytes:
    h = {"User-Agent": "Mozilla/5.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# MakerWorld public API
# ---------------------------------------------------------------------------


def fetch_design_info(design_id: int | str) -> dict[str, Any]:
    """Fetch full design metadata. No auth needed."""
    return _http_get_json(f"{MAKERWORLD_API}/design/{int(design_id)}")


def list_instances(design_id: int | str) -> list[dict[str, Any]]:
    """Return the list of sliced profiles for a design."""
    info = fetch_design_info(design_id)
    return info.get("instances") or []


def fetch_design_cover(design_id: int | str, out: Path) -> int:
    """Download the design's cover image. Returns bytes written."""
    info = fetch_design_info(design_id)
    cover = info.get("coverUrl") or info.get("coverPortrait") or info.get("coverLandscape")
    if not cover:
        raise RuntimeError(f"design {design_id} has no cover image URL")
    data = _http_get_bytes(cover)
    out.write_bytes(data)
    return len(data)


# ---------------------------------------------------------------------------
# Bambu Cloud (authed)
# ---------------------------------------------------------------------------


def fetch_user_tasks(limit: int = 20) -> list[dict[str, Any]]:
    """Return the user's recent Bambu Cloud print tasks. Requires login."""
    from cloud_client import CloudClient
    c = CloudClient.load_or_anonymous()
    return c.get_user_tasks(limit=limit)


def fetch_bound_devices() -> list[dict[str, Any]]:
    """Printers bound to the Bambu Cloud account. Requires login."""
    from cloud_client import CloudClient
    c = CloudClient.load_or_anonymous()
    return c.get_bound_devices()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_design_summary(info: dict[str, Any]) -> str:
    """Render a one-screen summary of design metadata."""
    lines = []
    lines.append(f"id:           {info.get('id')}")
    lines.append(f"title:        {info.get('title')}")
    lines.append(f"slug:         {info.get('slug')}")
    lines.append(f"model_id:     {info.get('modelId')}")
    lines.append(f"license:      {info.get('license')}")
    lines.append(f"print_count:  {info.get('printCount')}")
    lines.append(f"download_count: {info.get('downloadCount')}")
    lines.append(f"like_count:   {info.get('likeCount')}")
    cover = info.get("coverUrl")
    if cover:
        lines.append(f"cover:        {cover}")
    instances = info.get("instances") or []
    lines.append(f"instances:    {len(instances)}")
    for inst in instances[:10]:
        modelinfo = inst.get("extention", {}).get("modelInfo", {})
        compat = modelinfo.get("compatibility") or {}
        other = modelinfo.get("otherCompatibility") or []
        compat_names = ([compat.get("devProductName")]
                        + [c.get("devProductName") for c in other[:4]])
        compat_str = ", ".join(c for c in compat_names if c)
        plates = modelinfo.get("plates") or []
        plate_str = ""
        if plates:
            first = plates[0]
            pred = first.get("prediction")
            w = first.get("weight")
            if pred:
                plate_str = f"   ({pred}s, {w}g)"
        lines.append(
            f"  [{inst.get('id')}] profile={inst.get('profileId')}  "
            f"\"{inst.get('title')}\"{plate_str}  → {compat_str}"
        )
    if len(instances) > 10:
        lines.append(f"  …and {len(instances) - 10} more.")
    return "\n".join(lines)


def format_task_summary(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "no recent tasks (or list returned empty for this account/region)"
    lines = [f"{len(tasks)} recent task(s):"]
    for t in tasks:
        lines.append(
            f"  [{t.get('id')}] {t.get('title') or t.get('subtaskName') or '<no title>'}  "
            f"design={t.get('designId')} status={t.get('status')} "
            f"weight={t.get('weight')}g"
        )
    return "\n".join(lines)


def format_devices(devices: list[dict[str, Any]]) -> str:
    if not devices:
        return "no printers bound to this Bambu Cloud account"
    lines = [f"{len(devices)} bound printer(s):"]
    for d in devices:
        code = d.get('dev_access_code') or ""
        lines.append(
            f"  [{d.get('dev_id')}] {d.get('name')} "
            f"model={d.get('dev_product_name')} "
            f"version={code[:4]}…  "
            f"online={d.get('online')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "cloud-fetch",
        help="Query MakerWorld + Bambu Cloud for design info, instances, "
             "user tasks, bound devices. Replaces the stale `fetch` "
             "MakerWorld endpoint.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--info", metavar="DESIGN_ID",
                   help="Print full metadata for a MakerWorld design")
    g.add_argument("--instances", metavar="DESIGN_ID",
                   help="List all sliced profiles for a design")
    g.add_argument("--design-cover", metavar="DESIGN_ID",
                   help="Download the design's cover image")
    g.add_argument("--user-tasks", action="store_true",
                   help="List recent Bambu Cloud print tasks (auth)")
    g.add_argument("--bound-devices", action="store_true",
                   help="List printers bound to Bambu Cloud (auth)")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Machine-readable JSON instead of human summary")
    p.add_argument("--limit", type=int, default=20,
                   help="Max items for --user-tasks (default 20)")
    p.add_argument("--out", help="Output path for --design-cover")
    p.set_defaults(fn=cmd_cloud_fetch)
    return p


def cmd_cloud_fetch(args: argparse.Namespace) -> int:
    try:
        if args.info:
            info = fetch_design_info(args.info)
            if args.json_out:
                print(json.dumps(info, indent=2))
            else:
                print(format_design_summary(info))
        elif args.instances:
            instances = list_instances(args.instances)
            if args.json_out:
                print(json.dumps(instances, indent=2))
            else:
                for inst in instances:
                    plates = (inst.get("extention", {}).get("modelInfo", {})
                              .get("plates") or [])
                    pred = plates[0].get("prediction") if plates else None
                    print(f"[{inst.get('id')}] profile={inst.get('profileId')}  "
                          f"{inst.get('title')!r}  "
                          f"({pred}s)" if pred else
                          f"[{inst.get('id')}] profile={inst.get('profileId')}  "
                          f"{inst.get('title')!r}")
        elif args.design_cover:
            out = Path(args.out or f"design-{args.design_cover}-cover.png")
            n = fetch_design_cover(args.design_cover, out)
            print(f"wrote {n:,} bytes → {out}", file=sys.stderr)
        elif args.user_tasks:
            tasks = fetch_user_tasks(limit=args.limit)
            if args.json_out:
                print(json.dumps(tasks, indent=2))
            else:
                print(format_task_summary(tasks))
        elif args.bound_devices:
            devices = fetch_bound_devices()
            if args.json_out:
                print(json.dumps(devices, indent=2))
            else:
                print(format_devices(devices))
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason} ({e.url})", file=sys.stderr)
        return 1
    except Exception as e:                                  # noqa: BLE001
        print(f"cloud-fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
