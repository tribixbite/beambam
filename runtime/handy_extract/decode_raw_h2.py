#!/usr/bin/env python3
"""Decode x2d_raw.bin (captured by the x2dcap Zygisk module) into readable
HTTP/2 requests.

Bambu Handy's API is HTTP/2, so request headers reach BoringSSL SSL_write already
HPACK-compressed. The module logs each SSL_write's raw plaintext as a record:

    magic 'X2RW' (4) | ssl ptr (8, LE u64) | via tag (1 byte 'C'/'F') | len (4, LE u32) | bytes…

We group records by SSL* pointer (one TLS connection), concatenate their bytes in
file order to recover the outbound client→server byte stream, strip the HTTP/2
connection preface, parse the frame stream, and HPACK-decode the HEADERS frames in
order (the HPACK decoder is stateful per connection). Each decoded request — its
:method/:path/:authority plus every header, including Bambu's signed x-bbl-*/
x-jiange-*/authorization headers — is printed, with /f3mf and design-service
requests flagged.

Usage: python3 decode_raw_h2.py x2d_raw.bin
"""
import sys
import struct
from collections import OrderedDict

from hpack import Decoder
from hyperframe.frame import Frame

H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
REC_HDR = struct.Struct("<4sQcI")   # magic, ssl, via, len


def parse_records(blob: bytes):
    """Yield (ssl_ptr, via, payload) records in file order."""
    off, n = 0, len(blob)
    while off + REC_HDR.size <= n:
        magic, ssl, via, ln = REC_HDR.unpack_from(blob, off)
        if magic != b"X2RW":
            # Resync: scan forward to the next magic (tolerates any torn record).
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
    """Concatenate each connection's outbound bytes. A connection is keyed by
    (ssl_ptr, generation): a fresh H2 preface starts a new generation, because the
    allocator reuses SSL* addresses after a connection closes."""
    conns = OrderedDict()
    gen = {}
    for ssl, via, payload in records:
        if payload.startswith(H2_PREFACE) or payload.startswith(b"PRI * HTTP/2.0"):
            gen[ssl] = gen.get(ssl, 0) + 1
        key = (ssl, via, gen.get(ssl, 0))
        conns.setdefault(key, bytearray()).extend(payload)
    return conns


def iter_frames(stream: bytes):
    """Yield (frame, body_bytes) over an HTTP/2 frame stream, tolerating a
    truncated trailing frame."""
    off, n = 0, len(stream)
    while off + 9 <= n:
        try:
            frame, length = Frame.parse_frame_header(memoryview(stream[off:off + 9]))
        except Exception:
            return
        body = bytes(stream[off + 9:off + 9 + length])
        if len(body) < length:
            return                      # truncated tail
        yield frame, body
        off += 9 + length


def decode_connection(stream: bytes):
    """Return a list of decoded requests (each an OrderedDict of headers) for one
    connection's outbound stream."""
    if stream.startswith(H2_PREFACE):
        stream = stream[len(H2_PREFACE):]
    elif stream.startswith(b"PRI * HTTP/2.0"):
        i = stream.find(b"\r\n\r\nSM\r\n\r\n")
        stream = stream[i + 8:] if i >= 0 else stream
    else:
        return []                       # not an H2 connection (or mid-stream)

    dec = Decoder()
    requests = []
    # Reassemble HEADERS(+CONTINUATION) per stream id before decoding.
    pending = {}                        # stream_id -> (bytearray fragment)
    for frame, body in iter_frames(stream):
        ftype = type(frame).__name__
        sid = getattr(frame, "stream_id", 0)
        if ftype == "HeadersFrame":
            # body includes optional padding/priority; hyperframe exposes the raw
            # header block via frame.data after parse_body.
            try:
                frame.parse_body(memoryview(body))
                block = bytes(frame.data)
            except Exception:
                block = body
            buf = pending.pop(sid, bytearray())
            buf.extend(block)
            if "END_HEADERS" in frame.flags:
                requests.append(_decode_block(dec, sid, bytes(buf)))
            else:
                pending[sid] = buf
        elif ftype == "ContinuationFrame":
            try:
                frame.parse_body(memoryview(body))
                block = bytes(frame.data)
            except Exception:
                block = body
            buf = pending.pop(sid, bytearray())
            buf.extend(block)
            if "END_HEADERS" in frame.flags:
                requests.append(_decode_block(dec, sid, bytes(buf)))
            else:
                pending[sid] = buf
    return [r for r in requests if r]


def _decode_block(dec, sid, block):
    try:
        headers = dec.decode(block)
    except Exception as e:
        return OrderedDict([(":decode-error", f"{e} on stream {sid}")])
    od = OrderedDict()
    od[":stream"] = str(sid)
    for k, v in headers:
        od[k] = v
    return od


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        blob = f.read()
    conns = group_connections(parse_records(blob))
    print(f"# {len(blob)} bytes, {len(conns)} connection-generations\n")

    all_reqs = []
    for (ssl, via, g), stream in conns.items():
        reqs = decode_connection(bytes(stream))
        for r in reqs:
            r["_conn"] = f"ssl=0x{ssl:x} via={via} gen={g}"
            all_reqs.append(r)

    flagged = []
    for r in all_reqs:
        path = r.get(":path", "")
        authority = r.get(":authority", "")
        is_f3mf = "f3mf" in path or "design-service" in path or "design-service" in authority
        hdr = f'{r.get(":method","?")} {authority}{path}  [{r.get("_conn","")}]'
        print(("★ " if is_f3mf else "  ") + hdr)
        if is_f3mf:
            flagged.append(r)

    print(f"\n# {len(all_reqs)} requests, {len(flagged)} matching f3mf/design-service\n")
    for r in flagged:
        print("=" * 72)
        print(f'{r.get(":method","?")} {r.get(":scheme","https")}://'
              f'{r.get(":authority","")}{r.get(":path","")}   {r.get("_conn","")}')
        for k, v in r.items():
            if k.startswith(":") or k.startswith("_"):
                continue
            print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    main()
