#!/data/data/com.termux/files/usr/bin/env python3
"""fcm_snapshot_harvest.py — pull Bambu finish-snapshot JPGs from a rooted Handy

Bambu Cloud pushes a Firebase Cloud Messaging notification whenever a print
finishes (success / cancel / error). The notification body contains a
pre-signed AWS S3 URL pointing to the finish-snapshot JPG that Bambu's
cloud captures at print end. The URL is **1-hour valid** (`X-Amz-Expires=3600`).

Handy stores every received FCM notification in
`/data/data/bbl.intl.bambulab.com/shared_prefs/io.flutter.plugins.firebase.messaging.xml`.
This script:
  1. Pulls that XML over ADB (`su -c cat`).
  2. Diffs against a local seen-IDs ledger.
  3. For each new entry whose URL is still inside its validity window
     (signed within the last 60 minutes), fetches the JPG.
  4. Writes the JPG to `~/.x2d/snapshots/<print_id>.jpg` plus a
     `<print_id>.json` metadata sidecar capturing title, deviceId,
     sentTime, status.

Daemon mode loops every N seconds — Bambu's `track_flush_interval` of 5s
suggests fresh FCM arrivals show up in the XML within ~10s of a print
ending. 60s polling gives us 59 minutes of margin inside the URL window.

Usage:
    fcm_snapshot_harvest.py --device 192.168.1.50:41351 --once
    fcm_snapshot_harvest.py --device 192.168.1.50:41351 --daemon --interval 60
    fcm_snapshot_harvest.py --device 192.168.1.50:41351 --backfill  # try every
        # entry regardless of signed-at, in case anything is still valid

Sidecar JSON shape:
    {
      "print_id": "961930281",
      "device_id": "00M09A000000000",
      "sent_time_ms": 1779264174938,
      "title": "x2d: Task Success",
      "body": "Task Success",
      "redirect_url": "bambulab://bbl/device/page?deviceId=00M09A000000000",
      "fetched_at": "2026-05-21T01:23:45Z",
      "http_status": 200,
      "image_url": "https://or-cloud-device-prod.s3.dualstack.us-west-2.amazonaws.com/..."
    }
"""
from __future__ import annotations
import argparse, datetime as dt, html as _html, json, os, re, subprocess, sys, time
from pathlib import Path
from urllib import request as urlreq, error as urlerr

XML_PATH = "/data/data/bbl.intl.bambulab.com/shared_prefs/io.flutter.plugins.firebase.messaging.xml"
SNAP_DIR = Path(os.environ.get("X2D_SNAPSHOT_DIR") or Path.home() / ".x2d" / "snapshots")
SEEN_LEDGER = SNAP_DIR / ".seen_print_ids.json"

# AWS signed URLs include "X-Amz-Date" (signed-at) + "X-Amz-Expires" (window).
AMZ_DATE_RE  = re.compile(r"X-Amz-Date=(\d{8}T\d{6}Z)")
AMZ_EXPIRES_RE = re.compile(r"X-Amz-Expires=(\d+)")
PRINT_ID_RE = re.compile(r"/finish_snapshot/(\d+)\.jpg")


def adb_cat(device: str, remote_path: str) -> str:
    """Run `adb -s <device> shell su -c 'cat <path>'`. Returns decoded text."""
    r = subprocess.run(
        ["adb", "-s", device, "exec-out", "su", "-c", f"cat {remote_path}"],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        raise RuntimeError(f"adb-cat failed rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def parse_xml(text: str) -> list[dict]:
    """Extract every notification JSON from the FCM-messaging prefs XML."""
    out = []
    for m in re.finditer(r'<string name="([^"]+)">(\{.*?\})</string>', text, re.DOTALL):
        name, raw = m.group(1), m.group(2)
        try:
            j = json.loads(_html.unescape(raw))
        except json.JSONDecodeError:
            continue
        # Unescape Dart's \/ → / on URLs
        for d in (j.get("notification", {}).get("android", {}), j.get("data", {})):
            for k, v in list(d.items()):
                if isinstance(v, str) and r"\/" in v:
                    d[k] = v.replace(r"\/", "/")
        out.append({"_xml_name": name, **j})
    return out


def extract_url_and_id(notif: dict) -> tuple[str | None, str | None]:
    """Return (image_url, print_id) for a finish-snapshot notification, or (None, None)."""
    url = notif.get("notification", {}).get("android", {}).get("imageUrl", "")
    if not url:
        return None, None
    m = PRINT_ID_RE.search(url)
    return url, m.group(1) if m else None


def url_still_valid(url: str, margin_s: int = 60) -> bool:
    """Check if a pre-signed S3 URL is still within its validity window
    (with a small safety margin)."""
    m_date = AMZ_DATE_RE.search(url)
    m_exp  = AMZ_EXPIRES_RE.search(url)
    if not (m_date and m_exp):
        return False
    signed_at = dt.datetime.strptime(m_date.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    expires_at = signed_at + dt.timedelta(seconds=int(m_exp.group(1)) - margin_s)
    return dt.datetime.now(dt.timezone.utc) < expires_at


def load_seen() -> set[str]:
    if SEEN_LEDGER.exists():
        try:
            return set(json.loads(SEEN_LEDGER.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    SEEN_LEDGER.write_text(json.dumps(sorted(seen)))


def fetch_one(notif: dict, force_stale: bool = False) -> dict | None:
    """Fetch the snapshot JPG for one notification. Returns the sidecar
    dict on success, None on skip/error."""
    url, pid = extract_url_and_id(notif)
    if not pid or not url:
        return None
    if not force_stale and not url_still_valid(url):
        return {"print_id": pid, "skipped": "url_expired"}
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    jpg_path = SNAP_DIR / f"{pid}.jpg"
    if jpg_path.exists():
        return {"print_id": pid, "skipped": "already_have"}
    req = urlreq.Request(url, headers={"User-Agent": "x2d-bridge/fcm-harvest"})
    try:
        with urlreq.urlopen(req, timeout=15) as r:
            body = r.read()
            status = r.status
    except urlerr.HTTPError as e:
        return {"print_id": pid, "error": f"HTTP {e.code}", "image_url": url}
    except Exception as e:
        return {"print_id": pid, "error": f"{type(e).__name__}: {e}", "image_url": url}
    jpg_path.write_bytes(body)
    sidecar = {
        "print_id": pid,
        "device_id": _device_id_from_url(url),
        "sent_time_ms": notif.get("sentTime"),
        "title": notif.get("notification", {}).get("title", ""),
        "body":  notif.get("notification", {}).get("body", ""),
        "redirect_url": notif.get("data", {}).get("redirect_url", ""),
        "image_url": url,
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "http_status": status,
        "jpg_bytes": len(body),
    }
    (SNAP_DIR / f"{pid}.json").write_text(json.dumps(sidecar, indent=2))
    return sidecar


def _device_id_from_url(url: str) -> str | None:
    m = re.search(r"/devices/([^/]+)/finish_snapshot/", url)
    return m.group(1) if m else None


def run_once(device: str, force_stale: bool = False, verbose: bool = True) -> dict:
    seen = load_seen()
    xml = adb_cat(device, XML_PATH)
    notifs = parse_xml(xml)
    stats = {"total": len(notifs), "fetched": 0, "expired": 0, "skipped": 0, "errors": 0, "new": []}
    for n in notifs:
        url, pid = extract_url_and_id(n)
        if not pid: continue
        if pid in seen and not force_stale: continue
        result = fetch_one(n, force_stale=force_stale)
        if not result:
            continue
        if "error" in result:
            stats["errors"] += 1
            if verbose: print(f"  err  {pid}: {result['error']}")
        elif result.get("skipped") == "url_expired":
            stats["expired"] += 1
            seen.add(pid)  # mark seen so we don't try again
            if verbose: print(f"  stale  {pid} (signed >1h ago)")
        elif result.get("skipped") == "already_have":
            stats["skipped"] += 1
            seen.add(pid)
        else:
            stats["fetched"] += 1
            seen.add(pid)
            stats["new"].append(result)
            if verbose:
                print(f"  ✓ {pid}: {result['jpg_bytes']:>6}B  {result['title']}")
    save_seen(seen)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(prog="fcm_snapshot_harvest",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--device", required=True, help="adb serial / ip:port of rooted Handy host")
    ap.add_argument("--once", action="store_true", help="Single sweep")
    ap.add_argument("--daemon", action="store_true", help="Loop forever")
    ap.add_argument("--interval", type=int, default=60, help="Seconds between sweeps in --daemon mode (default 60)")
    ap.add_argument("--backfill", action="store_true",
                    help="Re-attempt every stored entry even if URL appears expired. Cheap to try, "
                         "AWS occasionally serves URLs past their stated expiry if the bucket has soft expiry.")
    ap.add_argument("--verbose", action="store_true", help="Per-entry trace output")
    args = ap.parse_args()

    if not (args.once or args.daemon):
        args.once = True

    if args.once:
        stats = run_once(args.device, force_stale=args.backfill, verbose=args.verbose or True)
        print(f"\n=== {stats['total']} notifications scanned | fetched {stats['fetched']} | "
              f"expired {stats['expired']} | skipped {stats['skipped']} | errors {stats['errors']}")
        print(f"  snapshots dir: {SNAP_DIR}")
        return 0

    print(f"[harvest] daemon polling {args.device} every {args.interval}s → {SNAP_DIR}")
    while True:
        try:
            stats = run_once(args.device, force_stale=False, verbose=args.verbose)
            if stats["fetched"]:
                ts = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                print(f"[harvest {ts}] fetched {stats['fetched']} new snapshot(s)")
        except Exception as e:
            print(f"[harvest] sweep failed: {e}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
