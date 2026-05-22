"""beambam.serve_socket — the Unix-socket JSON-RPC server BambuStudio's
`libbambu_networking.so` shim talks to.

The shim spawns `python3 x2d_bridge.py serve` as a child process and
opens a Unix domain socket the bridge listens on. The wire format is
documented in `runtime/network_shim/PROTOCOL.md`:

    req:  {"kind": "req", "id": <int>, "op": "<verb>", "args": {...}}
    rsp:  {"kind": "rsp", "id": <int>, "ok": <bool>, "result": {...} | "error": {...}}
    evt:  {"kind": "evt", "name": "<topic>", "data": {...}}

The protocol is byte-stable across the v1.3.x split — moving the
classes from x2d_bridge.py to this module changes the dispatch table
location but not the wire format. The E2E regression gate lives at
`runtime/network_shim/tests/test_shim_e2e.py`.

Layout:

  * `_OpError`             one-shot exception with a wire code
  * `_PrinterSession`      reference-counted MQTT subscription per
                           printer, fan-out to multiple shim conns
  * `ServeServer`          accept-loop + SSDP discovery + printer
                           registry. The thing `cmd_serve` runs.
  * `_ConnHandler`         one per accepted shim socket; reads req
                           lines, looks up `_OPS[op]`, sends rsp
  * `_op_*`                per-verb handlers; small, one per
                           PROTOCOL.md op

`_OPS` (the dispatch table) is built at module load. Adding a new op
means writing `_op_foo` + adding an entry to `_OPS` — same shape as
the old monolith file."""
from __future__ import annotations

import configparser
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

from beambam.config import Creds
from beambam.ftps import upload_file
from beambam.mqtt import X2DClient, metric_global_inc as _metric_global_inc

# `_next_seq` lives in beambam/cli/_helpers.py (Phase 5a). Used by the
# `pushall` publishes inside `_PrinterSession.acquire()` + `_op_subscribe_local`.
from beambam.cli._helpers import _next_seq

# Lazy-imported inside handlers to dodge the
# x2d_bridge → beambam.serve_socket → beambam.print_job → ... cycle on
# fresh interpreter starts:
#   PrintRefusal, _derive_print_params_from_3mf, _validate_ams_slot,
#   start_print
# All four come from beambam.print_job and are only touched by
# `_op_start_local_print`; deferring the import keeps module load fast.


# ---------------------------------------------------------------------------
# Wire-format constants — matches runtime/network_shim/PROTOCOL.md.
# ---------------------------------------------------------------------------

ABI_VERSION = 1
SHIM_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class _OpError(Exception):
    """Op handler failure — surfaces as `{ok:false, error:{code, message}}`."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Per-printer subscription session (reference-counted)
# ---------------------------------------------------------------------------


class _PrinterSession:
    """One live MQTT connection to a single printer plus a fan-out of state
    pushes to every shim that asked for it. Reference-counted so the
    underlying X2DClient closes only when the last shim disconnects."""

    def __init__(self, dev_id: str, dev_ip: str, code: str):
        from threading import Lock as _Lock
        self.dev_id = dev_id
        self.dev_ip = dev_ip
        self.code = code
        self._refcount = 0
        self._lock = _Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._connect_listeners: list[Callable[[int, str, str], None]] = []
        # Item #29: cache the most recent state push so a fresh shim
        # subscriber can replay it immediately and DeviceManager populates
        # MachineObject (AMS, temps, lights, etc.) without waiting up to
        # 30s for the next push.
        self._latest_state: dict | None = None
        self.client = X2DClient(
            Creds(ip=dev_ip, code=code, serial=dev_id),
            on_state=self._dispatch_state,
        )

    def _dispatch_state(self, payload: dict) -> None:
        with self._lock:
            self._latest_state = payload
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(payload)
            except Exception as e:  # one bad subscriber shouldn't poison others
                print(f"[serve] state listener raised: {e}", file=sys.stderr)

    def latest_state(self) -> dict | None:
        with self._lock:
            return self._latest_state

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    def add_connect_listener(self, fn: Callable[[int, str, str], None]) -> None:
        with self._lock:
            self._connect_listeners.append(fn)

    def remove_connect_listener(self, fn: Callable[[int, str, str], None]) -> None:
        with self._lock:
            try:
                self._connect_listeners.remove(fn)
            except ValueError:
                pass

    def _emit_connect(self, status: int, msg: str = "") -> None:
        with self._lock:
            listeners = list(self._connect_listeners)
        for fn in listeners:
            try:
                fn(status, self.dev_id, msg)
            except Exception as e:
                print(f"[serve] connect listener raised: {e}", file=sys.stderr)

    def acquire(self) -> None:
        with self._lock:
            first = self._refcount == 0
            self._refcount += 1
        if first:
            try:
                self.client.connect(timeout=8.0)
                self._emit_connect(0, "connected")  # ConnectStatusOk
                self.client.publish(
                    {"pushing": {"sequence_id": _next_seq(), "command": "pushall"}}
                )
            except Exception as e:
                self._emit_connect(1, str(e))  # ConnectStatusFailed
                raise _OpError(-2, f"connect failed: {e}") from e

    def release(self) -> None:
        with self._lock:
            self._refcount -= 1
            now_zero = self._refcount <= 0
        if now_zero:
            try:
                self.client.disconnect()
            finally:
                self._emit_connect(2, "lost")  # ConnectStatusLost


# ---------------------------------------------------------------------------
# Server — owns the Unix socket, the printer registry, and SSDP discovery.
# ---------------------------------------------------------------------------


class ServeServer:
    def __init__(self, sock_path: Path):
        self.sock_path = sock_path
        self._printers: dict[str, _PrinterSession] = {}
        self._printers_lock = __import__("threading").Lock()
        self._stop = Event()
        self._ssdp_listeners: list[Callable[[dict], None]] = []
        self._ssdp_lock = __import__("threading").Lock()
        # Cache of {dev_id: parsed_dict} so we can re-emit the most-recent
        # SSDP notify to a newly-connecting shim without waiting for the
        # printer's next 30-second broadcast.
        self._ssdp_cache: dict[str, dict] = {}
        self._ssdp_thread: Thread | None = None
        # Item #40: serial → (code, name) map loaded from ~/.x2d/credentials
        # so the SSDP loop can recognise our own printers when their NOTIFY
        # arrives and open the MQTT subscription proactively.
        self._known_creds: dict[str, tuple[str, str]] = self._load_known_creds()
        # Refcount holder: any session opened proactively from SSDP (item #40)
        # gets one persistent acquire() so the connection survives across
        # shim subscribe/unsubscribe cycles. Released on serve_forever exit.
        self._proactive_sessions: dict[str, _PrinterSession] = {}

    @staticmethod
    def _load_known_creds() -> dict[str, tuple[str, str]]:
        """Read every [printer] / [printer:NAME] section in
        ~/.x2d/credentials and return {serial: (code, name)}. Quietly
        returns {} if the file is missing or malformed — the bridge stays
        usable for unrecognised printers via the lazy shim path."""
        path = Path.home() / ".x2d" / "credentials"
        if not path.exists():
            return {}
        cp = configparser.ConfigParser()
        try:
            cp.read(path)
        except configparser.Error:
            return {}
        out: dict[str, tuple[str, str]] = {}
        for section in cp.sections():
            if section == "printer":
                name = ""
            elif section.startswith("printer:"):
                name = section.split(":", 1)[1]
            else:
                continue
            serial = cp.get(section, "serial", fallback="").strip()
            code = cp.get(section, "code", fallback="").strip()
            if serial and code:
                out[serial] = (code, name)
        return out

    # --- SSDP discovery -----------------------------------------------

    def add_ssdp_listener(self, fn: Callable[[dict], None]) -> None:
        with self._ssdp_lock:
            self._ssdp_listeners.append(fn)
            cache = list(self._ssdp_cache.values())
        # Replay the cache so a fresh shim sees existing devices immediately.
        for parsed in cache:
            try:
                fn(parsed)
            except Exception as e:
                print(f"[serve] ssdp replay raised: {e}", file=sys.stderr)

    def remove_ssdp_listener(self, fn: Callable[[dict], None]) -> None:
        with self._ssdp_lock:
            try:
                self._ssdp_listeners.remove(fn)
            except ValueError:
                pass

    def _ensure_ssdp_thread(self) -> None:
        if self._ssdp_thread and self._ssdp_thread.is_alive():
            return
        t = Thread(target=self._ssdp_loop, name="ssdp", daemon=True)
        t.start()
        self._ssdp_thread = t

    def _seed_appconfig_for_ssdp(self, parsed: dict) -> None:
        """Item #17: when we see the FIRST SSDP NOTIFY of the bridge's
        lifetime, ensure the user's BambuStudio.conf has a Bambu vendor
        preset selected. Without this, freshly-installed users land on
        the missing_connection.html fallback even though their printer
        is broadcasting itself.

        Idempotent: a marker file at ~/.x2d/.ssdp_seeded prevents
        re-patching across bridge restarts. Atomic write so a crash
        mid-write doesn't corrupt the user's AppConfig."""
        import os as _os
        marker = Path.home() / ".x2d" / ".ssdp_seeded"
        if marker.exists():
            return
        appconf = Path.home() / ".config" / "BambuStudioInternal" / "BambuStudio.conf"
        if not appconf.exists() or appconf.stat().st_size == 0:
            # No AppConfig yet — install.sh will seed it on next install
            # run. We can't sensibly create one out of nothing here.
            return
        try:
            data = json.loads(appconf.read_text())
        except (json.JSONDecodeError, OSError):
            return  # Don't touch a config we can't parse.
        presets = data.setdefault("presets", {})
        current = presets.get("printer", "")
        # If already on a Bambu vendor preset, leave alone.
        if current.lower().startswith("bambu lab"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            return
        # Patch the same gate keys install.sh #11 sets — defaults to the
        # X2D since that's what this toolkit is for. The upstream BBL
        # profile catalogue ships full X2D variants (machine, filaments,
        # 0.20mm Standard process), so the GUI lands directly on the
        # right model without the user having to pick.
        data.setdefault("vendors", {})["BBL"] = "1"
        models = data.get("models") or []
        if not any(m.get("vendor") == "BBL" for m in models):
            models.append({
                "vendor": "BBL",
                "model": "Bambu Lab X2D",
                "nozzle_diameter": '"0.4"',
            })
            data["models"] = models
        presets["printer"]   = "Bambu Lab X2D 0.4 nozzle"
        presets["filament"]  = "Bambu PLA Basic @BBL X2D"
        presets.setdefault("print", "0.20mm Standard @BBL X2D")
        if not isinstance(presets.get("filaments"), list) or not presets["filaments"]:
            presets["filaments"] = ["Bambu PLA Basic @BBL X2D"]
        # Atomic write
        tmp = appconf.with_suffix(appconf.suffix + ".tmp-x2d")
        tmp.write_text(json.dumps(data, indent=4))
        _os.replace(tmp, appconf)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        print(f"[serve] ssdp seed: patched {appconf} (printer→{presets['printer']}, "
              f"triggered by {parsed.get('dev_name', '?')} @ {parsed.get('dev_ip', '?')})",
              file=sys.stderr)

    def _seed_access_code(self, parsed: dict) -> None:
        """Write access_code / user_access_code / ip_address keyed by
        dev_id into BambuStudio.conf so the GUI auto-binds on SSDP.
        Re-runs on every NOTIFY (cheap and idempotent: same code +
        dev_id only flips the file when the IP changes).

        Looks up the access code in self._known_creds (populated from
        ~/.x2d/credentials at startup). If the SSDP'd dev_id isn't in
        creds, do nothing — we don't have the access code for that
        printer."""
        import os as _os
        dev_id = parsed.get("dev_id", "")
        dev_ip = parsed.get("dev_ip", "")
        if not (dev_id and dev_ip):
            return
        creds = self._known_creds.get(dev_id)
        if creds is None:
            return
        code, _name = creds
        for app_dir in ("BambuStudio", "BambuStudioInternal"):
            appconf = Path.home() / ".config" / app_dir / "BambuStudio.conf"
            if not appconf.exists() or appconf.stat().st_size == 0:
                continue
            try:
                data = json.loads(appconf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            changed = False
            for key in ("access_code", "user_access_code"):
                slot = data.setdefault(key, {})
                if not isinstance(slot, dict):
                    slot = {}
                    data[key] = slot
                if slot.get(dev_id) != code:
                    slot[dev_id] = code
                    changed = True
            slot_ip = data.setdefault("ip_address", {})
            if not isinstance(slot_ip, dict):
                slot_ip = {}
                data["ip_address"] = slot_ip
            if slot_ip.get(dev_id) != dev_ip:
                slot_ip[dev_id] = dev_ip
                changed = True
            app = data.setdefault("app", {})
            if app.get("user_last_selected_machine") != dev_id:
                app["user_last_selected_machine"] = dev_id
                changed = True
            if not changed:
                continue
            tmp = appconf.with_suffix(appconf.suffix + ".tmp-x2d-ac")
            tmp.write_text(json.dumps(data, indent=4))
            _os.replace(tmp, appconf)
            print(f"[serve] access-code seed: {appconf} dev_id={dev_id} "
                  f"ip={dev_ip}", file=sys.stderr)

    def _ssdp_loop(self) -> None:
        """Listen for Bambu's multicast NOTIFY broadcasts on UDP 2021
        and convert each into the JSON shape BambuStudio's
        DeviceManager::on_machine_alive expects."""
        import socket as _socket
        import struct as _struct
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM, _socket.IPPROTO_UDP)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            sock.bind(("", 2021))
            sock.setsockopt(_socket.IPPROTO_IP, _socket.IP_ADD_MEMBERSHIP,
                            _struct.pack("4sl",
                                         _socket.inet_aton("239.255.255.250"),
                                         _socket.INADDR_ANY))
            sock.settimeout(1.0)
        except OSError as e:
            print(f"[serve] ssdp bind failed: {e}", file=sys.stderr)
            return
        print("[serve] ssdp listening on udp/2021 (239.255.255.250)", file=sys.stderr)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except (_socket.timeout, BlockingIOError):
                continue
            except OSError:
                break
            parsed = self._parse_ssdp(data, addr[0])
            if parsed is None:
                continue
            with self._ssdp_lock:
                self._ssdp_cache[parsed["dev_id"]] = parsed
            _metric_global_inc("ssdp_notifies_total")
            with self._ssdp_lock:
                listeners = list(self._ssdp_listeners)
            # Item #40: proactive auto-connect. If this NOTIFY's USN
            # matches a credentials section's serial, open the MQTT
            # subscription before any shim asks. _PrinterSession is
            # refcounted, so a persistent acquire() here keeps the
            # connection live across shim subscribe/unsubscribe cycles
            # — and the cached state replay (#29) means the GUI's
            # StatusPanel populates within milliseconds of subscribe.
            try:
                self._maybe_auto_connect(parsed)
            except Exception as e:
                print(f"[serve] ssdp auto-connect failed: {e}", file=sys.stderr)
            # Fire-and-forget: ensure the AppConfig has a Bambu preset
            # so the GUI's Device tab works on first launch (#17).
            try:
                self._seed_appconfig_for_ssdp(parsed)
            except Exception as e:
                print(f"[serve] ssdp seed failed: {e}", file=sys.stderr)
            # Also seed access_code / user_access_code / ip_address /
            # user_last_selected_machine — runs every NOTIFY, idempotent.
            # This makes the GUI auto-bind without the user clicking
            # through the ConnectPrinterDialog (which has UX bugs on
            # the wx 3.3 / GTK build).
            try:
                self._seed_access_code(parsed)
            except Exception as e:
                print(f"[serve] access-code seed failed: {e}",
                      file=sys.stderr)
            for fn in listeners:
                try:
                    fn(parsed)
                except Exception as e:
                    print(f"[serve] ssdp listener raised: {e}", file=sys.stderr)

    @staticmethod
    def _parse_ssdp(data: bytes, src_ip: str) -> dict | None:
        """Extract the on_machine_alive fields from a Bambu NOTIFY.
        Format example:
            NOTIFY * HTTP/1.1\r\n
            Location: 192.168.x.y\r\n
            USN: <serial>\r\n
            DevModel.bambu.com: N6\r\n
            DevName.bambu.com: x2d\r\n
            DevConnect.bambu.com: cloud|lan\r\n
            DevBind.bambu.com: free|occupied\r\n
            Devseclink.bambu.com: secure\r\n
            DevVersion.bambu.com: 01.01.00.00\r\n
        """
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return None
        if not text.startswith("NOTIFY "):
            return None
        headers: dict[str, str] = {}
        for line in text.split("\r\n")[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        usn = headers.get("usn", "")
        if not usn:
            return None
        dev_ip = headers.get("location", src_ip) or src_ip
        connect_type = headers.get("devconnect.bambu.com", "lan").lower()
        if connect_type == "cloud":
            # The bridge replaces what the cloud plug-in would do, so
            # tell the host this is reachable as a LAN device.
            connect_type = "lan"
        return {
            "dev_name":       headers.get("devname.bambu.com", ""),
            "dev_id":         usn,
            "dev_ip":         dev_ip,
            "dev_type":       headers.get("devmodel.bambu.com", ""),
            "dev_signal":     "",  # Bambu doesn't advertise signal strength in SSDP
            "connect_type":   connect_type,
            "bind_state":     headers.get("devbind.bambu.com", "free").lower(),
            "sec_link":       headers.get("devseclink.bambu.com", ""),
            "ssdp_version":   headers.get("devversion.bambu.com", ""),
            "connection_name": "",
        }

    def _maybe_auto_connect(self, parsed: dict) -> None:
        """Item #40: open MQTT proactively when an SSDP NOTIFY matches a
        known credentials section. Idempotent — only one persistent
        acquire() per serial, so repeated NOTIFYs (every ~30s) don't
        rack up the refcount. IP changes are tolerated because
        get_or_open_printer rebuilds the session on mismatch."""
        dev_id = parsed.get("dev_id", "")
        dev_ip = parsed.get("dev_ip", "")
        if not dev_id or not dev_ip:
            return
        creds = self._known_creds.get(dev_id)
        if creds is None:
            return
        code, _name = creds
        with self._printers_lock:
            existing = self._proactive_sessions.get(dev_id)
            existing_ip = existing.dev_ip if existing else None
        # If we already hold a proactive ref AND IP is unchanged → done.
        if existing is not None and existing_ip == dev_ip:
            return
        # Either fresh or IP changed; acquire (will rebuild on IP mismatch).
        try:
            sess = self.get_or_open_printer(dev_id, dev_ip, code)
        except _OpError as e:
            print(f"[serve] auto-connect {dev_id}@{dev_ip} failed: {e}",
                  file=sys.stderr)
            return
        with self._printers_lock:
            stale = self._proactive_sessions.get(dev_id)
            self._proactive_sessions[dev_id] = sess
        # Drop the previous proactive ref now that the new one is in place.
        if stale is not None and stale is not sess:
            try:
                stale.release()
            except Exception:
                pass
        print(f"[serve] auto-connect {dev_id}@{dev_ip} (proactive, "
              f"matched creds section {_name or '<default>'!r})",
              file=sys.stderr)

    def _release_proactive_sessions(self) -> None:
        """Drop the persistent SSDP-driven refs at shutdown so MQTT
        connections close cleanly."""
        with self._printers_lock:
            sessions = list(self._proactive_sessions.values())
            self._proactive_sessions.clear()
        for sess in sessions:
            try:
                sess.release()
            except Exception:
                pass

    # --- printer registry ---------------------------------------------

    def get_or_open_printer(self, dev_id: str, dev_ip: str, code: str) -> _PrinterSession:
        with self._printers_lock:
            sess = self._printers.get(dev_id)
            if sess is None:
                sess = _PrinterSession(dev_id, dev_ip, code)
                self._printers[dev_id] = sess
            elif sess.dev_ip != dev_ip or sess.code != code:
                # IP/code changed under us — close old, open new.
                try:
                    sess.client.disconnect()
                except Exception:
                    pass
                sess = _PrinterSession(dev_id, dev_ip, code)
                self._printers[dev_id] = sess
        sess.acquire()
        return sess

    def release_printer(self, dev_id: str) -> None:
        with self._printers_lock:
            sess = self._printers.get(dev_id)
        if sess is not None:
            sess.release()

    # --- main loop ----------------------------------------------------

    def serve_forever(self) -> int:
        import socket
        from threading import Thread as _Thread

        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(str(self.sock_path))
        os.chmod(str(self.sock_path), 0o600)
        srv.listen(8)
        srv.settimeout(0.5)

        import signal as _signal
        def _stop_handler(signum, frame):  # noqa: ARG001
            self._stop.set()
        _signal.signal(_signal.SIGINT, _stop_handler)
        _signal.signal(_signal.SIGTERM, _stop_handler)

        # Start SSDP discovery up-front so the AppConfig auto-pop (#17)
        # fires even when no shim has connected yet (e.g. when run_gui.sh's
        # watchdog brought us up before bambu-studio's plug-in load).
        self._ensure_ssdp_thread()

        print(f"[serve] listening on {self.sock_path}", file=sys.stderr)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            handler = _ConnHandler(self, conn)
            t = _Thread(target=handler.run, name=f"shim-{handler.id}", daemon=True)
            t.start()

        srv.close()
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        # Drop SSDP-driven proactive refs (#40) before the bulk close
        # so refcounts don't underflow when we hit the disconnect loop.
        self._release_proactive_sessions()
        # Disconnect every active printer cleanly.
        with self._printers_lock:
            for sess in self._printers.values():
                try:
                    sess.client.disconnect()
                except Exception:
                    pass
        print("[serve] stopped cleanly", file=sys.stderr)
        return 0


# Monotonic counter for shim connection labels (logs only).
_conn_id = 0


class _ConnHandler:
    """One shim connection. Owns its socket; spawns no extra threads."""

    def __init__(self, server: ServeServer, sock):
        global _conn_id
        _conn_id += 1
        self.id = _conn_id
        self.server = server
        self.sock = sock
        self._write_lock = __import__("threading").Lock()
        self._subscribed: set[str] = set()
        self._state_cb: Callable[[dict], None] | None = None
        self._connect_cb: Callable[[int, str, str], None] | None = None
        self._ssdp_cb: Callable[[dict], None] | None = None

    # --- I/O primitives ----------------------------------------------

    def _send(self, obj: dict) -> None:
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            try:
                self.sock.sendall(line)
            except (BrokenPipeError, OSError):
                pass

    def _read_lines(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except (ConnectionResetError, OSError):
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line

    # --- callbacks injected into _PrinterSession ---------------------

    def _emit_local_message(self, dev_id: str, payload: dict) -> None:
        self._send({
            "kind": "evt",
            "name": "local_message",
            "data": {
                "dev_id": dev_id,
                "msg": json.dumps(payload, separators=(",", ":")),
            },
        })

    def _emit_local_connect(self, status: int, dev_id: str, msg: str) -> None:
        self._send({
            "kind": "evt",
            "name": "local_connect",
            "data": {"status": status, "dev_id": dev_id, "msg": msg},
        })

    # --- main loop ----------------------------------------------------

    def run(self) -> None:
        try:
            for raw in self._read_lines():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"[serve] bad json from shim: {e}", file=sys.stderr)
                    continue
                if msg.get("kind") != "req":
                    continue
                self._handle_request(msg)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        # Drop our subscriptions and release each printer ref.
        for dev_id in list(self._subscribed):
            sess = self.server._printers.get(dev_id)
            if sess is not None:
                if self._state_cb:
                    sess.remove_listener(self._state_cb)
                if self._connect_cb:
                    sess.remove_connect_listener(self._connect_cb)
                sess.release()
        if self._ssdp_cb is not None:
            self.server.remove_ssdp_listener(self._ssdp_cb)
            self._ssdp_cb = None
        try:
            self.sock.close()
        except OSError:
            pass

    def _handle_request(self, req: dict) -> None:
        op = req.get("op", "")
        args = req.get("args") or {}
        rid = req.get("id")
        handler = _OPS.get(op)
        if handler is None:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": -1, "message": f"unknown op: {op}"},
            })
            return
        try:
            result = handler(self, args)
            self._send({"kind": "rsp", "id": rid, "ok": True, "result": result})
        except _OpError as e:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": e.code, "message": str(e)},
            })
        except Exception as e:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": -128, "message": f"{type(e).__name__}: {e}"},
            })


# ---------------------------------------------------------------------------
# Op handlers — small, one per `op` in PROTOCOL.md
# ---------------------------------------------------------------------------

def _op_hello(h: _ConnHandler, args: dict) -> dict:
    abi = int(args.get("abi", 0))
    if abi != ABI_VERSION:
        raise _OpError(-100, f"abi mismatch: shim {abi}, bridge {ABI_VERSION}")
    return {"bridge_version": SHIM_VERSION, "abi": ABI_VERSION,
            "default_printer": None}


def _op_connect_printer(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", args.get("code", "")))
    if not (dev_id and dev_ip and code):
        raise _OpError(-1, "missing dev_id/dev_ip/password")
    sess = h.server.get_or_open_printer(dev_id, dev_ip, code)
    h._subscribed.add(dev_id)

    def listener(p: dict) -> None:
        h._emit_local_message(dev_id, p)

    sess.add_listener(listener)
    sess.add_connect_listener(h._emit_local_connect)
    h._state_cb = listener
    h._connect_cb = h._emit_local_connect
    return {}


def _op_disconnect_printer(h: _ConnHandler, args: dict) -> dict:
    for dev_id in list(h._subscribed):
        sess = h.server._printers.get(dev_id)
        if sess is not None:
            if h._state_cb:
                sess.remove_listener(h._state_cb)
            if h._connect_cb:
                sess.remove_connect_listener(h._connect_cb)
            sess.release()
        h._subscribed.discard(dev_id)
    h._state_cb = None
    h._connect_cb = None
    return {}


def _op_send_message_to_printer(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    payload_json = args.get("json", "")
    if not (dev_id and payload_json):
        raise _OpError(-1, "missing dev_id/json")
    sess = h.server._printers.get(dev_id)
    if sess is None:
        raise _OpError(-1, "printer not connected")
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except json.JSONDecodeError as e:
        raise _OpError(-19, f"invalid json payload: {e}") from e
    try:
        sess.client.publish(payload, qos=int(args.get("qos", 1)))
    except Exception as e:
        raise _OpError(-4, f"publish failed: {e}") from e
    return {}


def _op_start_local_print(h: _ConnHandler, args: dict) -> dict:
    # Lazy-import the print-job helpers to dodge the import cycle on
    # fresh interpreter starts (beambam.print_job → beambam.printer →
    # x2d_bridge → beambam.serve_socket).
    from beambam.print_job import (
        PrintRefusal,
        _derive_print_params_from_3mf,
        _validate_ams_slot,
        start_print,
    )
    dev_id = str(args.get("dev_id", ""))
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", ""))
    filename = str(args.get("filename", ""))
    if not (dev_id and dev_ip and code and filename):
        raise _OpError(-1, "missing dev_id/dev_ip/password/filename")
    creds = Creds(ip=dev_ip, code=code, serial=dev_id)
    local = Path(filename)
    if not local.is_file():
        raise _OpError(-14, f"file not found: {filename}")
    remote = local.name
    try:
        upload_file(creds, local, remote_name=remote)
    except Exception as e:
        raise _OpError(-20, f"FTPS upload failed: {e}") from e
    sess = h.server._printers.get(dev_id)
    if sess is None:
        # Auto-connect for fire-and-forget print flows.
        sess = h.server.get_or_open_printer(dev_id, dev_ip, code)
    ams_mapping_str = args.get("ams_mapping") or "[0]"
    try:
        ams_mapping = json.loads(ams_mapping_str) if isinstance(ams_mapping_str, str) else ams_mapping_str
    except json.JSONDecodeError:
        ams_mapping = [0]
    use_ams = bool(args.get("task_use_ams", True))
    # Defaults removed (per code-review #1): pre-existing
    # `task_bed_type="textured_plate"` fallback re-introduced the same
    # silent-misheat hole. start_print() will auto-derive from `local`
    # (the 3MF we just uploaded) when the GUI shim doesn't supply
    # task_bed_type / task_bed_temp.
    raw_bed = args.get("task_bed_type")
    bed_type_arg = str(raw_bed) if raw_bed else None
    raw_temp = args.get("task_bed_temp")
    # Per second-pass review NIT #6: don't crash the worker on
    # float-as-string. coerce defensively.
    try:
        bed_temp_arg = int(float(raw_temp)) if raw_temp is not None else None
    except (TypeError, ValueError):
        bed_temp_arg = None
    target_slot = ams_mapping[0] if ams_mapping else 0
    # Per code-review #1: parity with cmd_print — validate AMS state
    # before publishing. The serve-mode shim is the BS GUI's primary
    # path and was previously skipping this guard entirely.
    if use_ams:
        try:
            derived = _derive_print_params_from_3mf(local, filament_index=0)
        except PrintRefusal as e:
            raise _OpError(-4031, f"3MF derive failed: {e}") from e
        try:
            live = sess.client.request_state(timeout=15.0)
        except TimeoutError as e:
            raise _OpError(-4032, f"could not pull printer state: {e}") from e
        try:
            _validate_ams_slot(live, int(target_slot), derived, force=False)
        except PrintRefusal as e:
            raise _OpError(-4033, f"AMS slot validation failed: {e}") from e
    try:
        start_print(
            sess.client, remote,
            use_ams=use_ams,
            ams_slot=target_slot,
            bed_levelling=bool(args.get("task_bed_leveling", True)),
            flow_cali=bool(args.get("task_flow_cali", False)),
            timelapse=bool(args.get("task_record_timelapse", False)),
            vibration_cali=bool(args.get("task_vibration_cali", False)),
            bed_type=bed_type_arg,
            bed_temp=bed_temp_arg,
            local_path=local,
        )
    except PrintRefusal as e:
        raise _OpError(-4034, f"start_print refused: {e}") from e
    except Exception as e:
        raise _OpError(-4030, f"start_print MQTT failed: {e}") from e
    return {}


def _op_start_send_gcode_to_sdcard(h: _ConnHandler, args: dict) -> dict:
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", ""))
    filename = str(args.get("filename", ""))
    if not (dev_ip and code and filename):
        raise _OpError(-1, "missing dev_ip/password/filename")
    creds = Creds(ip=dev_ip, code=code, serial=str(args.get("dev_id", "")))
    local = Path(filename)
    if not local.is_file():
        raise _OpError(-14, f"file not found: {filename}")
    try:
        upload_file(creds, local, remote_name=local.name)
    except Exception as e:
        raise _OpError(-5010, f"FTPS upload failed: {e}") from e
    return {}


def _op_start_discovery(h: _ConnHandler, args: dict) -> dict:
    """Begin (or stop) SSDP listener; pipe each parsed device to this
    shim as `evt:ssdp_msg`. Idempotent — re-arming twice doesn't
    duplicate listeners."""
    enable = bool(args.get("start", True))
    if not enable:
        # Tear down this shim's listener.
        if h._ssdp_cb is not None:
            h.server.remove_ssdp_listener(h._ssdp_cb)
            h._ssdp_cb = None
        return {}

    h.server._ensure_ssdp_thread()
    if h._ssdp_cb is None:
        def emit(parsed: dict) -> None:
            h._send({
                "kind": "evt",
                "name": "ssdp_msg",
                "data": {"json": json.dumps(parsed, separators=(",", ":"))},
            })
        h._ssdp_cb = emit
        h.server.add_ssdp_listener(emit)
        # Replay every SSDP packet the bridge has seen so far so the
        # GUI's DeviceManager populates immediately instead of waiting
        # up to 30s for the next NOTIFY. This is the SSDP analogue of
        # the local_message latest-state replay (#29). Same shape as a
        # live ssdp_msg event so DeviceManager::on_machine_alive
        # processes them through the normal path.
        with h.server._ssdp_lock:
            cached_packets = list(h.server._ssdp_cache.values())
        for parsed in cached_packets:
            try:
                emit(parsed)
            except Exception as e:
                print(f"[serve] ssdp replay failed: {e}", file=sys.stderr)
    return {}


def _op_subscribe_local(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    interval = int(args.get("interval_s", 5))
    enable = bool(args.get("enable", True))
    sess = h.server._printers.get(dev_id) if dev_id else None
    if sess is None:
        raise _OpError(-1, "printer not connected")
    if enable:
        # Item #29: replay cached state immediately so DeviceManager
        # populates MachineObject (AMS slots, temps, lights, etc.)
        # without waiting up to 30s for the next live push. The cached
        # state was set by _PrinterSession._dispatch_state from a prior
        # MQTT push (typically the initial pushall after connect).
        cached = sess.latest_state()
        if cached is not None:
            try:
                h._emit_local_message(dev_id, cached)
            except Exception as e:
                print(f"[serve] state replay raised: {e}", file=sys.stderr)
        # The X2DClient already listens for state pushes once subscribed
        # in connect; kick a fresh pushall here for good measure.
        try:
            sess.client.publish(
                {"pushing": {"sequence_id": _next_seq(),
                             "command": "pushall"}},
            )
        except Exception as e:
            raise _OpError(-4, f"pushall publish failed: {e}") from e
    return {"interval_s": interval, "enable": enable}


def _op_get_version(h: _ConnHandler, args: dict) -> dict:
    return {"version": "02.06.00.50"}  # matches BAMBU_NETWORK_AGENT_VERSION


def _op_noop_ok(h: _ConnHandler, args: dict) -> dict:
    """Cloud-only entry points return success-with-empty so the GUI's
    paint paths don't choke on missing data."""
    return {}


def _cloud_client():
    """Lazy-load the cloud_client module + session. Returns None if the
    module isn't importable (older install without the file) so the
    bridge stays alive even when cloud is broken."""
    try:
        import cloud_client  # noqa: WPS433 — intentional lazy import
        return cloud_client.CloudClient.load_or_anonymous()
    except Exception as e:
        print(f"[serve] cloud_client unavailable: {e}", file=sys.stderr)
        return None


def _op_login_status(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    return {"logged_in": bool(cli and cli.is_logged_in())}


def _op_user_id(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            return {"id": cli.get_user_id()}
        except Exception as e:
            print(f"[serve] get_user_id failed: {e}", file=sys.stderr)
    return {"id": ""}


def _op_user_presets(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            return {"presets": cli.get_user_presets()}
        except Exception as e:
            print(f"[serve] get_user_presets failed: {e}", file=sys.stderr)
    # Anonymous fallback: load the BBL filament JSONs that ship with
    # bambu-studio plus a small community-curated set, so the GUI's
    # AMS spool dropdown isn't empty for users who haven't signed in.
    from beambam.presets import _load_local_presets
    return {"presets": _load_local_presets()}


def _op_user_tasks(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            limit = int(args.get("limit", 20))
            return {"tasks": cli.get_user_tasks(limit=limit)}
        except Exception as e:
            print(f"[serve] get_user_tasks failed: {e}", file=sys.stderr)
    return {"tasks": []}


_OPS: dict[str, Callable[["_ConnHandler", dict], dict]] = {
    "hello":                       _op_hello,
    "get_version":                 _op_get_version,
    "connect_printer":             _op_connect_printer,
    "disconnect_printer":          _op_disconnect_printer,
    "send_message_to_printer":     _op_send_message_to_printer,
    "start_local_print":           _op_start_local_print,
    "start_local_print_with_record": _op_start_local_print,
    "start_send_gcode_to_sdcard":  _op_start_send_gcode_to_sdcard,
    "subscribe_local":             _op_subscribe_local,
    "start_discovery":             _op_start_discovery,
    # cloud / catalog stubs
    "connect_server":              _op_noop_ok,
    "is_user_login":               _op_login_status,
    "get_user_id":                 _op_user_id,
    "get_user_presets":            _op_user_presets,
    "get_user_tasks":              _op_user_tasks,
    "start_print":                 _op_start_local_print,  # cloud → LAN
}
