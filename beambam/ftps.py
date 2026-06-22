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

All three public functions route through the same resilient transport —
`_manual_ftp_tls` (TLSv1.2-forced `create_default_context` + manual socket +
PASV session reuse), the one that survives mid-print. list_files used to use a
bare-`PROTOCOL_TLS_CLIENT` subclass; that tripped INVALID_ALERT on subdir
listings during an active print (the exact trap above), so it now shares the
resilient path + a short retry for the flaky mid-print data channel.
"""
from __future__ import annotations

import ftplib
import socket
import ssl
import sys
import time
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beambam.config import Creds


__all__ = ["download_file", "list_files", "upload_file"]


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


def list_files(creds: "Creds", path: str = "", *, retries: int = 3) -> list[str]:
    """Return raw LIST entries from the printer's SD card. Empty path
    lists the FTP root (where uploaded files land for X1C-style
    profiles); pass a subdir (e.g. ``"cache"``) to list that directory.

    Routes through the resilient `_manual_ftp_tls` transport (TLSv1.2-forced)
    and retries the data channel: a bare TLS context tripped `[SSL:
    INVALID_ALERT]` on subdir listings while a print was active, and even the
    resilient path can drop the data connection mid-print, so we retry a few
    times before giving up."""
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        ftp = _manual_ftp_tls(creds, timeout=20.0)
        try:
            ftp.voidcmd("TYPE A")
            conn = ftp.transfercmd(f"LIST {path}".rstrip())
            buf = b""
            try:
                while True:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
                if isinstance(conn, ssl.SSLSocket):
                    try:
                        conn.unwrap()
                    except (OSError, ssl.SSLError):
                        pass
            finally:
                conn.close()
            ftp.voidresp()
            return [ln.rstrip("\r")
                    for ln in buf.decode("utf-8", errors="replace").splitlines()
                    if ln.strip()]
        except (ssl.SSLError, OSError, ftplib.error_temp, EOFError) as e:
            last_exc = e
        finally:
            try:
                ftp.quit()
            except (OSError, ssl.SSLError, ftplib.error_perm, EOFError):
                try: ftp.close()
                except Exception: pass
        if attempt < retries - 1:
            time.sleep(1.0)
    raise last_exc if last_exc else RuntimeError("LIST failed with no exception")


def upload_file(creds: "Creds", local_path: Path,
                remote_name: str | None = None,
                remote_dir: str | None = None) -> None:
    """Upload a file to the printer's SD card via implicit FTPS.

    `remote_dir` (e.g. ``"cache"``) STORs into that subdirectory, creating it if
    needed. The LAN print flow uploads the .gcode.3mf to ``cache/`` because the
    firmware-accepted `print.project_file` URL is ``ftp:///cache/<file>`` — what
    Bambu Studio's desktop LAN flow does. With no `remote_dir` the file lands at
    the FTP root (back-compat)."""
    if not local_path.is_file():
        sys.exit(f"file not found: {local_path}")
    if remote_name is None:
        remote_name = local_path.name
    ftp = _manual_ftp_tls(creds, timeout=15.0)
    try:
        if remote_dir:
            try:
                ftp.cwd(remote_dir)
            except ftplib.error_perm:
                ftp.mkd(remote_dir)
                ftp.cwd(remote_dir)
        with local_path.open("rb") as f:
            ftp.storbinary(f"STOR {remote_name}", f, blocksize=32768)
    finally:
        try:
            ftp.quit()
        except (OSError, ssl.SSLError, ftplib.error_perm):
            try: ftp.close()
            except Exception: pass
