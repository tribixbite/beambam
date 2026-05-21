"""beambam.queuecli — `beambam queue` print queue CLI.

Edits the daemon's persistent queue file (~/.x2d/queue.json) so jobs
queued from the CLI are picked up by `beambam daemon --queue` for
dispatch.

Sub-CLI tree:

    beambam queue list                        # all jobs, status badges
    beambam queue add FILE [--printer NAME] [--slot N] [--label STR]
    beambam queue remove JOB_ID               # delete a job
    beambam queue cancel JOB_ID               # mark cancelled (kept for audit)
    beambam queue clear [--all]               # clear pending (or all with --all)
    beambam queue path                        # print queue file location

NOTE: the queue *file* edits land immediately, but actual print
dispatch only happens while `beambam daemon --queue` is running. Use
`beambam daemon` to start the dispatcher.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _qm():
    """Return a QueueManager pointed at the default file with a no-op
    dispatch callback (we're only doing file edits from CLI)."""
    from runtime.queue.manager import QueueManager
    return QueueManager(dispatch_cb=lambda _job: False)


def _format_age(ts: float) -> str:
    """Render a unix timestamp as 'Nm ago' / 'Nh ago' / etc."""
    import time
    if ts <= 0:
        return "—"
    age = max(0, time.time() - ts)
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h ago"
    return f"{int(age // 86400)}d ago"


_STATUS_GLYPH = {
    "pending":   "⋯",
    "running":   "▶",
    "done":      "✓",
    "failed":    "✗",
    "cancelled": "−",
}


# ----- subcommand impls ---------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    qm = _qm()
    jobs = qm.list()
    if not jobs:
        print("queue is empty")
        return 0
    pending = sum(1 for j in jobs if j.status == "pending")
    running = sum(1 for j in jobs if j.status == "running")
    done = sum(1 for j in jobs if j.status == "done")
    failed = sum(1 for j in jobs if j.status == "failed")
    cancelled = sum(1 for j in jobs if j.status == "cancelled")
    print(f"{len(jobs)} job(s): "
          f"{pending} pending, {running} running, {done} done, "
          f"{failed} failed, {cancelled} cancelled")
    print(f"  {'ID':<10} {'STATUS':<10} {'PRINTER':<12} {'SLOT':<5} "
          f"{'AGE':<10} LABEL")
    for j in jobs:
        glyph = _STATUS_GLYPH.get(j.status, "?")
        printer = j.printer or "(default)"
        age = _format_age(j.enqueued)
        label = j.label or Path(j.gcode).name
        print(f"  {j.id[:8]:<10} {glyph} {j.status:<8} {printer[:12]:<12} "
              f"{j.slot:<5} {age:<10} {label}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    qm = _qm()
    job = qm.add(
        printer=args.printer or "",
        gcode=str(path.resolve()),
        slot=args.slot,
        label=args.label or path.name,
    )
    if args.json_out:
        print(json.dumps(job.to_dict(), indent=2))
    else:
        print(f"enqueued {job.id[:8]} → {job.printer or '(default)'} "
              f"slot {job.slot}: {job.label}")
        print(f"  (waiting for `beambam daemon --queue` to dispatch)")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    qm = _qm()
    # Allow short prefix match (8 chars from `list`)
    target = _resolve_id(qm, args.job_id)
    if target is None:
        return 1
    ok = qm.remove(target.id)
    if ok:
        print(f"removed {target.id[:8]}")
        return 0
    print(f"could not remove {args.job_id}", file=sys.stderr)
    return 1


def cmd_cancel(args: argparse.Namespace) -> int:
    qm = _qm()
    target = _resolve_id(qm, args.job_id)
    if target is None:
        return 1
    ok = qm.cancel(target.id)
    if ok:
        print(f"cancelled {target.id[:8]}")
        return 0
    print(f"could not cancel {args.job_id} (already done/running?)",
          file=sys.stderr)
    return 1


def cmd_clear(args: argparse.Namespace) -> int:
    qm = _qm()
    jobs = qm.list()
    if not jobs:
        print("queue already empty")
        return 0
    removed = 0
    for j in jobs:
        if args.all or j.status in ("pending", "cancelled", "done", "failed"):
            if qm.remove(j.id):
                removed += 1
    print(f"removed {removed} job(s)")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    from runtime.queue.manager import _DEFAULT_PATH
    print(_DEFAULT_PATH)
    return 0


def _resolve_id(qm, prefix: str):
    """Find a job by id prefix (≥3 chars). Returns None if 0 or >1
    matches; prints a helpful error to stderr."""
    if len(prefix) < 3:
        print(f"id prefix too short: {prefix!r} (need ≥3 chars)",
              file=sys.stderr)
        return None
    matches = [j for j in qm.list() if j.id.startswith(prefix)]
    if not matches:
        print(f"no job matching {prefix!r}", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(f"ambiguous prefix {prefix!r} — matches: "
              f"{[m.id[:8] for m in matches]}", file=sys.stderr)
        return None
    return matches[0]


# ----- CLI ----------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "queue",
        help="Print queue (FIFO) editor. Daemon-side dispatch needs "
             "`beambam daemon --queue` running.",
    )
    q_sub = p.add_subparsers(dest="queue_cmd", required=True)

    ls = q_sub.add_parser("list", help="Show all jobs")
    ls.set_defaults(fn=cmd_list)

    pa = q_sub.add_parser("path", help="Print the queue file path")
    pa.set_defaults(fn=cmd_path)

    ad = q_sub.add_parser("add", help="Enqueue a .gcode.3mf")
    ad.add_argument("file", help="Local .gcode.3mf to enqueue")
    ad.add_argument("--printer", help="Printer section name "
                                       "(default: empty = default printer)")
    ad.add_argument("--slot", type=int, default=1,
                    help="AMS slot 1..16 (default 1)")
    ad.add_argument("--label", help="Display label (default: filename)")
    ad.add_argument("--json", dest="json_out", action="store_true")
    ad.set_defaults(fn=cmd_add)

    rm = q_sub.add_parser("remove", aliases=["rm"],
                            help="Delete a job (by id prefix)")
    rm.add_argument("job_id")
    rm.set_defaults(fn=cmd_remove)

    cn = q_sub.add_parser("cancel", help="Cancel a pending job (kept for audit)")
    cn.add_argument("job_id")
    cn.set_defaults(fn=cmd_cancel)

    cl = q_sub.add_parser("clear", help="Remove all jobs (--all incl. running)")
    cl.add_argument("--all", action="store_true",
                    help="Also remove running jobs (dangerous)")
    cl.set_defaults(fn=cmd_clear)

    return p
