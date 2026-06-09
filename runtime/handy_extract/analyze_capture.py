#!/usr/bin/env python3
"""analyze_capture.py — full traffic analysis of an x2dcap raw capture.

Reads x2d_raw.bin (length-framed SSL_write records captured by the Zygisk
module), reassembles each TLS connection's outbound HTTP/2 stream, and decodes
BOTH the HPACK HEADERS frames (request line + headers) AND the DATA frames
(request bodies — where the analytics/tracking payloads live). It then:

  * inventories every host, endpoint, HTTP method, query arg and request header;
  * decodes the analytics/tracking POST bodies (gzip-aware JSON) and summarises
    their event types + field schema;
  * diffs the observed endpoints against the ones beambam's cloud_client.py
    already knows, flagging NEW endpoints / args / methods worth wiring;
  * writes a Markdown report (results + metadata + a full per-request log).

Secrets (bearer tokens, captcha results, cookies, password-ish body fields) are
REDACTED to `<redacted N chars>` so the report is safe to keep in-repo.

Usage: python3 analyze_capture.py x2d_raw.bin [-o report.md] [--cloud-client cloud_client.py]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import struct
import sys
import zlib
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

from hpack import Decoder
from hyperframe.frame import Frame

H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
REC_HDR = struct.Struct("<4sQcI")   # magic 'X2RW', ssl ptr, via byte, len

# Header names whose VALUES are secrets — redact in the report.
SECRET_HEADERS = {"authorization", "x-bbl-captcha-result", "cookie",
                  "set-cookie", "x-auth-token", "x-bbl-trace-id"}
# Body keys (case-insensitive substring) whose values are secrets.
SECRET_BODY_KEYS = ("password", "token", "secret", "captcha", "pass_token",
                    "captcha_output", "authorization", "credential",
                    "app_cert", "sign_string", "access_code", "accesscode",
                    "crl")


def redact(value: str, keep: int = 0) -> str:
    n = len(value)
    head = value[:keep] if keep else ""
    return f"{head}<redacted {n} chars>"


# ---------------------------------------------------------------------------
# Record parsing + connection reassembly (mirrors decode_raw_h2 but keeps DATA)
# ---------------------------------------------------------------------------
def parse_records(blob: bytes):
    off, n = 0, len(blob)
    while off + REC_HDR.size <= n:
        magic, ssl, via, ln = REC_HDR.unpack_from(blob, off)
        if magic != b"X2RW":
            nxt = blob.find(b"X2RW", off + 1)
            if nxt < 0:
                break
            off = nxt
            continue
        off += REC_HDR.size
        if off + ln > n:
            break
        yield ssl, via.decode("latin1"), blob[off:off + ln]
        off += ln


def group_connections(records):
    conns, gen = OrderedDict(), {}
    for ssl, via, payload in records:
        if payload.startswith(b"PRI * HTTP/2.0"):
            gen[ssl] = gen.get(ssl, 0) + 1
        key = (ssl, via, gen.get(ssl, 0))
        conns.setdefault(key, bytearray()).extend(payload)
    return conns


def iter_frames(stream: bytes):
    off, n = 0, len(stream)
    while off + 9 <= n:
        try:
            frame, length = Frame.parse_frame_header(memoryview(stream[off:off + 9]))
        except Exception:
            return
        body = bytes(stream[off + 9:off + 9 + length])
        if len(body) < length:
            return
        yield frame, body
        off += 9 + length


def decode_connection(stream: bytes, via: str):
    """Return list of request dicts: {method, scheme, authority, path, headers
    (OrderedDict), body (bytes), via, stream}."""
    if stream.startswith(H2_PREFACE):
        stream = stream[len(H2_PREFACE):]
    elif stream.startswith(b"PRI * HTTP/2.0"):
        i = stream.find(b"\r\n\r\nSM\r\n\r\n")
        stream = stream[i + 8:] if i >= 0 else b""
    else:
        return []
    dec = Decoder()
    pending = {}                         # sid -> header-block fragment
    headers_by_sid = {}                  # sid -> OrderedDict
    body_by_sid = defaultdict(bytearray)
    order = []                           # sids in first-seen order
    for frame, body in iter_frames(stream):
        ft = type(frame).__name__
        sid = getattr(frame, "stream_id", 0)
        if ft in ("HeadersFrame", "ContinuationFrame"):
            try:
                frame.parse_body(memoryview(body))
                block = bytes(frame.data)
            except Exception:
                block = body
            buf = pending.pop(sid, bytearray())
            buf.extend(block)
            if "END_HEADERS" in frame.flags:
                try:
                    hl = dec.decode(bytes(buf))
                except Exception:
                    hl = [(":decode-error", "1")]
                od = OrderedDict()
                for k, v in hl:
                    od[k] = v
                headers_by_sid[sid] = od
                if sid not in order:
                    order.append(sid)
            else:
                pending[sid] = buf
        elif ft == "DataFrame":
            body_by_sid[sid].extend(body)
            if sid not in order:
                order.append(sid)
    out = []
    for sid in order:
        h = headers_by_sid.get(sid)
        if not h:
            continue
        out.append({
            "via": via, "stream": sid,
            "method": h.get(":method", "?"),
            "scheme": h.get(":scheme", "https"),
            "authority": h.get(":authority", ""),
            "path": h.get(":path", ""),
            "headers": OrderedDict((k, v) for k, v in h.items()
                                   if not k.startswith(":")),
            "body": bytes(body_by_sid.get(sid, b"")),
        })
    return out


# ---------------------------------------------------------------------------
def decode_body(headers: dict, body: bytes):
    """Best-effort decode a request body to text/JSON, gzip/deflate-aware."""
    if not body:
        return None
    enc = (headers.get("content-encoding") or "").lower()
    raw = body
    try:
        if enc == "gzip" or body[:2] == b"\x1f\x8b":
            raw = gzip.decompress(body)
        elif enc == "deflate":
            raw = zlib.decompress(body)
    except Exception:
        raw = body
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        try:
            return raw.decode("utf-8")
        except Exception:
            return f"<binary {len(raw)} bytes>"


def redact_obj(o):
    """Recursively redact secret-ish values in a decoded JSON body."""
    if isinstance(o, dict):
        r = {}
        for k, v in o.items():
            if any(s in k.lower() for s in SECRET_BODY_KEYS) and isinstance(v, str) and v:
                r[k] = redact(v)
            else:
                r[k] = redact_obj(v)
        return r
    if isinstance(o, list):
        return [redact_obj(x) for x in o[:20]] + (["…"] if len(o) > 20 else [])
    return o


def path_template(path: str) -> str:
    """Normalise a path to an endpoint template: strip query, replace numeric
    + uuid + long-hex segments with {id}."""
    base = path.split("?", 1)[0]
    segs = []
    for s in base.split("/"):
        if re.fullmatch(r"\d+", s):
            segs.append("{id}")
        elif re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}", s):
            segs.append("{uuid}")
        elif re.fullmatch(r"[0-9a-fA-F]{16,}", s):
            segs.append("{hex}")
        else:
            segs.append(s)
    return "/".join(segs)


def query_keys(path: str):
    if "?" not in path:
        return []
    q = path.split("?", 1)[1]
    keys = []
    for part in q.split("&"):
        if part:
            keys.append(part.split("=", 1)[0])
    return keys


def known_endpoints(cloud_client_path: Path):
    """Extract /v1/... endpoint templates already referenced in cloud_client.py."""
    if not cloud_client_path.is_file():
        return set()
    txt = cloud_client_path.read_text(encoding="utf-8", errors="ignore")
    paths = set()
    for m in re.findall(r"/v1/[A-Za-z0-9/_{}\-]+", txt):
        paths.add(path_template(m.replace("{id}", "0").replace("{kind}", "x")))
    return paths


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--cloud-client", default=str(Path(__file__).resolve().parents[2] / "cloud_client.py"))
    args = ap.parse_args()

    blob = Path(args.capture).read_bytes()
    records = list(parse_records(blob))
    conns = group_connections(records)
    reqs = []
    for (ssl, via, g), stream in conns.items():
        for r in decode_connection(bytes(stream), via):
            r["conn"] = f"0x{ssl:x}/{via}/g{g}"
            reqs.append(r)

    hosts = Counter(r["authority"] for r in reqs if r["authority"])
    methods = Counter(r["method"] for r in reqs)
    # endpoint catalog: (authority, template) -> {methods, args, count, examples}
    endpoints: dict = OrderedDict()
    for r in reqs:
        tmpl = path_template(r["path"])
        key = (r["authority"], tmpl)
        e = endpoints.setdefault(key, {"methods": Counter(), "args": Counter(),
                                       "count": 0, "example": r["path"], "via": set()})
        e["methods"][r["method"]] += 1
        e["count"] += 1
        e["via"].add(r["via"])
        for qk in query_keys(r["path"]):
            e["args"][qk] += 1
    header_names = Counter()
    for r in reqs:
        for k in r["headers"]:
            header_names[k] += 1

    known = known_endpoints(Path(args.cloud_client))
    def is_known(authority, tmpl):
        t = path_template(tmpl)
        return any(t == k or t in k or k in t for k in known)

    # analytics bodies
    analytics = []
    for r in reqs:
        if r["method"] in ("POST", "PUT") and r["body"]:
            dec = decode_body({k.lower(): v for k, v in r["headers"].items()}, r["body"])
            if dec is not None:
                analytics.append((r["authority"] + path_template(r["path"]), dec, r))

    out_path = Path(args.out) if args.out else Path(args.capture).with_suffix(".analysis.md")
    L = []
    w = L.append
    w(f"# Bambu Handy captured-traffic analysis\n")
    w(f"_Generated by `analyze_capture.py` from `{Path(args.capture).name}`. "
      f"Secrets (bearer tokens, captcha results, cookies, password/token body fields) are redacted._\n")
    # ---- metadata
    w("## Metadata\n")
    w(f"- capture bytes: **{len(blob):,}**")
    w(f"- SSL_write records: **{len(records):,}**")
    w(f"- TLS connection-generations: **{len(conns)}**")
    w(f"- decoded requests: **{len(reqs)}**")
    w(f"- distinct hosts: **{len(hosts)}**")
    w(f"- distinct endpoints (templated): **{len(endpoints)}**")
    w(f"- request bodies decoded: **{len(analytics)}**")
    w(f"- via Flutter BoringSSL (F): **{sum(1 for r in reqs if r['via']=='F')}**, "
      f"via Conscrypt (C): **{sum(1 for r in reqs if r['via']=='C')}**\n")
    w("### Methods\n")
    w("| method | count |\n|---|---|")
    for m, c in methods.most_common():
        w(f"| {m} | {c} |")
    w("\n### Hosts\n")
    w("| host | requests |\n|---|---|")
    for h, c in hosts.most_common():
        w(f"| `{h}` | {c} |")

    # ---- new endpoints
    new_eps = [(a, t, e) for (a, t), e in endpoints.items() if not is_known(a, t)]
    known_eps = [(a, t, e) for (a, t), e in endpoints.items() if is_known(a, t)]
    w(f"\n## Endpoints NOT in cloud_client.py ({len(new_eps)})\n")
    w("Candidates to wire / args beambam doesn't pass yet. (★ = bambulab.com API)\n")
    w("| host | endpoint | methods | query args | n |\n|---|---|---|---|---|")
    for a, t, e in sorted(new_eps, key=lambda x: (x[0], x[1])):
        star = "★ " if "bambulab.com" in a else ""
        mm = ",".join(f"{m}×{c}" for m, c in e["methods"].most_common())
        ar = ", ".join(sorted(e["args"])) or "—"
        w(f"| {star}`{a}` | `{t}` | {mm} | {ar} | {e['count']} |")

    w(f"\n## Endpoints already in cloud_client.py ({len(known_eps)})\n")
    w("| host | endpoint | methods | query args seen | n |\n|---|---|---|---|---|")
    for a, t, e in sorted(known_eps, key=lambda x: (x[0], x[1])):
        mm = ",".join(f"{m}×{c}" for m, c in e["methods"].most_common())
        ar = ", ".join(sorted(e["args"])) or "—"
        w(f"| `{a}` | `{t}` | {mm} | {ar} | {e['count']} |")

    # ---- header inventory
    w("\n## Request header inventory\n")
    w("| header | seen on N requests |\n|---|---|")
    for k, c in header_names.most_common():
        note = " _(redacted)_" if k.lower() in SECRET_HEADERS else ""
        w(f"| `{k}`{note} | {c} |")

    # ---- analytics
    w("\n## Analytics / tracking payloads\n")
    tracking_hosts = sorted({a.split("/")[0] for a, _, _ in analytics
                             if "lunkuo" in a or "event" in a or "analy" in a or "firelog" in a})
    w(f"Tracking endpoints: {', '.join('`'+h+'`' for h in tracking_hosts) or '—'}\n")

    # Flatten every analytics event. Bodies are either a dict or a list of
    # event dicts; the tracking host posts arrays of events.
    def events_of(dec):
        if isinstance(dec, list):
            return [x for x in dec if isinstance(x, dict)]
        if isinstance(dec, dict):
            return [dec]
        return []

    evt_types = Counter()           # the `evt` event-name field
    field_schema = Counter()        # union of keys across all events
    total_events = 0
    one_event_sample = None
    for a, dec, r in analytics:
        if "lunkuo" not in a and "event" not in a:
            continue
        for ev in events_of(dec):
            total_events += 1
            for k in ev:
                field_schema[k] += 1
            name = ev.get("evt") or ev.get("event") or ev.get("name") or "?"
            evt_types[name] += 1
            if one_event_sample is None:
                one_event_sample = redact_obj(ev)
    w(f"Captured **{total_events:,}** individual tracking events across "
      f"{sum(1 for a,_,_ in analytics if 'lunkuo' in a or 'event' in a)} POST batches.\n")
    if evt_types:
        w(f"### Tracking event types (`evt`) — {len(evt_types)} distinct\n")
        w("| event | count |\n|---|---|")
        for k, c in evt_types.most_common():
            w(f"| `{k}` | {c} |")
    if field_schema:
        w("\n### Tracking event field schema (union of keys)\n")
        w("| field | events |\n|---|---|")
        for k, c in field_schema.most_common():
            w(f"| `{k}` | {c} |")
    if one_event_sample:
        w("\n### Sample tracking event (redacted)\n```json")
        w(json.dumps(one_event_sample, ensure_ascii=False, indent=1)[:1800])
        w("```")

    # API request bodies (non-tracking) — small, show each schema once.
    w("\n### API request bodies (non-tracking, redacted)\n")
    seen_api = {}
    for a, dec, r in analytics:
        if "lunkuo" in a or "event" in a:
            continue
        seen_api.setdefault(a, redact_obj(dec))
    for a, s in seen_api.items():
        w(f"\n**`{a}`**\n```json")
        js = json.dumps(s, ensure_ascii=False, indent=1)
        w(js[:1200] + ("\n…(truncated)" if len(js) > 1200 else ""))
        w("```")

    # ---- MQTT printer-control (non-h2 connections beginning with CONNECT)
    def _mqtt_packets(st: bytes):
        i = 0
        out = []
        while i < len(st):
            b0 = st[i]; typ = b0 >> 4; flags = b0 & 0xf; i += 1
            mult = 1; rl = 0
            while i < len(st):
                c = st[i]; i += 1; rl += (c & 0x7f) * mult; mult *= 128
                if not c & 0x80:
                    break
            body = st[i:i + rl]; i += rl
            out.append((typ, flags, body))
        return out

    def _mstr(b, j):
        n = (b[j] << 8) | b[j + 1]; return b[j + 2:j + 2 + n].decode("utf-8", "replace"), j + 2 + n

    mqtt_lines = []
    for (ssl, via, g), stream in conns.items():
        st = bytes(stream)
        if not st.startswith(b"\x10"):
            continue
        mqtt_lines.append(f"\n**connection** `0x{ssl:x}/{via}` ({len(st)} bytes outbound)\n")
        for typ, flags, body in _mqtt_packets(st):
            if typ == 1 and len(body) > 6:        # CONNECT
                try:
                    proto, j = _mstr(body, 0); ver = body[j]; j += 1; cf = body[j]; j += 1
                    j += 2; cid, j = _mstr(body, j); user = ""
                    if cf & 0x80:
                        user, j = _mstr(body, j)
                    mqtt_lines.append(f"- CONNECT proto={proto} v{ver} clientid=`{cid}` "
                                      f"user=`{user}` pass=_(redacted)_")
                except Exception:
                    mqtt_lines.append("- CONNECT (parse error)")
            elif typ == 8:                        # SUBSCRIBE
                try:
                    j = 2; tps = []
                    while j < len(body):
                        t, j = _mstr(body, j); q = body[j]; j += 1; tps.append(f"`{t}` q{q}")
                    mqtt_lines.append(f"- SUBSCRIBE → {', '.join(tps)}")
                except Exception:
                    mqtt_lines.append("- SUBSCRIBE (parse error)")
            elif typ == 3:                        # PUBLISH
                try:
                    topic, j = _mstr(body, 0)
                    if flags & 0x06:
                        j += 2
                    payload = body[j:]
                    try:
                        obj = json.loads(payload)
                        fam = next((k for k in obj if k not in ("user_id", "header")), "?")
                        sub = obj.get(fam, {}) if isinstance(obj.get(fam), dict) else {}
                        cmd = sub.get("command", "")
                        signed = " [RSA-signed]" if isinstance(obj.get("header"), dict) and obj["header"].get("sign_string") else ""
                        js = json.dumps(redact_obj(obj))
                        mqtt_lines.append(f"- PUBLISH `{topic}` **{fam}.{cmd}**{signed}\n"
                                          f"    `{js[:300]}`")
                    except Exception:
                        mqtt_lines.append(f"- PUBLISH `{topic}` raw[{len(payload)}]")
                except Exception:
                    mqtt_lines.append("- PUBLISH (parse error)")
            elif typ == 12:
                mqtt_lines.append("- PINGREQ")
            elif typ == 10:
                mqtt_lines.append("- UNSUBSCRIBE")
    w("\n## MQTT printer-control (outbound)\n")
    if mqtt_lines:
        w("Commands Handy published to `device/<serial>/request` (cert/sign-string "
          "values redacted). `print.*` publishes only appear if a print was "
          "started/modified during capture.\n")
        for ln in mqtt_lines:
            w(ln)
    else:
        w("_No MQTT connection in this capture._")

    # ---- security-signed requests (SHIELD)
    SIGN_HEADERS = {"x-bbl-device-security-sign", "x-bbl-app-certification-id",
                    "x-bbl-client-id"}
    signed = [r for r in reqs if any(h.lower() in SIGN_HEADERS for h in r["headers"])]
    w(f"\n## SHIELD-signed requests ({len(signed)})\n")
    w("Requests carrying device-security-sign / app-certification-id (the only "
      "ones SHIELD signs per-request — the rest ride the plain Bearer token).\n")
    for r in signed:
        present = [h for h in r["headers"] if h.lower() in SIGN_HEADERS]
        w(f"- **{r['method']} {r['authority']}{path_template(r['path'])}** — signs: "
          f"{', '.join('`'+h+'`' for h in present)}")

    # ---- full log
    w("\n## Full request log\n")
    w("Every decoded request in capture order: method, URL, redacted headers, body summary.\n")
    for i, r in enumerate(reqs):
        hl = []
        for k, v in r["headers"].items():
            if k.lower() in SECRET_HEADERS:
                hl.append(f"{k}={redact(v)}")
            else:
                hl.append(f"{k}={v if len(v) < 80 else v[:77]+'…'}")
        bodynote = ""
        if r["body"]:
            dec = decode_body({k.lower(): v for k, v in r["headers"].items()}, r["body"])
            if isinstance(dec, (dict, list)):
                js = json.dumps(redact_obj(dec), ensure_ascii=False)
                bodynote = f"\n    body: {js[:400]}{'…' if len(js) > 400 else ''}"
            elif isinstance(dec, str):
                bodynote = f"\n    body[text {len(r['body'])}B]: {dec[:200]}"
            else:
                bodynote = f"\n    body: {len(r['body'])} bytes"
        w(f"\n{i+1}. **{r['method']} {r['scheme']}://{r['authority']}{r['path']}**  "
          f"`[{r['conn']} s{r['stream']}]`")
        w("    headers: " + "; ".join(hl))
        if bodynote:
            w(bodynote.lstrip("\n"))

    # ---- PII scrub: replace device/user identifiers with stable tokens so the
    # report is safe to keep in a public repo (correlation preserved, value
    # hidden). Bearer tokens / captcha results are already redacted inline.
    ID_KEYS = ("uid", "uuid", "session_id", "os_identifier", "dev_id",
               "deviceid", "device_id", "serial", "host_name")
    id_vals: set[str] = set()
    for r in reqs:
        dv = {k.lower(): v for k, v in r["headers"].items()}.get("x-bbl-device-id")
        if dv:
            id_vals.add(dv)
        dec = decode_body({k.lower(): v for k, v in r["headers"].items()}, r["body"]) \
            if r["body"] else None
        for ev in (dec if isinstance(dec, list) else [dec] if isinstance(dec, dict) else []):
            if isinstance(ev, dict):
                for k, v in ev.items():
                    if isinstance(v, str) and v and any(s in k.lower() for s in ID_KEYS):
                        id_vals.add(v)
    # stable token per distinct value, longest-first so substrings don't clobber
    scrub = {}
    for i, v in enumerate(sorted(id_vals, key=len, reverse=True)):
        if len(v) >= 8:
            scrub[v] = f"<id-{i+1}>"
    text = "\n".join(L)
    for v, t in scrub.items():
        text = text.replace(v, t)
    out_path.write_text(text, encoding="utf-8")
    print(f"wrote {out_path}  ({len(L)} lines)  scrubbed {len(scrub)} identifiers")
    print(f"requests={len(reqs)} hosts={len(hosts)} endpoints={len(endpoints)} "
          f"new={len(new_eps)} bodies={len(analytics)}")


if __name__ == "__main__":
    main()
