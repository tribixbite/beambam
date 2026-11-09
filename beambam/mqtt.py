"""beambam.mqtt — signed MQTT primitives + X2DClient connection facade.

Canonical home as of v1.3.0:

    from beambam.mqtt import sign_payload, BAMBU_CERT_ID, X2DClient

Signing
-------
Bambu's Jan-2025+ firmware rejects MQTT messages whose `header.sign_string`
doesn't verify against a recognised RSA cert. `sign_payload()` here is the
**leaked-cert path**: it signs with the publicly-leaked Bambu Connect cert
(cert_id GLOF1000000000-...) shipped in `bambu_cert.py`. That satisfies
firmware that trusts the Bambu Connect chain, but NOT authorization-control
firmware's `print.*` check (which wants a cert whose CN matches the printer's
own serial) — for that, control routes through `beambam.cloud_control` using
the per-installation key recovered by `beambam key`. Usage of this path:

    payload = {"print": {"sequence_id": "0", "command": "pause"}}
    signed = sign_payload(payload)
    # signed["header"]["sign_string"] = "<base64 RSA-SHA256 over payload>"
    # signed["header"]["cert_id"]     = BAMBU_CERT_ID
    # signed["header"]["payload_len"] = len(compact-JSON of payload)

CRITICAL ORDERING: the signature is computed against the compact-JSON of
the un-headered dict in DICT-INSERTION ORDER. `sort_keys=True` breaks
ALL commands (pause/resume/print/etc.) because the firmware re-serializes
the parsed payload in receive order. Always use `json.dumps(payload,
separators=(",", ":"))` with no sort_keys.

X2DClient
---------
Long-lived MQTT connection facade. Phase 4 of the bridge split (v1.3.0)
moved this class out of x2d_bridge.py into here so it can be imported
without dragging the rest of the bridge into scope. `x2d_bridge.X2DClient`
is still a valid alias (re-exported) for any external caller.
"""
from __future__ import annotations

import base64
import collections
import copy
import json
import os
import ssl
import sys
import threading
import time
from pathlib import Path
from collections import deque
from threading import Condition, Event
from typing import Any, Callable

import paho.mqtt.client as mqtt

from beambam.config import Creds
from beambam.paths import config_dir
from beambam.state import CommandReply, command_reply, deep_merge, is_full_report


__all__ = [
    "BAMBU_CERT_ID",
    "PublishTimeout",
    "X2DClient",
    "sign_payload",
    # Metrics state (Phase 4): the daemon's Prometheus snapshot pulls
    # from these. Kept module-level so X2DClient instances + the HTTP
    # server share one counter table per process.
    "metric_inc",
    "metric_global_inc",
    "metrics_snapshot",
]


# Cert ID — public-leak constant. Falls back to the literal if the user's
# bambu_cert.py doesn't define it (some leaked copies omit it).
try:
    from bambu_cert import BAMBU_CERT_ID
except (ImportError, AttributeError):
    BAMBU_CERT_ID = "GLOF1000000000-524a37c80000c6a6a274a47b3281"


def _load_private_key():
    """Lazy-load + decode the RSA private key. Cached so subsequent
    sign_payload calls don't pay the parse cost."""
    try:
        from bambu_cert import BAMBU_PRIVATE_KEY_PEM
    except ImportError:
        BAMBU_PRIVATE_KEY_PEM = None
    if BAMBU_PRIVATE_KEY_PEM is None:
        sys.exit(
            "Bambu signing cert missing. Place the PEM-encoded private key in\n"
            "  bambu_cert.py:BAMBU_PRIVATE_KEY_PEM\n"
            f"next to this script. The cert is the publicly-leaked Bambu\n"
            f"Connect global cert — search public references for\n"
            f"`{BAMBU_CERT_ID}` if you need a copy."
        )
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(
        BAMBU_PRIVATE_KEY_PEM.encode(), password=None
    )


# Cache the parsed key after first call.
_PRIVATE_KEY = None


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a Bambu MQTT payload with the `header` block the X2D / H2D /
    refreshed P1+X1 firmware require. Returns a NEW dict with `header`
    appended; the input is not mutated."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        _PRIVATE_KEY = _load_private_key()

    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = _PRIVATE_KEY.sign(body, padding.PKCS1v15(), hashes.SHA256())
    out = dict(payload)
    out["header"] = {
        "sign_ver":     "v1.0",
        "sign_alg":     "RSA_SHA256",
        "sign_string":  base64.b64encode(sig).decode("ascii"),
        "cert_id":      BAMBU_CERT_ID,
        "payload_len":  len(body),
    }
    return out


# ---------------------------------------------------------------------------
# Per-printer metrics counters (item #38). Module-level so multiple
# X2DClients in the same process share state. Key is the printer serial.
# Reset to 0 on process start; not persisted across restarts.
# ---------------------------------------------------------------------------
_metrics_counters: dict[str, dict[str, int]] = collections.defaultdict(
    lambda: {"messages_total": 0, "mqtt_disconnects_total": 0,
             "mqtt_connects_total": 0})
_metrics_global: dict[str, int] = {"ssdp_notifies_total": 0}
_metrics_lock = threading.Lock()


def metric_inc(serial: str, name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics_counters[serial][name] = (
            _metrics_counters[serial].get(name, 0) + delta)


def metric_global_inc(name: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics_global[name] = _metrics_global.get(name, 0) + delta


def metrics_snapshot() -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    with _metrics_lock:
        return ({k: dict(v) for k, v in _metrics_counters.items()},
                dict(_metrics_global))


# Underscore-prefixed back-compat aliases (Phase 4 renames). External
# importers (network_shim, runtime/*, tests) may reference the
# `_metric_inc` names from the v1.2.0 monolith era; keep them resolvable.
_metric_inc = metric_inc
_metric_global_inc = metric_global_inc
_metrics_snapshot = metrics_snapshot


class PublishTimeout(ConnectionError):
    """The broker did not acknowledge a QoS-1 publish in time."""


class X2DClient:
    PORT = 8883
    # Persistent last-message timestamp (items #19, #37). Lets /healthz
    # report a meaningful age immediately after a daemon restart instead
    # of always-stale until the first new push arrives. Per-printer file
    # so multi-printer setups don't smash each other's timestamps.
    _TS_DIR = config_dir()

    @classmethod
    def _ts_path_for(cls, serial: str) -> Path:
        # Persist under ~/.x2d/last_message_ts_<serial>. Hash-fall-back
        # if serial contains chars unsafe for a filename. Empty serial
        # (shouldn't happen for a real X2DClient but guard anyway) falls
        # back to the legacy single-file name.
        if not serial:
            return cls._TS_DIR / "last_message_ts"
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in serial)
        return cls._TS_DIR / f"last_message_ts_{safe}"

    def __init__(self, creds: Creds,
                 on_state: Callable[[dict], None] | None = None):
        self.creds = creds
        self.on_state = on_state
        self._connected = Event()
        self._got_state = Event()
        # Last raw message (kept for callers that want exactly what arrived).
        self._latest_state: dict | None = None
        # Every report deep-merged into one document (beambam.state): deltas
        # and command replies no longer masquerade as the whole state.
        self._merged: dict[str, Any] = {}
        # Message counter + recent window so a caller can wait for "a reply
        # that arrived after I published" (wait_for / wait_reply).
        self._cond = Condition()
        self._msg_seq = 0
        self._recent: deque[tuple[int, dict[str, Any]]] = deque(maxlen=512)
        # Per-printer persist file path keyed by serial.
        self._ts_path = self._ts_path_for(creds.serial)
        # Restore last-known ts from disk so /healthz works on first
        # request post-restart. If the file is missing, malformed, or
        # claims a future time, fall through to "0" (treated as
        # "no message ever received").
        self._last_message_ts = 0.0
        try:
            ts = float(self._ts_path.read_text().strip())
            if 0 < ts <= time.time():
                self._last_message_ts = ts
        except (FileNotFoundError, ValueError, OSError):
            pass
        # One-time migration: if the legacy single-file path exists and
        # this serial has no per-printer file yet, inherit its timestamp.
        if self._last_message_ts == 0.0 and creds.serial:
            legacy = self._TS_DIR / "last_message_ts"
            if legacy.exists():
                try:
                    ts = float(legacy.read_text().strip())
                    if 0 < ts <= time.time():
                        self._last_message_ts = ts
                except (ValueError, OSError):
                    pass

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"x2d-bridge-{os.getpid()}",
            protocol=mqtt.MQTTv311,
        )
        # No CA verification — Bambu uses a self-signed device cert
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        self.client.tls_set_context(ssl_ctx)
        self.client.username_pw_set("bblp", creds.code)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # --- callbacks --------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            client.subscribe(f"device/{self.creds.serial}/report")
            self._connected.set()
            metric_inc(self.creds.serial, "mqtt_connects_total")
        else:
            print(f"[x2d-bridge] MQTT connect failed: rc={rc}",
                  file=sys.stderr)
            metric_inc(self.creds.serial, "mqtt_disconnects_total")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        with self._cond:
            self._latest_state = payload
            deep_merge(self._merged, payload)
            self._msg_seq += 1
            self._recent.append((self._msg_seq, payload))
            self._cond.notify_all()
        now = time.time()
        self._last_message_ts = now
        metric_inc(self.creds.serial, "messages_total")
        # Persist atomically (items #19 + #37 per-printer). Cheap — even
        # at 10 Hz this is a few hundred bytes/s of writeback. Keep the
        # parent dir mode exclusive (creds live there too).
        #
        # Concurrency (item #78): the tmp filename embeds PID + a random
        # suffix so a serve daemon writing the per-printer ts file at the
        # same time as a one-shot CLI doesn't race on `<file>.tmp`. Each
        # writer gets its own .tmp; os.replace into the canonical path is
        # atomic on POSIX so whichever writer's replace runs last wins
        # (and that's fine — we just want the most recent timestamp).
        tmp: Path | None = None
        try:
            self._ts_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._ts_path.with_suffix(
                self._ts_path.suffix
                + f".tmp.{os.getpid()}.{os.urandom(4).hex()}"
            )
            tmp.write_text(f"{now}\n")
            os.replace(tmp, self._ts_path)
            tmp = None  # successfully renamed; nothing to clean up
        except OSError as e:
            # Don't let a transient FS error kill the listener — log once.
            if not getattr(self, "_ts_persist_warned", False):
                print(f"[x2d-bridge] last_msg_ts persist failed: {e}",
                      file=sys.stderr)
                self._ts_persist_warned = True
            # Best-effort cleanup of any tmp we created mid-failure.
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        self._got_state.set()
        if self.on_state:
            try:
                self.on_state(payload)
            except Exception as e:  # don't kill the listener loop
                print(f"[x2d-bridge] on_state callback raised: {e}",
                      file=sys.stderr)

    @property
    def last_message_ts(self) -> float:
        """Wall-clock time of the most recent state push from the
        printer, or 0.0 if none received yet. Used by the daemon's
        /healthz endpoint to flag a silent disconnect."""
        return getattr(self, "_last_message_ts", 0.0)

    # --- public API -------------------------------------------------------

    def connect(self, timeout: float = 8.0) -> None:
        self.client.connect(self.creds.ip, self.PORT, keepalive=60)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            raise TimeoutError(
                f"MQTT did not connect to {self.creds.ip}:{self.PORT} "
                f"within {timeout}s")

    @property
    def message_seq(self) -> int:
        """Number of messages received so far; pass it as `since=` to
        wait_for()/wait_reply() to only consider later messages."""
        with self._cond:
            return self._msg_seq

    def merged_state(self) -> dict[str, Any]:
        """Deep copy of every report received so far, merged."""
        with self._cond:
            return copy.deepcopy(self._merged)

    def wait_for(self, predicate: Callable[[dict[str, Any]], Any], timeout: float,
                 since: int | None = None) -> dict[str, Any] | None:
        """Block until a message received after `since` (default: now)
        satisfies `predicate`; return it, or None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            cursor = self._msg_seq if since is None else since
            while True:
                for seq, payload in self._recent:
                    if seq > cursor:
                        cursor = seq
                        try:
                            if predicate(payload):
                                return payload
                        except Exception:  # a predicate bug must not kill the wait
                            continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def wait_reply(self, family: str, command: str, timeout: float,
                   since: int | None = None) -> CommandReply | None:
        """Wait for the printer's reply to `family.command` (e.g. "print",
        "project_file"); None if none arrives within `timeout`."""
        msg = self.wait_for(lambda m: command_reply(m, family, command) is not None,
                            timeout, since)
        return command_reply(msg, family, command) if msg is not None else None

    def request_state(self, timeout: float = 8.0) -> dict:
        """Send pushall and return the full merged state.

        Waits for a FULL report (one with `print.gcode_state`); a delta or a
        command reply that happens to arrive first is merged but not
        returned on its own, so callers always see `ams`, temperatures etc.
        Raises TimeoutError when no full report arrives in time.
        """
        since = self.message_seq
        self._got_state.clear()
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
        if self.wait_for(is_full_report, timeout, since=since) is None:
            with self._cond:
                have_full = is_full_report(self._merged)
            if not have_full:
                raise TimeoutError("no full state report received from printer")
        return self.merged_state()

    def publish(self, payload: dict, qos: int = 1, *,
                max_attempts: int = 3,
                backoff_base: float = 0.5) -> None:
        """Publish `payload` (signed) on the printer's request topic with
        retry-on-disconnect.

        One-shot CLIs (lan_print, x2d_bridge.py print/gcode/etc.) used to
        treat any MQTT hiccup between publish-call and broker-ack as a
        hard failure — the user got a stack trace and had to re-run.
        Now the client transparently retries up to `max_attempts` times
        with exponential backoff (base * 2**i seconds), reconnecting if
        the underlying paho client says the broker dropped us. The
        `serve` daemon's watchdog already handled this for long-lived
        sessions; this brings parity to one-shots.
        """
        import paho.mqtt.client as _mqtt  # for ERR_NO_CONN constant

        signed = sign_payload(payload)
        topic = f"device/{self.creds.serial}/request"
        body = json.dumps(signed, separators=(",", ":"))
        # Wire-level intercept (env-var controlled). Set X2D_WIRE_LOG=path
        # to capture every outbound MQTT publish — used to debug what
        # BambuStudio's GUI shim sends when its Print button is clicked,
        # vs what our CLI sends. One JSON line per publish; appended.
        wire_log = os.environ.get("X2D_WIRE_LOG", "")
        if wire_log:
            try:
                with open(wire_log, "a") as f:
                    f.write(json.dumps({
                        "ts":     time.time(),
                        "serial": self.creds.serial,
                        "topic":  topic,
                        "payload": signed,
                    }) + "\n")
            except OSError:
                pass
        last_err: Exception | None = None
        for attempt in range(max_attempts):
            try:
                if not self.client.is_connected():
                    # Reconnect before publishing; reuses the same
                    # paho.Client (keeps subscriptions, callbacks, IDs).
                    self._connected.clear()
                    try:
                        self.client.reconnect()
                    except Exception as e:
                        last_err = e
                        # Fall through to back-off-and-retry.
                        time.sleep(backoff_base * (2 ** attempt))
                        continue
                    if not self._connected.wait(timeout=5.0):
                        last_err = TimeoutError("reconnect timed out")
                        time.sleep(backoff_base * (2 ** attempt))
                        continue
                    metric_inc(self.creds.serial, "mqtt_reconnects_total")
                info = self.client.publish(topic, body, qos=qos)
                # rc==MQTT_ERR_NO_CONN means the broker isn't ready yet.
                if info.rc == _mqtt.MQTT_ERR_NO_CONN:
                    last_err = ConnectionError("MQTT_ERR_NO_CONN")
                    time.sleep(backoff_base * (2 ** attempt))
                    continue
                info.wait_for_publish(timeout=5)
                if qos > 0 and not info.is_published():
                    # paho's wait_for_publish returns silently on timeout.
                    # Don't re-send: the message may still be delivered,
                    # and a duplicated command (a print start) is worse
                    # than a reported failure.
                    raise PublishTimeout(
                        f"printer did not acknowledge the publish within 5 s ({topic})")
                if attempt > 0:
                    metric_inc(self.creds.serial,
                                "mqtt_publish_retries_total",
                                attempt)
                return
            except PublishTimeout:
                raise
            except (RuntimeError, ConnectionError, OSError) as e:
                # paho raises RuntimeError("The client is not currently connected.")
                # if the loop thread spotted a disconnect before our publish
                # made it to the wire.
                last_err = e
                time.sleep(backoff_base * (2 ** attempt))
        raise ConnectionError(
            f"MQTT publish failed after {max_attempts} attempts: {last_err}"
        )

    def publish_raw(self, topic: str, wire: str | bytes, qos: int = 1,
                    timeout: float = 5.0) -> None:
        """Publish an already-signed message and wait for the broker's ack.
        Raises PublishTimeout if it is not acknowledged within `timeout`."""
        info = self.client.publish(topic, wire, qos=qos)
        info.wait_for_publish(timeout=timeout)
        if qos > 0 and not info.is_published():
            raise PublishTimeout(
                f"printer did not acknowledge the publish within {timeout:g} s ({topic})")

    def disconnect(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
