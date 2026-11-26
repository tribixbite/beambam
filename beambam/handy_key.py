"""beambam.handy_key — the printer-control signing key and its app cert, from Bambu Handy on a rooted phone.

X2D / H2D firmware runs `print.*` only when it is signed by an app key Bambu's CA certified (beambam.signing).
Bambu Handy holds one per install, in its Dart heap while it is connected to a printer. With root on the phone
(Magisk `su`) over adb (USB, or the phone's LAN address), :func:`extract` gets both halves without any other
step:

1. the phone: the one rooted adb device with Handy installed, or `--adb IP[:PORT]` (connected if needed);
2. Handy running, and connected to the printer: if the key is not in its heap yet, Handy is brought to the
   front and its Devices tab tapped (the Devices tab connecting is what loads the key);
3. the app cert from the heap (PEM or DER, issuer `GLOF<n>.bambulab.com`; the cert Handy installs on the
   printer with `security.app_cert_install`), unless one is given;
4. the private key: a 1024-bit window of the heap that divides the cert's modulus is one of its primes
   (runtime/handy_extract/DART_HEAP_KEY_EXTRACTION.md), so the whole key follows;
5. checked (the key's public half is the cert's), saved to the config dir with `printer_cert_id.txt`, and the
   app that was in front on the phone put back.

Nothing is sent to the printer and nothing on the phone is changed beyond opening Handy's Devices tab. The
heap is read with `dd` from /proc/<pid>/mem as root; numpy makes the scan fast enough (`pip install numpy`).
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

HANDY = "bbl.intl.bambulab.com"
ADB_PORT = 5555
MIN_REGION = 1 << 18                     # smaller anonymous mappings hold no Dart heap worth reading
# The bottom tab whose screen makes Handy connect to the printer (and so load the signing key).
DEVICES_TAB = ("Devices", "Device", "设备", "Geräte", "Appareils", "Dispositivos", "Dispositivi", "デバイス")
PEM_RE = re.compile(rb"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\r\n]{200,4000}-----END CERTIFICATE-----")
ISSUER_RE = re.compile(r"^GLOF\d+\.bambulab\.com$")

Say = Callable[[str], None]


class KeyError_(RuntimeError):
    """Extraction cannot go on, in words for the user."""


# --------------------------------------------------------------------------- adb

@dataclass
class Phone:
    """One adb device."""
    serial: str
    timeout: float = 60.0

    def adb(self, *args: str, timeout: float | None = None) -> bytes:
        try:
            r = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True,
                               timeout=timeout or self.timeout)
        except FileNotFoundError as e:
            raise KeyError_("adb is not installed (Android platform-tools)") from e
        except subprocess.TimeoutExpired as e:
            raise KeyError_(f"adb did not answer from {self.serial} within {e.timeout:.0f} s") from e
        return r.stdout

    def sh(self, cmd: str, timeout: float | None = None) -> str:
        return self.adb("shell", cmd, timeout=timeout).decode("utf-8", "replace")

    def su(self, cmd: str, timeout: float | None = None) -> bytes:
        return self.adb("exec-out", "su", "-c", cmd, timeout=timeout)

    def rooted(self) -> bool:
        return "uid=0" in self.su("id", timeout=20).decode("utf-8", "replace")

    def has_handy(self) -> bool:
        return "package:" in self.sh(f"pm path {HANDY}", timeout=20)

    def pid(self) -> str:
        return self.sh(f"pidof -s {HANDY}", timeout=20).strip()

    def focus(self) -> str:
        """`package/activity` in front, '' when unknown (e.g. the lock screen)."""
        m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^\s}]+)\}", self.sh("dumpsys window", timeout=20))
        return m.group(1) if m else ""


def _adb_devices() -> list[str]:
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=30).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise KeyError_(f"adb is not usable here: {e}") from e
    return [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("\tdevice")]


def connect(addr: str) -> str:
    """`addr` as an adb serial, connected first when it is an IP (default port 5555) not already listed."""
    serial = addr if ":" in addr or not re.fullmatch(r"[\d.]+", addr) else f"{addr}:{ADB_PORT}"
    if serial in _adb_devices():
        return serial
    if re.fullmatch(r"[\d.]+:\d+", serial):
        out = subprocess.run(["adb", "connect", serial], capture_output=True, text=True, timeout=30).stdout
        if serial in _adb_devices():
            return serial
        raise KeyError_(f"cannot reach {serial} over adb ({out.strip()}): turn on wireless or network adb "
                        "on the phone (or plug it in) and allow this computer")
    raise KeyError_(f"no adb device {serial}")


def find_phone(addr: str | None = None, say: Say = lambda _: None) -> Phone:
    """The phone to read: `addr`, else the one connected rooted device with Handy."""
    if addr:
        phone = Phone(connect(addr))
        if not phone.has_handy():
            raise KeyError_(f"Bambu Handy is not installed on {phone.serial}")
        if not phone.rooted():
            raise KeyError_(f"{phone.serial} has no root (su): the heap can only be read as root (Magisk); "
                            "allow su for the shell in Magisk if it asked")
        return phone
    found = []
    for serial in _adb_devices():
        p = Phone(serial, timeout=20)
        try:
            if p.has_handy() and p.rooted():
                found.append(p)
        except KeyError_:
            continue                                   # an unresponsive device is not the one
    if len(found) == 1:
        say(f"using {found[0].serial}: rooted, with Bambu Handy")
        return Phone(found[0].serial)
    if not found:
        raise KeyError_("no rooted adb device with Bambu Handy: connect the phone (`--adb IP` for one on the "
                        "network) and check that `adb shell su -c id` says uid=0")
    raise KeyError_(f"several rooted devices with Handy ({', '.join(p.serial for p in found)}): pick one with --adb")


# --------------------------------------------------------------------------- Handy on screen

def _ui_nodes(phone: Phone) -> list[dict[str, str]]:
    xml = phone.sh("uiautomator dump /data/local/tmp/beambam_ui.xml >/dev/null 2>&1; "
                   "cat /data/local/tmp/beambam_ui.xml; rm -f /data/local/tmp/beambam_ui.xml", timeout=40)
    nodes = []
    for m in re.finditer(r"<node [^>]*>", xml):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(0)))
        nodes.append(attrs)
    return nodes


def _tap_devices_tab(phone: Phone, say: Say) -> bool:
    """Open Handy's Devices tab: back out of whatever Handy shows until the tab bar is there, then tap it. Every
    input checks first that Handy has the focus (a key press must never reach another app)."""
    for _ in range(5):
        if not phone.focus().startswith(HANDY + "/"):
            say("Handy is not in front: open its Devices tab by hand")
            return False
        for n in _ui_nodes(phone):
            label = (n.get("content-desc") or n.get("text") or "").split("\n")[0].strip()
            box = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
            if label in DEVICES_TAB and box:
                x1, y1, x2, y2 = map(int, box.groups())
                phone.sh(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
                say("opened Handy's Devices tab (it connects to the printer, which loads the key)")
                return True
        phone.sh("input keyevent BACK")                 # a page above the tab bar
        time.sleep(1.5)
    say("could not find Handy's Devices tab: open it by hand")
    return False


def bring_handy_up(phone: Phone, say: Say) -> None:
    """Start Handy if needed, put it in front, and open its Devices tab."""
    if not phone.pid():
        say("starting Bambu Handy…")
    phone.sh(f"monkey -p {HANDY} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")
    for _ in range(20):
        if phone.focus().startswith(HANDY + "/") and phone.pid():
            break
        time.sleep(1)
    else:
        raise KeyError_("Bambu Handy did not come to the front: unlock the phone and run this again")
    time.sleep(3)                                       # its first screen
    _tap_devices_tab(phone, say)


def restore_focus(phone: Phone, before: str) -> None:
    """Put back the app that was in front before (best effort; Handy itself stays as it is)."""
    if not before or before.startswith(HANDY + "/"):
        return
    pkg = before.split("/")[0]
    phone.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")


# --------------------------------------------------------------------------- the heap

def heap_regions(phone: Phone, pid: str) -> list[tuple[int, int, str]]:
    """(start, size, name) of the large anonymous rw mappings, biggest first: Dart heap candidates (the Java
    heaps are skipped: the key is a Dart object)."""
    maps = phone.su(f"cat /proc/{pid}/maps").decode("utf-8", "replace")
    out = []
    for ln in maps.splitlines():
        p = ln.split()
        if len(p) < 5 or "rw" not in p[1]:
            continue
        name = p[5] if len(p) > 5 else "[anon]"
        if "dalvik" in name or "stack" in name or (len(p) > 5 and not name.startswith("[")):
            continue                    # Java heaps, file mappings, thread stacks (a read of their guard page hangs)
        lo, hi = (int(x, 16) for x in p[0].split("-"))
        if hi - lo >= MIN_REGION:
            out.append((lo, hi - lo, name))
    out.sort(key=lambda r: -r[1])
    return out


def read_region(phone: Phone, pid: str, lo: int, size: int) -> bytes:
    return phone.su(f"dd if=/proc/{pid}/mem bs=1048576 iflag=skip_bytes,count_bytes skip={lo} count={size} "
                    "conv=noerror,sync 2>/dev/null", timeout=60.0 + size / 5e5)   # a 0.5 MB/s floor over Wi-Fi


def _read(phone: Phone, pid: str, lo: int, size: int, say: Say) -> bytes:
    """`read_region`, or b'' for a region that does not answer (one such mapping must not end the scan)."""
    try:
        return read_region(phone, pid, lo, size)
    except KeyError_ as e:
        say(f"skipped the region at {lo:#x} ({e})")
        return b""


def app_certs(data: bytes) -> Iterator[Any]:
    """The Handy app certs in a heap dump (PEM strings and DER blobs), as cryptography x509 objects."""
    from cryptography import x509
    seen: set[bytes] = set()
    for m in PEM_RE.finditer(data):
        try:
            cert = x509.load_pem_x509_certificate(m.group(0).replace(b"\r", b""))
        except ValueError:
            continue
        if _is_app_cert(cert) and cert.fingerprint(_sha256()) not in seen:
            seen.add(cert.fingerprint(_sha256()))
            yield cert
    for m in re.finditer(rb"\.bambulab\.com", data):
        # A DER cert holding the issuer name starts (0x30 0x82 <len16>) a few hundred bytes before it.
        for start in range(max(0, m.start() - 600), m.start()):
            if data[start] != 0x30 or data[start + 1] != 0x82:
                continue
            length = 4 + int.from_bytes(data[start + 2:start + 4], "big")
            if not 600 <= length <= 3000 or start + length < m.end():
                continue
            try:
                cert = x509.load_der_x509_certificate(data[start:start + length])
            except ValueError:
                continue
            fp = cert.fingerprint(_sha256())
            if _is_app_cert(cert) and fp not in seen:
                seen.add(fp)
                yield cert
            break


def _sha256() -> Any:
    from cryptography.hazmat.primitives import hashes
    return hashes.SHA256()


def _is_app_cert(cert: Any) -> bool:
    """A Handy app leaf: RSA-2048, issued by `GLOF<n>.bambulab.com` (not the printer's device cert, not a CA)."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    cns = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    key = cert.public_key()
    return (bool(cns) and bool(ISSUER_RE.match(str(cns[0].value))) and isinstance(key, rsa.RSAPublicKey)
            and key.key_size == 2048)


def factor_in(data: bytes, n: int) -> int | None:
    """A 1024-bit little-endian window of `data` (4-byte aligned) that divides `n`, or None. Pre-filtered to odd
    windows with the top bit set (a 1024-bit prime is both) before the modulo."""
    try:
        import numpy as np
    except ImportError as e:
        raise KeyError_("reading the key needs numpy: pip install numpy (or uvx --with numpy beambam key)") from e
    arr: "np.ndarray" = np.frombuffer(data, dtype=np.uint8)
    if len(arr) < 128:
        return None
    idx = np.arange(0, len(arr) - 128, 4)
    cand = idx[((arr[idx] & 1) == 1) & ((arr[idx + 127] & 0x80) != 0)]
    for off in cand.tolist():
        v = int.from_bytes(data[off:off + 128], "little")
        if 1 < v < n and n % v == 0:
            return v
    return None


def private_key(p: int, cert: Any) -> Any:
    """The RSA private key whose modulus `cert` carries and `p` divides."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    pub = cert.public_key().public_numbers()
    q = pub.n // p
    p, q = max(p, q), min(p, q)
    d = pow(pub.e, -1, (p - 1) * (q - 1))
    return rsa.RSAPrivateNumbers(p=p, q=q, d=d, dmp1=d % (p - 1), dmq1=d % (q - 1), iqmp=pow(q, -1, p),
                                 public_numbers=pub).private_key()


# --------------------------------------------------------------------------- the whole run

@dataclass
class Found:
    cert: Any
    key: Any
    cert_id: str


def scan(phone: Phone, cert: Any | None, say: Say) -> Found | None:
    """One pass over Handy's heap: the app cert (unless given) and the key that matches it."""
    from beambam.mqtt_sign import cert_id_from_app_cert
    pid = phone.pid()
    if not pid:
        raise KeyError_("Bambu Handy is not running")
    regions = heap_regions(phone, pid)
    say(f"reading Handy's heap ({len(regions)} regions, {sum(r[1] for r in regions) / 1048576:.0f} MB)…")
    certs: list[Any] = [cert] if cert is not None else []
    unread: list[tuple[int, int, str]] = []            # read before a cert was known: their windows unchecked
    for lo, size, _name in regions:
        data = _read(phone, pid, lo, size, say)
        if cert is None:
            for c in app_certs(data):
                if all(c.fingerprint(_sha256()) != k.fingerprint(_sha256()) for k in certs):
                    say(f"app cert in the heap: {c.subject.rfc4514_string()} (valid to {c.not_valid_after_utc:%Y-%m-%d})")
                    certs.append(c)
        if not certs:
            unread.append((lo, size, _name))
            continue
        for c in sorted(certs, key=lambda k: k.not_valid_before_utc, reverse=True):
            p = factor_in(data, c.public_key().public_numbers().n)
            if p:
                return Found(c, private_key(p, c), cert_id_from_app_cert(c))
    for lo, size, _name in unread:                     # the key can sit before the cert in the heap
        data = _read(phone, pid, lo, size, say)
        for c in certs:
            p = factor_in(data, c.public_key().public_numbers().n)
            if p:
                return Found(c, private_key(p, c), cert_id_from_app_cert(c))
    if not certs:
        say("no app cert in the heap yet")
    return None


def extract(addr: str | None = None, cert_path: Path | None = None, attempts: int = 3,
            say: Say = lambda m: print(f"[key] {m}", file=sys.stderr)) -> Found:
    """Find the phone, get Handy connected, read the cert and key (see the module doc)."""
    from cryptography import x509
    phone = find_phone(addr, say)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes()) if cert_path else None
    before = phone.focus()
    try:
        if not phone.pid():
            bring_handy_up(phone, say)
        for attempt in range(1, attempts + 1):
            found = scan(phone, cert, say)
            if found:
                return found
            if attempt < attempts:
                say("the key is not in Handy's heap yet: opening Handy on its Devices tab and trying again")
                bring_handy_up(phone, say)
                time.sleep(8)                           # it connects and signs its first command
        raise KeyError_("the key never appeared in Handy's heap. Is Handy signed in, and does its Devices tab "
                        "show the printer online? (A cert given with --cert from an older Handy login has the "
                        "wrong modulus: leave --cert out to read the current one.)")
    finally:
        restore_focus(phone, before)


def save(found: Found, out_dir: Path, say: Say = lambda m: print(f"[key] {m}", file=sys.stderr)) -> list[Path]:
    """Write key (0600), app cert and cert_id into `out_dir`, keeping a different previous copy as `.bak`."""
    from cryptography.hazmat.primitives import serialization
    pub = found.key.public_key().public_numbers()
    if pub != found.cert.public_key().public_numbers():
        raise KeyError_("the key does not match the app cert (not saved)")
    files = {
        "printer_sign_key.pem": found.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()),
        "printer_app_cert.pem": found.cert.public_bytes(serialization.Encoding.PEM),
        "printer_cert_id.txt": found.cert_id.encode(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in files.items():
        path = out_dir / name
        if path.is_file() and path.read_bytes().strip() != data.strip():
            backup = path.with_name(f"{name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            path.replace(backup)
            say(f"kept the previous {name} as {backup.name}")
        tmp = path.with_name(name + ".tmp")
        tmp.write_bytes(data)
        if name.endswith("key.pem"):
            tmp.chmod(0o600)
        tmp.replace(path)
        written.append(path)
    return written
