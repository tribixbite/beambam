#!/usr/bin/env python3
"""extract_project_file.py — pull the MQTT print.project_file (and any other
print.* command) out of an x2dcap raw capture.

The MQTT connection's SSL_write records aren't HTTP/2, so analyze_capture.py
skips them. But the publish payload is plaintext JSON, so we reassemble each
TLS connection's outbound bytes (grouped by ssl ptr, in file order) and scan for
JSON objects that contain `"command":"project_file"` (or any print/pushing
command), printing each pretty-printed.

Usage: python3 extract_project_file.py x2d_raw.bin [--command project_file]
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import OrderedDict

REC_HDR = struct.Struct("<4sQcI")   # magic 'X2RW' | ssl ptr | via | len


def _records(data: bytes):
    off = 0
    n = len(data)
    while off + REC_HDR.size <= n:
        magic, sp, via, ln = REC_HDR.unpack_from(data, off)
        if magic != b"X2RW":
            off += 1            # resync
            continue
        off += REC_HDR.size
        body = data[off:off + ln]
        off += ln
        yield sp, via, body


def _streams(data: bytes) -> "OrderedDict[int, bytes]":
    streams: "OrderedDict[int, bytearray]" = OrderedDict()
    for sp, _via, body in _records(data):
        streams.setdefault(sp, bytearray()).extend(body)
    return OrderedDict((k, bytes(v)) for k, v in streams.items())


def _json_objects_containing(blob: bytes, needle: bytes):
    """Yield decoded JSON objects in `blob` whose raw bytes contain `needle`.
    Brace-matched scan from each `{"` that precedes a needle hit."""
    seen_spans = []
    for m in re.finditer(re.escape(needle), blob):
        # walk back to a plausible object start
        start = blob.rfind(b'{"', 0, m.start())
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(blob)):
            ch = blob[i]
            if esc:
                esc = False
                continue
            if ch == 0x5c and in_str:        # backslash
                esc = True
                continue
            if ch == 0x22:                   # quote
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == 0x7b:                   # {
                depth += 1
            elif ch == 0x7d:                 # }
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0 or (start, end) in seen_spans:
            continue
        seen_spans.append((start, end))
        try:
            yield json.loads(blob[start:end].decode("utf-8", "replace"))
        except json.JSONDecodeError:
            pass


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--command", default="project_file",
                    help="command to extract (default project_file; use 'all' "
                         "for every print/pushing command)")
    args = ap.parse_args(argv)
    data = open(args.capture, "rb").read()
    streams = _streams(data)
    print(f"# {len(data):,} bytes, {len(streams)} TLS connections", file=sys.stderr)
    needles = ([b'"command":"project_file"'] if args.command != "all"
               else [b'"command":"', b'"pushing":'])
    found = 0
    for sp, blob in streams.items():
        for needle in needles:
            for obj in _json_objects_containing(blob, needle):
                # only print top-level command objects
                top = next(iter(obj)) if isinstance(obj, dict) else None
                if top in ("print", "pushing", "system", "camera", "info") or \
                   (isinstance(obj, dict) and "command" in obj):
                    found += 1
                    print(f"\n=== match on conn {sp:#x} ===")
                    print(json.dumps(obj, indent=2, ensure_ascii=False))
    print(f"\n# {found} command object(s) found", file=sys.stderr)
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
