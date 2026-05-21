"""beambam.ftps — FTPS implicit-TLS helpers for the printer's SD card.

Canonical home of the FTPS subclass + 3 public functions as of v1.2.0.
The bridge (x2d_bridge.py) re-imports from here for backward compat.

    from beambam.ftps import upload_file, download_file, list_files

    upload_file(creds, Path("model.gcode.3mf"))            # → /<name> on SD
    download_file(creds, "/cache/x.3mf", Path("./x.3mf"))  # → bytes written
    list_files(creds, "")                                  # root listing
    list_files(creds, "cache")                             # /cache listing

For most callers `beambam.Printer.{upload,download,list_files}` is
the better entry point — it's a thin facade over these.

Wire-level notes (Bambu vsFTPd 3.0.5 quirks)
--------------------------------------------

* **Implicit TLS on port 990** — control channel TLS-wrapped from byte 0
  (no AUTH TLS handshake). Mirror with `ssl_ctx.wrap_socket(raw)` before
  any FTP protocol bytes.
* **Session reuse on PASV** — vsFTPd with `ssl_session_reuse=YES` (the
  default on Bambu's build) rejects fresh TLS sessions on data
  connections. Override `ntransfercmd` to pass `session=self.sock.session`
  on the data wrap.
* **Unwrap before close** — the data socket's SSL layer must be
  `conn.unwrap()`'d before `conn.close()` or the next command on the
  control channel hangs / drops.
* **INVALID_ALERT trap** — bare `SSLContext(PROTOCOL_TLS_CLIENT)` fires
  `[SSL: INVALID_ALERT] invalid alert` during the data-channel handshake
  on Python 3.12+ when the printer is busy (active print). Use
  `ssl.create_default_context()` + force TLSv1.2 + manual
  socket.create_connection() + wrap_socket-once pattern instead.

All three patterns are encoded below. download_file uses the
TLSv1.2-forced create_default_context path (it's the one that survives
mid-print); list_files uses the legacy `_ImplicitFTPTLS` subclass
(its lighter-weight, list-only flow doesn't trip the alert); upload_file
uses the manual-socket path.
"""
from __future__ import annotations

import ftplib
import socket
import ssl
import sys
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beambam.config import Creds


__all__ = ["download_file", "list_files", "upload_file"]


# ----- subclass for the LIST path -----------------------------------------


class _ImplicitFTPTLS(FTP_TLS):
    """FTPS implicit TLS over port 990 — Bambu's protocol of choice.

    Implicit TLS on the control channel, plus a `ntransfercmd` override
    that re-uses the control session on PASV data channels (Bambu's
    recent firmware rejects fresh sessions with `522 SSL connection
    failed: session reuse required`), plus storbinary/retrbinary/
    retrlines that unwrap before close to avoid the post-transfer hang
    seen on the X2D / H2D.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = FTP_TLS.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,
            )
        return conn, size

    def storbinary(self, cmd, fp, blocksize=32768, callback=None, rest=None):
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except (OSError, ssl.SSLError):
                    pass
        finally:
            conn.close()
        return self.voidresp()

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                data = conn.recv(blocksize)
                if not data:
                    break
                callback(data)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except (OSError, ssl.SSLError):
                    pass
        finally:
            conn.close()
        return self.voidresp()

    def retrlines(self, cmd, callback=None):
        self.voidcmd("TYPE A")
        conn = self.transfercmd(cmd)
        try:
            buf = b""
            while True:
                chunk = conn.recv(8192)
                if not chunk:
                    if buf:
                        line = buf.decode("utf-8", errors="replace").rstrip("\r")
                        if callback:
                            callback(line)
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line_str = line.decode("utf-8", errors="replace").rstrip("\r")
                    if callback:
                        callback(line_str)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except (OSError, ssl.SSLError):
                    pass
        finally:
            conn.close()
        return self.voidresp()


# ----- internal: manual-socket FTP_TLS with PASV session reuse ------------


class _FTP_TLS_Reuse(FTP_TLS):
    """The upload/download path uses a manually-constructed FTP_TLS that
    bypasses _ImplicitFTPTLS' sock-setter chain (which double-wraps on
    Python 3.12+ → INVALID_ALERT). PASV data channel inherits the
    control TLS session per Bambu's `ssl_session_reuse=YES` requirement."""
    def ntransfercmd(self, cmd, rest=None):
        conn, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host,
                session=self.sock.session,
            )
        return conn, size


def _manual_ftp_tls(creds: "Creds", *, timeout: float = 20.0) -> _FTP_TLS_Reuse:
    """Build a manually-connected FTP_TLS_Reuse client. Caller is
    responsible for calling `.quit()` / `.close()`."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass

    ftp = _FTP_TLS_Reuse(context=ssl_ctx)
    ftp.set_pasv(True)
    raw = socket.create_connection((creds.ip, 990), timeout=timeout)
    ftp.sock = ssl_ctx.wrap_socket(raw, server_hostname=creds.ip)
    ftp.sock.settimeout(timeout + 10)
    ftp.file = ftp.sock.makefile("r", encoding="utf-8")
    ftp.host = creds.ip
    ftp.port = 990
    ftp.af = socket.AF_INET
    ftp.timeout = timeout + 10
    ftp.passiveserver = True
    ftp.encoding = "utf-8"
    ftp._prot_p = False  # type: ignore[attr-defined]
    ftp.welcome = ftp.getresp()
    ftp.login(user="bblp", passwd=creds.code)
    ftp.prot_p()
    return ftp


# ----- public API ---------------------------------------------------------


def download_file(creds: "Creds", remote_name: str, local_path: Path) -> int:
    """Download a file from the printer's SD card. Returns bytes written.

    Uses the TLSv1.2-forced manual-socket path that survives mid-print
    (see _manual_ftp_tls + the file-level INVALID_ALERT note)."""
    ftp = _manual_ftp_tls(creds, timeout=20.0)
    written = 0
    ftp.voidcmd("TYPE I")
    conn = ftp.transfercmd(f"RETR {remote_name}")
    try:
        with local_path.open("wb") as f:
            while True:
                chunk = conn.recv(32768)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        if isinstance(conn, ssl.SSLSocket):
            try:
                conn.unwrap()
            except (OSError, ssl.SSLError):
                pass
    finally:
        conn.close()
    ftp.voidresp()
    try:
        ftp.quit()
    except (OSError, ssl.SSLError, ftplib.error_perm):
        try: ftp.close()
        except Exception: pass
    return written


def list_files(creds: "Creds", path: str = "") -> list[str]:
    """Return raw LIST entries from the printer's SD card. Empty path
    lists the FTP root (where uploaded files land for X1C-style
    profiles); pass `"sdcard"` to list the X2D's actual SD mount."""
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ftp = _ImplicitFTPTLS(context=ssl_ctx)
    ftp.connect(creds.ip, 990, timeout=15)
    ftp.login(user="bblp", passwd=creds.code)
    ftp.prot_p()
    lines: list[str] = []
    cmd = f"LIST {path}".rstrip()
    ftp.retrlines(cmd, lambda line: lines.append(line))
    try:
        ftp.quit()
    except (OSError, ssl.SSLError, ftplib.error_perm):
        try: ftp.close()
        except Exception: pass
    return lines


def upload_file(creds: "Creds", local_path: Path,
                remote_name: str | None = None) -> None:
    """Upload a file to the printer's SD card via implicit FTPS."""
    if not local_path.is_file():
        sys.exit(f"file not found: {local_path}")
    if remote_name is None:
        remote_name = local_path.name
    ftp = _manual_ftp_tls(creds, timeout=15.0)
    try:
        with local_path.open("rb") as f:
            ftp.storbinary(f"STOR {remote_name}", f, blocksize=32768)
    finally:
        try:
            ftp.quit()
        except (OSError, ssl.SSLError, ftplib.error_perm):
            try: ftp.close()
            except Exception: pass
