"""beambam.http_cloud — HTTP-route helpers for cloud operations.

Phase 5e batch 4. These functions back the cloud-related HTTP routes
served by `x2d_bridge._serve_http`'s Handler class:

  POST /cloud/login         → _http_cloud_login
  POST /cloud/logout        → _http_cloud_logout
  GET  /cloud/status        → _http_cloud_status
  GET  /cloud/printers      → _http_cloud_printers
  GET  /cloud/state         → _http_cloud_state
  POST /cloud/publish       → _http_cloud_publish

Each returns either `(status_code, json_dict)` or a plain dict —
`_serve_http` adapts. Two-step interactive flows (email-verification,
2FA) are non-interactive over HTTP: the caller passes the code/key
as a JSON field in a follow-up POST after seeing the `requires_*`
flag in the first response.

x2d_bridge.py re-exports each name so `_serve_http`'s global-name
lookups inside its Handler class continue to resolve unchanged.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any


def _http_cloud_login(*, email: str, password: str,
                      region: str | None = None,
                      email_code: str | None = None,
                      tfa_code: str | None = None,
                      ) -> tuple[int, dict[str, Any]]:
    """HTTP-driven cloud-login. Returns the same status fields as
    /cloud/status on success, or a structured error.

    Two-step flows (verifyCode / tfa) are NOT interactive over HTTP —
    the caller passes `email_code` / `tfa_code` in a follow-up POST
    after seeing the corresponding `requires_*` flag in the first
    response. The cloud_client.login() callback uses the supplied
    fixed value rather than prompting via stdin."""
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    if not email or not password:
        return 400, {"error": ("expected {email: str, password: str, "
                                "region?: str, email_code?: str, "
                                "tfa_code?: str}")}
    cli = cloud_client.CloudClient.load_or_anonymous()
    requires_email_code = False
    requires_tfa = False

    def _email_resolver(_email: str) -> str:
        nonlocal requires_email_code
        if email_code is None:
            requires_email_code = True
            raise cloud_client.CloudError(
                "email-code required (re-POST with email_code)")
        return email_code

    def _tfa_resolver(_key: str) -> str:
        nonlocal requires_tfa
        if tfa_code is None:
            requires_tfa = True
            raise cloud_client.CloudError(
                "tfa code required (re-POST with tfa_code)")
        return tfa_code

    try:
        cli.login(email, password, region=region,
                  email_code_resolver=_email_resolver,
                  two_factor_resolver=_tfa_resolver)
    except cloud_client.CloudError as e:
        if requires_email_code:
            return 200, {"requires_email_code": True,
                         "hint": ("Bambu sent a verification code to "
                                  "your email; re-POST with email_code")}
        if requires_tfa:
            return 200, {"requires_tfa": True,
                         "hint": "TOTP required; re-POST with tfa_code"}
        return 401, {"error": f"login failed: {e}",
                     "status": e.status, "body": e.body}
    except Exception as e:
        return 500, {"error": f"login crashed: {e}"}
    return 200, {
        "logged_in":    True,
        "user_id":      cli.session.user_id,
        "region":       cli.session.region,
        "expires_at":   cli.session.expires_at,
        "expires_in_s": int(max(0, cli.session.expires_at
                                 - time.time())),
    }


def _http_cloud_logout() -> tuple[int, dict[str, Any]]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    cli.logout()
    return 200, {"logged_out": True}


def _http_cloud_status() -> dict[str, Any]:
    try:
        import cloud_client
    except ImportError as e:
        return {"error": f"cloud_client unavailable: {e}",
                "logged_in": False}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return {"logged_in": False}
    return {
        "logged_in":    True,
        "user_id":      cli.session.user_id,
        "region":       cli.session.region,
        "expired":      cli.session.expired,
        "expires_at":   cli.session.expires_at,
        "expires_in_s": int(max(0, cli.session.expires_at
                                 - time.time())),
    }


def _http_cloud_printers() -> tuple[int, dict[str, Any]]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return 401, {"error": "not logged in",
                     "hint": ("POST /cloud/login or run cloud-login "
                              "first")}
    try:
        return 200, {"printers": cli.get_bound_devices()}
    except cloud_client.CloudError as e:
        return 502, {"error": f"cloud API failed: {e}",
                     "status": e.status, "body": e.body}


def _http_cloud_state(serial: str | None,
                      timeout: float = 15.0,
                      ) -> tuple[int, dict[str, Any]]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    from beambam.cli._helpers import _next_seq
    from beambam.cli.cloud import _cloud_mqtt_connect

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return 401, {"error": "not logged in"}
    if not serial:
        try:
            devs = cli.get_bound_devices()
        except Exception as e:
            return 502, {"error": f"can't list bound devices: {e}"}
        if len(devs) == 1:
            serial = (devs[0].get("dev_id")
                      or devs[0].get("device_id"))
    if not serial:
        return 400, {"error": ("serial required (?serial=XXX) — "
                                "multiple printers bound")}
    topic_report  = f"device/{serial}/report"
    topic_request = f"device/{serial}/request"
    state_seen: dict[str, Any] = {}
    pushall_done = threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc != 0:
            return
        c.subscribe(topic_report, qos=0)
        c.publish(topic_request, json.dumps({
            "pushing": {"command": "pushall",
                        "sequence_id": _next_seq(),
                        "version": 1, "push_target": 1}
        }))

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"_raw": msg.payload.decode("utf-8",
                                                   errors="replace")}
        state_seen.update(payload)
        if any(k in payload for k in ("print", "system", "info")):
            pushall_done.set()

    c = _cloud_mqtt_connect(serial, cli)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    try:
        if not pushall_done.wait(timeout=timeout):
            return 504, {"error": f"timeout after {timeout}s",
                         "partial": state_seen, "serial": serial}
        return 200, {"serial": serial, "state": state_seen}
    finally:
        c.loop_stop()
        c.disconnect()


def _http_cloud_publish(serial: str, payload: dict,
                        timeout: float = 10.0,
                        ) -> tuple[int, dict[str, Any]]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    from beambam.cli.cloud import _cloud_mqtt_connect

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return 401, {"error": "not logged in"}
    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = threading.Event()

    def on_publish(client, userdata, mid,
                   reason_code=None, properties=None):
        published.set()

    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=timeout)
        if not published.wait(timeout=timeout):
            return 504, {"error": f"no broker ack in {timeout}s"}
        return 200, {"published": True,
                     "topic": topic_request,
                     "payload": payload}
    finally:
        c.loop_stop()
        c.disconnect()


__all__ = [
    "_http_cloud_login",
    "_http_cloud_logout",
    "_http_cloud_status",
    "_http_cloud_printers",
    "_http_cloud_state",
    "_http_cloud_publish",
]
