#!/usr/bin/env python3
"""extract_signing_key.py — recover Bambu Handy's printer-control RSA private key
from the running app's Dart heap, no Frida / no hooking.

Why this works (the whole story is in SIGNER_PLAN.md / SIGNER_HANDOFF.md):
  * Printer-control MQTT commands are RSA-SHA256-PKCS#1v1.5 signed; the firmware
    rejects unsigned ones. The signing is done in **pure Dart** (libapp.so AOT) —
    NOT in libflutter's BoringSSL (hooks never fired) and NOT in libgojni (that's
    the OpenIM chat SDK). So there is no native crypto call to hook.
  * BUT the app's RSA key is a Dart object, and its primes p,q live in the Dart
    heap as little-endian Uint32List digit arrays. We KNOW the public modulus n
    (from the app's X.509 cert, captured in `security.app_cert_install`). So we
    dump the heap and scan for a 128-byte (1024-bit) window that DIVIDES n — that
    window is a prime factor, which hands us the entire private key.

Verified: the recovered key reproduces a real captured Handy signature bit-for-bit,
and the live printer accepts beambam-signed commands (`reason:"ERROR STATE"`, not
`"mqtt message verify failed"`).

Usage:
  python3 extract_signing_key.py --serial <adb-serial> --modulus-hex <n_hex> \
      [--out ~/.x2d/printer_sign_key.pem]
  # n_hex comes from the app cert: load the PEM in security.app_cert_install and
  # take public_key().public_numbers().n  (see decode the capture, or pass --cert).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.exit("needs numpy: pip install numpy  (run from a clean cwd — $PREFIX/tmp "
             "shadows stdlib `inspect` and breaks numpy import)")


def adb(serial: str, *args: str) -> bytes:
    return subprocess.run(["adb", "-s", serial, *args],
                          capture_output=True).stdout


def anon_rw_regions(serial: str, pid: str):
    """Large anonymous rw regions, biggest first — Dart heap candidates. Skips the
    dalvik (Java) heaps: the key is a Dart object, not Java."""
    maps = adb(serial, "exec-out", "su", "-c", f"cat /proc/{pid}/maps").decode(
        "utf-8", "ignore")
    out = []
    for ln in maps.splitlines():
        p = ln.split()
        if len(p) < 5 or "rw" not in p[1]:
            continue
        name = p[5] if len(p) > 5 else "[anon]"
        if "dalvik" in name:                      # Java heap — not our key
            continue
        a, b = p[0].split("-")
        lo, hi = int(a, 16), int(b, 16)
        if hi - lo < 1 << 20:                     # skip < 1 MB
            continue
        out.append((hi - lo, lo, hi, name))
    out.sort(reverse=True)
    return out


def scan_region(serial: str, pid: str, lo: int, size: int, N: int):
    """dd the region out and scan 4-byte-aligned 128-byte windows for a factor of
    N. Pre-filter to odd + top-bit-set (a proper 1024-bit prime is odd and has its
    MSB set) before the expensive modulo."""
    raw = adb(serial, "exec-out", "su", "-c",
              f"dd if=/proc/{pid}/mem bs=1048576 iflag=skip_bytes,count_bytes "
              f"skip={lo} count={size} conv=noerror,sync 2>/dev/null")
    data = np.frombuffer(raw, dtype=np.uint8)
    if len(data) < 128:
        return None
    idx = np.arange(0, len(data) - 128, 4)
    cand = idx[((data[idx] & 1) == 1) & ((data[idx + 127] & 0x80) != 0)]
    for off in cand:
        v = int.from_bytes(data[int(off):int(off) + 128].tobytes(), "little")
        if 1 < v < N and N % v == 0:
            return v
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True, help="adb device serial (ip:port)")
    ap.add_argument("--modulus-hex", help="the app cert RSA modulus n (hex)")
    ap.add_argument("--cert", help="path to the app cert PEM (alternative to -hex)")
    ap.add_argument("--package", default="bbl.intl.bambulab.com")
    ap.add_argument("--out", default=str(Path.home() / ".x2d" / "printer_sign_key.pem"))
    ap.add_argument("--e", type=int, default=65537)
    args = ap.parse_args()

    if args.cert:
        from cryptography import x509
        N = x509.load_pem_x509_certificate(
            Path(args.cert).read_bytes()).public_key().public_numbers().n
    elif args.modulus_hex:
        N = int(args.modulus_hex, 16)
    else:
        sys.exit("provide --modulus-hex or --cert")

    pid = adb(args.serial, "shell", "pidof", "-s", args.package).decode().strip()
    if not pid:
        sys.exit(f"{args.package} not running — launch it (key must be in the heap)")
    print(f"[+] {args.package} pid={pid}; modulus {N.bit_length()}-bit")

    p = None
    for size, lo, hi, name in anon_rw_regions(args.serial, pid):
        print(f"[..] scanning {size/1048576:6.0f} MB {hex(lo)} {name}")
        p = scan_region(args.serial, pid, lo, size, N)
        if p:
            print(f"[+] FACTOR found in {name} @ {hex(lo)}")
            break
    if not p:
        sys.exit("no factor found — is the key loaded? (open the Devices tab so "
                 "Handy connects + signs at least once)")

    q = N // p
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    if p < q:
        p, q = q, p
    d = pow(args.e, -1, (p - 1) * (q - 1))
    key = rsa.RSAPrivateNumbers(
        p=p, q=q, d=d, dmp1=d % (p - 1), dmq1=d % (q - 1), iqmp=pow(q, -1, p),
        public_numbers=rsa.RSAPublicNumbers(e=args.e, n=N)).private_key()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pem)
    out.chmod(0o600)
    print(f"[+] reconstructed RSA-{N.bit_length()} private key -> {out} (chmod 600)")
    print("    verify against a captured signature with beambam.mqtt_sign.verify_message")


if __name__ == "__main__":
    main()
