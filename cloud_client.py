"""cloud_client — minimal Bambu Lab cloud REST client.

Wraps the public-knowledge endpoints (login, refresh, bind list, profile,
print history) so the shim's currently-stubbed cloud entry points
(`bambu_network_is_user_login`, `get_user_id`, `get_user_presets`,
`get_user_tasks`) can return real data when the user has logged in.

Endpoints reverse-engineered from open-source consumers of the same
API: pybambu (Home Assistant), bambu-farm-manager, bambu-node, OrcaSlicer.
None of this is from Bambu's own SDK — they don't publish one. If
Bambu rotates an endpoint, all of those projects break too; rotate
this module in lockstep.

Tokens live in ~/.x2d/cloud_session.json (chmod 600). The file holds
{access_token, refresh_token, expires_at, user_id, region}. We
auto-refresh when the access_token's `expires_at` is within 5 minutes.

Usage from CLI:
    x2d_bridge.py cloud-login --email me@x.com --password '…'
    x2d_bridge.py cloud-status

Usage from the bridge (dispatch):
    cli = CloudClient.load_or_anonymous()
    cli.is_logged_in()              # bool
    cli.get_user_id()               # str | None
    cli.get_user_presets()          # dict[name, dict]
    cli.get_user_tasks(limit=20)    # list[dict]
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Bambu has two regional clouds. The login endpoint disambiguates from
# the email's TLD; users can also force one via X2D_REGION=us|cn.
#
# `mqtt` is the cloud-side MQTT broker (TLS on 8883). It accepts
# username `u_<user_id>` + password `<access_token>` (the JWT). Topic
# layout matches the LAN-side broker: `device/<SN>/report` (printer →
# server, subscribe to read state) and `device/<SN>/request` (server →
# printer, publish print/pause/resume/etc commands). Brokers serve
# Let's-Encrypt-rooted TLS certs, so any client with a normal trust
# store works — no per-installation cert needed (which is what blocks
# us on the LAN-direct `print.*` path; #65/#66/#68).
REGIONS: dict[str, dict[str, str]] = {
    "us": {
        "api":  "https://api.bambulab.com",
        "iot":  "https://api.bambulab.com",
        "mqtt": "us.mqtt.bambulab.com",
    },
    "cn": {
        "api":  "https://api.bambulab.cn",
        "iot":  "https://api.bambulab.cn",
        "mqtt": "cn.mqtt.bambulab.com",
    },
}
MQTT_PORT = 8883

SESSION_PATH = Path.home() / ".x2d" / "cloud_session.json"
DEFAULT_TIMEOUT = 15

# Mimic OrcaSlicer's headers — Bambu's API sometimes 403s on bare requests
# behind Cloudflare. Verified working set from ha-bambulab and openhab-addons
# (Apr 2026). X-BBL-Region is intentionally NOT included; region is purely
# host-based (api.bambulab.com vs .cn) per all third-party clients.
USER_AGENT = "bambu_network_agent/01.09.05.01"
BBL_HEADERS = {
    "User-Agent":            USER_AGENT,
    "X-BBL-Client-Name":     "OrcaSlicer",
    "X-BBL-Client-Type":     "slicer",
    "X-BBL-Client-Version":  "01.09.05.51",
    "X-BBL-Language":        "en-US",
    "X-BBL-OS-Type":         "linux",
    "X-BBL-OS-Version":      "6.6.0",
    "X-BBL-Agent-Version":   "01.09.05.01",
    "X-BBL-Executable-info": "{}",
    "X-BBL-Agent-OS-Type":   "linux",
    "Accept":                "application/json",
    "Accept-Encoding":       "gzip, deflate",
}

# TFA (TOTP) endpoint lives on bambulab.com (not api.bambulab.com) and
# returns the token in a Set-Cookie header instead of JSON.
TFA_URL = "https://bambulab.com/api/sign-in/tfa"


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    # Unix-epoch seconds; 0 means "unknown / treat as expired".
    expires_at: float = 0.0
    user_id: str = ""
    region: str = "us"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        # 5-minute safety margin so a long-running call doesn't 401 mid-way.
        return self.access_token == "" or time.time() >= (self.expires_at - 300)

    @property
    def empty(self) -> bool:
        return not self.access_token

    def to_json(self) -> str:
        return json.dumps({
            "access_token":  self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at":    self.expires_at,
            "user_id":       self.user_id,
            "region":        self.region,
            "extra":         self.extra,
        }, indent=2)

    @classmethod
    def from_json(cls, blob: str) -> "Session":
        d = json.loads(blob)
        return cls(
            access_token=d.get("access_token", ""),
            refresh_token=d.get("refresh_token", ""),
            expires_at=float(d.get("expires_at", 0.0)),
            user_id=d.get("user_id", ""),
            region=d.get("region", "us"),
            extra=d.get("extra", {}),
        )


class CloudError(RuntimeError):
    """HTTP / API failure. Carries the status code so callers can branch
    on 401 (re-login) vs 5xx (transient)."""

    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


def _request(method: str,
             url: str,
             *,
             body: dict | None = None,
             headers: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT,
             return_cookies: bool = False) -> dict:
    """One HTTP round-trip returning parsed JSON. Raises CloudError on
    non-2xx or unparseable response. With return_cookies=True, the
    parsed JSON is augmented with a synthetic '_cookies' dict (used by
    the TFA endpoint, which delivers the token via Set-Cookie)."""
    h = dict(BBL_HEADERS)
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)

    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, method=method, data=data, headers=h)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            # Decode gzip/deflate manually since urllib doesn't auto-handle
            # Accept-Encoding when the server does honor it.
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                import zlib
                raw = zlib.decompress(raw)
            payload = raw.decode("utf-8", errors="replace")
            cookies = {}
            if return_cookies:
                # urllib doesn't expose Set-Cookie nicely; parse manually.
                from http.cookies import SimpleCookie
                for h_val in r.headers.get_all("Set-Cookie") or []:
                    sc = SimpleCookie()
                    sc.load(h_val)
                    for k, m in sc.items():
                        cookies[k] = m.value
            # Empty body is legal for some endpoints (e.g. TFA returns 200
            # with token in cookie and no body). Synthesize a dict.
            if not payload.strip():
                out = {}
            else:
                try:
                    out = json.loads(payload)
                except json.JSONDecodeError as e:
                    raise CloudError(f"non-JSON response: {payload[:200]}", r.status, payload) from e
            if return_cookies:
                out["_cookies"] = cookies
            return out
    except urllib.error.HTTPError as e:
        body_str = ""
        try:
            body_str = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        # Bambu returns JSON error bodies; pull the message if present.
        msg = body_str
        try:
            j = json.loads(body_str)
            msg = j.get("message") or j.get("error_msg") or body_str
        except Exception:
            pass
        raise CloudError(f"HTTP {e.code} on {method} {url}: {msg}",
                         status=e.code, body=body_str) from e
    except urllib.error.URLError as e:
        raise CloudError(f"network failure on {method} {url}: {e.reason}") from e


def _username_from_jwt(token: str) -> str:
    """Extract the `username` claim from a Bambu JWT (HS256-signed by
    Bambu, so we don't verify — just decode the unprotected payload)."""
    try:
        import base64 as _b64
        # JWT = header.payload.signature; payload is base64url-encoded JSON.
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = _b64.urlsafe_b64decode(parts[1] + pad).decode()
        d = json.loads(payload)
        return str(d.get("username") or d.get("uid") or d.get("sub") or "")
    except Exception:
        return ""


class CloudClient:
    """Thin wrapper around Session + the few endpoints we actually call.

    All methods return Python primitives (no SDK objects) so the bridge
    can JSON-serialize the responses straight back to the shim."""

    def __init__(self, session: Session | None = None):
        self.session = session or Session()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load_or_anonymous(cls, path: Path = SESSION_PATH) -> "CloudClient":
        """Load session from disk if present, otherwise return an empty
        client (every getter will report "not logged in")."""
        if path.exists():
            try:
                return cls(Session.from_json(path.read_text()))
            except (json.JSONDecodeError, KeyError, ValueError):
                # Corrupt session file — leave the file alone (user can
                # inspect it) and run anonymous. Don't crash the bridge.
                return cls(Session())
        return cls(Session())

    def save(self, path: Path = SESSION_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then atomically rename so a crash mid-write
        # doesn't leave an unparseable JSON.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.session.to_json())
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def logout(self, path: Path = SESSION_PATH) -> None:
        self.session = Session()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @staticmethod
    def dry_run_check(region: str = "us") -> dict:
        """Validate that the cloud endpoint is reachable WITHOUT
        sending credentials. Hits the login URL with a GET (instead
        of the required POST) — a 405 response proves DNS, TLS, and
        the API route all resolve. Useful as a smoke test from CI or
        during install.sh validation. Returns:
            {'ok': bool,
             'status': int (HTTP code, 0 on transport error),
             'region': str,
             'endpoint': str,
             'message': str}"""
        if region not in REGIONS:
            return {"ok": False, "status": 0, "region": region,
                    "endpoint": "",
                    "message": f"unknown region {region!r}; expected us|cn"}
        url = REGIONS[region]["api"] + "/v1/user-service/user/login"
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": USER_AGENT,
            "Accept":     "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT,
                                        context=ssl.create_default_context()) as r:
                # 200 here would be unexpected (login should require POST)
                # but still proves reachability.
                return {"ok": True, "status": r.status, "region": region,
                        "endpoint": url,
                        "message": f"unexpected success (status {r.status}); "
                                   "endpoint reached"}
        except urllib.error.HTTPError as e:
            # 405 (Method Not Allowed) is the EXPECTED response — proves
            # the path exists. 404 means Bambu rotated the route.
            ok = e.code in (405, 400, 401, 403)
            return {"ok": ok, "status": e.code, "region": region,
                    "endpoint": url,
                    "message": f"HTTP {e.code} from {url} — "
                               f"{'endpoint reachable' if ok else 'endpoint missing or moved'}"}
        except urllib.error.URLError as e:
            return {"ok": False, "status": 0, "region": region,
                    "endpoint": url,
                    "message": f"network error: {e.reason}"}

    def _resolve_region(self, email: str, region: str | None) -> str:
        if region:
            if region not in REGIONS:
                raise ValueError(f"unknown region {region!r}; expected us|cn")
            return region
        env = os.environ.get("X2D_REGION", "").strip().lower()
        if env in REGIONS:
            return env
        # Bambu's accounts are global but the API host is regional. Default
        # to .com (US/EU/Asia-ex-CN) unless the email TLD says otherwise.
        return "cn" if email.lower().endswith(".cn") else "us"

    def _commit_session(self, region: str, access: str, refresh: str,
                        expires_at: float, raw: dict) -> None:
        """Take a successful-login response and persist it as the active
        session. Centralised so the password / email-code / TFA paths all
        end up with identical session objects."""
        # Username from JWT is more reliable than the inconsistent
        # userId/uid response fields.
        uid = _username_from_jwt(access)
        if not uid:
            uid = str(raw.get("userId") or raw.get("uid") or "")
        self.session = Session(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            user_id=uid,
            region=region,
            extra={k: v for k, v in raw.items()
                   if k not in {"accessToken", "access_token",
                                "refreshToken", "refresh_token",
                                "expiresIn", "expires_in",
                                "expiresAt", "expires_at",
                                "_cookies"}},
        )
        self.save()

    def _expiry_from_response(self, r: dict) -> float:
        expires_in = r.get("expiresIn") or r.get("expires_in")
        expires_at = r.get("expiresAt") or r.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at > 0:
            return float(expires_at)
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            return time.time() + float(expires_in)
        # Bambu tokens last ~3 months in practice; default conservative
        # ttl of 1h forces a refresh probe sooner.
        return time.time() + 3600

    def login(self, email: str, password: str,
              region: str | None = None,
              *,
              two_factor_resolver: Callable[[str], str] | None = None,
              email_code_resolver: Callable[[str], str] | None = None) -> None:
        """Exchange email + password for an access_token / refresh_token
        pair. Region defaults to "us" unless email ends with .cn or
        the user passes region="cn".

        If the account requires 2FA (TOTP) or email-code verification,
        the matching resolver callback is invoked with a hint string and
        must return the 6-digit code. Pass None to fail-fast on those
        paths instead.
        """
        region = self._resolve_region(email, region)
        url = REGIONS[region]["api"] + "/v1/user-service/user/login"
        # Bambu's login API uses `account` (legacy field name).
        # `apiError` is harmless empty per ha-bambulab.
        body = {"account": email, "password": password, "apiError": ""}
        r = _request("POST", url, body=body)
        access = r.get("accessToken") or r.get("access_token") or ""
        if access:
            self._commit_session(
                region, access,
                r.get("refreshToken") or r.get("refresh_token") or "",
                self._expiry_from_response(r), r)
            return

        login_type = (r.get("loginType") or "").lower()
        if login_type == "verifycode":
            # Email-code flow: send the code, prompt for it, re-POST login.
            self._send_email_code(region, email)
            if not email_code_resolver:
                raise CloudError(
                    "account requires email verification code; pass "
                    "email_code_resolver=callback or use cli interactively",
                    status=200, body=str(r))
            code = email_code_resolver(email)
            self._submit_email_code(region, email, code)
            return
        if login_type == "tfa":
            tfa_key = r.get("tfaKey") or ""
            if not tfa_key:
                raise CloudError("loginType=tfa but no tfaKey in response",
                                 status=200, body=str(r))
            if not two_factor_resolver:
                raise CloudError(
                    "account requires 2FA TOTP code; pass "
                    "two_factor_resolver=callback or use cli interactively",
                    status=200, body=str(r))
            code = two_factor_resolver(email)
            self._submit_tfa_code(region, tfa_key, code)
            return
        # Anything else is genuinely broken auth.
        raise CloudError(
            "login response missing accessToken and unknown loginType",
            body=str(r))

    def login_code_only(self, email: str, region: str | None = None,
                        *, code_resolver: Callable[[str], str] | None = None) -> None:
        """Email-only "device code" style login — skip the password
        entirely.

        Flow:
          1. POST /v1/user-service/user/sendemail/code with type=codeLogin
             (Bambu sends a 6-digit code to the email address)
          2. Wait for the user to paste the code (via `code_resolver`)
          3. POST /v1/user-service/user/login with {account, code} and no
             password field, which exchanges the code for tokens

        This is the right path for `uvx beambam` fresh-OS users — typing
        a Bambu password into an arbitrary terminal is hostile UX. The
        email-code roundtrip proves possession of the inbox instead.
        """
        region = self._resolve_region(email, region)
        self._send_email_code(region, email)
        if not code_resolver:
            raise CloudError(
                "code-only login needs a code_resolver callback to read "
                "the 6-digit code from the user. Use cmd_cloud_login "
                "--code-only for interactive entry.",
                status=200, body="")
        code = code_resolver(email)
        self._submit_email_code(region, email, code)

    def _send_email_code(self, region: str, email: str) -> None:
        url = REGIONS[region]["api"] + "/v1/user-service/user/sendemail/code"
        _request("POST", url, body={"email": email, "type": "codeLogin"})

    def _submit_email_code(self, region: str, email: str, code: str) -> None:
        url = REGIONS[region]["api"] + "/v1/user-service/user/login"
        r = _request("POST", url, body={"account": email, "code": code})
        access = r.get("accessToken") or r.get("access_token") or ""
        if not access:
            raise CloudError(
                "email-code login response missing accessToken",
                body=str(r))
        self._commit_session(
            region, access,
            r.get("refreshToken") or r.get("refresh_token") or "",
            self._expiry_from_response(r), r)

    def _submit_tfa_code(self, region: str, tfa_key: str, code: str) -> None:
        # TFA endpoint is on bambulab.com (not api.bambulab.com), and the
        # token comes back in a Set-Cookie header instead of JSON. The
        # CN region's TFA endpoint is bambulab.cn (if it exists at all);
        # ha-bambulab uses the .com host for both regions so we mirror it.
        r = _request("POST", TFA_URL,
                     body={"tfaKey": tfa_key, "tfaCode": code},
                     return_cookies=True)
        token = (r.get("_cookies") or {}).get("token") or ""
        if not token:
            raise CloudError("TFA response missing token cookie", body=str(r))
        self._commit_session(
            region, access=token,
            refresh="",  # TFA path doesn't issue a refresh token
            expires_at=time.time() + 90 * 86400,  # ~3 months, observed default
            raw=r)

    def refresh(self) -> None:
        """Exchange the refresh_token for a new access_token. Raises
        CloudError if the refresh token is rejected — caller must
        re-login interactively."""
        if not self.session.refresh_token:
            raise CloudError("no refresh_token; need to login first")
        url = REGIONS[self.session.region]["api"] + "/v1/user-service/user/refreshtoken"
        r = _request("POST", url, body={"refreshToken": self.session.refresh_token})
        self.session.access_token  = r.get("accessToken")  or r.get("access_token")  or self.session.access_token
        self.session.refresh_token = r.get("refreshToken") or r.get("refresh_token") or self.session.refresh_token
        expires_in = r.get("expiresIn") or r.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            self.session.expires_at = time.time() + float(expires_in)
        else:
            self.session.expires_at = time.time() + 3600
        self.save()

    def is_logged_in(self) -> bool:
        return not self.session.empty

    def _ensure_fresh(self) -> None:
        """Refresh the access_token if it's about to expire. Idempotent."""
        if self.session.empty:
            raise CloudError("not logged in")
        if self.session.expired:
            self.refresh()

    def _authed_get(self, path: str) -> dict:
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + path
        return _request("GET", url, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    # ------------------------------------------------------------------
    # Endpoints — only the few the shim actually consumes
    # ------------------------------------------------------------------

    def get_user_id(self) -> str:
        if self.session.user_id:
            return self.session.user_id
        # Fallback: hit /my/profile to derive it.
        r = self._authed_get("/v1/design-user-service/my/profile")
        uid = str(r.get("uidStr") or r.get("uid") or r.get("userId") or "")
        if uid:
            self.session.user_id = uid
            self.save()
        return uid

    def get_bound_devices(self) -> list[dict]:
        """List the printers tied to this account. Each entry has
        dev_id, dev_name, online, online_status. The shim doesn't
        currently consume this but it's the basis for any future
        cloud-side print monitoring."""
        r = self._authed_get("/v1/iot-service/api/user/bind")
        return r.get("devices") or r.get("data") or []

    def get_user_presets(self) -> dict:
        """Cloud-synced filament + print + printer presets for the
        logged-in user. Shape mirrors Slic3r::PresetCollection: a dict
        with three keys (filament, print, printer), each a dict from
        preset name → preset settings."""
        r = self._authed_get("/v1/iot-service/api/user/preset")
        # Bambu's response groups by preset type; we re-key so the shim
        # can pass it straight to PresetCollection::load_user_presets.
        out: dict[str, dict] = {"filament": {}, "print": {}, "printer": {}}
        for item in r.get("presets") or r.get("data") or []:
            kind = (item.get("type") or "").lower()
            name = item.get("name") or item.get("setting_name") or ""
            if kind in out and name:
                out[kind][name] = item.get("setting") or item
        return out

    def get_user_tasks(self, limit: int = 20) -> list[dict]:
        """Print-task history (a.k.a. subtasks): every print job this user
        has run across all bound printers. Returns the `hits` array of
        task records — each has id (task_id), designId, designTitle,
        instanceId, deviceId, status, startTime, endTime, plate, cover
        URL, etc. Use task_id with get_task() to pull full project
        metadata + signed S3 URLs to the .3mf project file.

        Was previously misnamed/wired to /v1/iot-service/api/user/print
        which actually returns DEVICES. Corrected 2026-05-21."""
        r = self._authed_get(f"/v1/user-service/my/tasks?limit={int(limit)}")
        return r.get("hits") or []

    def get_task(self, task_id: int | str) -> dict:
        """Fetch a single print task by its numeric ID. Returns full task
        record including signed S3 URLs to the .3mf project file, plate
        JSONs, configs, and finish-snapshot picture. The signed URLs are
        ~1-hour valid. Task IDs come from FCM notifications or from the
        device's `task_id` field in MQTT push reports."""
        return self._authed_get(f"/v1/iot-service/api/user/task/{task_id}")

    def get_message_count(self) -> dict:
        """Notification badge counts across every Bambu surface — comment,
        device, design, system, IM, paid-content, crowdfunding,
        community. Returns the full breakdown dict."""
        return self._authed_get("/v1/user-service/my/message/count")

    def get_trouble_tickets(self, limit: int = 10) -> dict:
        """Customer-support ticket history for the logged-in user.
        Returns `{total: N, hits: [...]}` matching Bambu's standard
        list-paging convention. Each hit has troubleId, deviceId,
        startTime, endTime, classification, status, etc."""
        return self._authed_get(
            f"/v1/aftersale-service/trouble/list?limit={int(limit)}")

    def get_trouble_unread_count(self) -> int:
        return int(self._authed_get(
            "/v1/aftersale-service/trouble/totalunreadcount").get("count", 0))

    def get_makerworld_unread_count(self) -> int:
        return int(self._authed_get(
            "/v1/aftersale-service/makerworld/totalunreadcount").get("count", 0))

    def get_for_you(self, seed: int = 1, limit: int = 20) -> dict:
        """The MakerWorld 'For You' recommendation feed. `seed` is the
        client-supplied paging seed (any positive int); same seed returns
        the same page, fresh seed gets a fresh page. The breadcrumbs we
        captured use 32-bit random ints like 501846844 here."""
        return self._authed_get(
            f"/v1/design-recommend-service/my/for-you?seed={int(seed)}&limit={int(limit)}")

    def get_homepage_nav(self) -> dict:
        """Top-tab config for the MakerWorld homepage. Returns the list
        of tabs (Following / Foryou / Trending / Household / …)."""
        return self._authed_get("/v1/search-service/homepage/nav")

    def get_search_suggestions(self) -> list[str]:
        """Trending / personalized search-bar suggestions for this user.
        Bambu uses this to populate the search-suggestions dropdown — the
        list reflects what the user has searched for + tariff popular
        terms. Returns a list of suggestion strings."""
        r = self._authed_get("/v1/search-service/recommand/youlike")
        return r.get("sugList") or r.get("suggestions") or []

    def get_my_profile(self) -> dict:
        """My MakerWorld profile (uid, handle, name, avatar, bio,
        like-count, follow-count, fan-count, posted-design-count)."""
        return self._authed_get("/v1/design-user-service/my/profile")

    def get_points_progress(self) -> dict:
        """Bambu gamification points / progress breakdown."""
        return self._authed_get("/v1/point-service/point-bill/progress")

    def get_messages(self, limit: int = 20) -> dict:
        """Full inbox of MakerWorld + device + system notifications.
        Returns `{total, hits: [...]}` — each hit has id, type, taskMessage
        (if device-related), title, designId/Title, createdAt, isRead."""
        return self._authed_get(f"/v1/user-service/my/messages?limit={int(limit)}")

    def get_device_firmware_versions(self) -> list[dict]:
        """Per-device firmware version + available updates. Returns the
        `devices` list — each entry has dev_id, version (current),
        firmware (list of available versions with force_update flag)."""
        r = self._authed_get("/v1/iot-service/api/user/device/version")
        return r.get("devices") or []

    def get_device_status_cached(self) -> list[dict]:
        """Cloud-cached current state of every bound device. No MQTT
        round-trip needed. Returns the `devices` list with dev_online +
        recent state. Bambu serves this from its own MQTT broker's
        last-known-state cache, refreshed by the printer ~every 5s."""
        r = self._authed_get("/v1/iot-service/api/user/print?force=true")
        return r.get("devices") or []

    def get_filament_inventory(self) -> list[dict]:
        """User's spool/filament inventory (RFID-tracked + manually added).
        Each hit has filamentVendor, filamentType, filamentName, filamentId,
        RFID, createType (ams|manual), etc."""
        r = self._authed_get("/v1/design-user-service/my/filament/v2")
        return r.get("hits") or []

    def add_spool(self, body: dict) -> dict:
        """Add a new spool entry to the user's inventory.

        POSTs `body` to `/v1/design-user-service/my/filament/v2`. The
        canonical body shape mirrors `get_filament_inventory` hits:

            {"filamentVendor": "Bambu", "filamentType": "PLA Basic",
             "filamentName": "Galaxy Black", "filamentId": "GFB02",
             "color": "#0F0F0F", "weight": 1000, "createType": "manual"}

        Bambu doesn't document the schema publicly; the fields above are
        the ones their own GUI sends. Server-side validation may reject
        bodies missing required fields with HTTP 400 — surface as
        CloudError. WRITES the user's account state; gate via
        `--allow-write` at the CLI layer.
        """
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + \
              "/v1/design-user-service/my/filament/v2"
        return _request("POST", url, body=body, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    def update_spool(self, filament_id: str | int, body: dict) -> dict:
        """Update an existing spool entry by filamentId.

        PUTs `body` to `/v1/design-user-service/my/filament/v2/<id>`.
        `filament_id` is the spool's `filamentId` field (the key Bambu's
        own apps use for editing). WRITES; gate via `--allow-write`.
        """
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + \
              f"/v1/design-user-service/my/filament/v2/{filament_id}"
        return _request("PUT", url, body=body, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    def delete_spool(self, filament_id: str | int) -> dict:
        """Delete a spool entry by filamentId.

        DELETEs `/v1/design-user-service/my/filament/v2/<id>`. Empty
        200 on success (no body); failure surfaces as CloudError with
        the server's message. WRITES; gate via `--allow-write`.
        """
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + \
              f"/v1/design-user-service/my/filament/v2/{filament_id}"
        return _request("DELETE", url, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    def get_app_configuration(self) -> dict:
        """Global app config / feature-flag manifest (rating-reason
        categories, region-specific toggles, etc.). Useful as a feature-
        flag probe — exposes upcoming features Bambu has provisioned
        server-side ahead of client releases."""
        return self._authed_get("/v1/operation-service/configuration")

    def get_ttcode(self, dev_id: str) -> dict:
        """Throughtek (TUTK) P2P NAT-traversal codes for cloud camera
        streaming. Returns the time-limited credentials that pair the
        Handy / Studio client to the printer's onboard Throughtek video
        SDK. Standard call shape per Doridian/OpenBambuAPI cloud-http.md."""
        return self._authed_get(
            f"/v1/iot-service/api/user/ttcode?dev_id={urllib.parse.quote(dev_id)}")

    # ------------------------------------------------------------------
    # MakerWorld design + search endpoints (read-only)
    # ------------------------------------------------------------------

    def get_design(self, design_id: int | str) -> dict:
        """Full design record: title, slug, creator, instances list, tags,
        like/download/print counts, cover URLs. The .3mf bundle download
        URL is in `instances[*].downloadUrl` (signed S3, 1h)."""
        return self._authed_get(f"/v1/design-service/design/{design_id}")

    def get_design_remixes(self, design_id: int | str) -> dict:
        """Remixes (derivative works) of a design. `{total, hits}` shape."""
        return self._authed_get(f"/v1/design-service/design/{design_id}/remixed")

    def get_favorites(self) -> dict:
        """User's MakerWorld favorites lists. Each list has id, title,
        cover, count of designs in it. Use the list id with a follow-up
        call to fetch its contents (TODO endpoint)."""
        return self._authed_get("/v1/design-service/my/favorites/listlite")

    def get_liked_designs(self) -> dict:
        """Designs the user has liked. `{total, hits: [...]}`."""
        return self._authed_get("/v1/design-service/my/design/like")

    def get_slicer_presets(self, version: str = "01.10.00.69",
                           public: bool = False) -> dict:
        """User's cloud-synced slicer presets (print + filament + printer).
        `public=false` returns only the user's private settings; `true`
        includes Bambu's shipped public ones."""
        return self._authed_get(
            f"/v1/iot-service/api/slicer/setting?public={'true' if public else 'false'}"
            f"&version={urllib.parse.quote(version)}")

    def browse_designs_by_nav(self, nav_key: str = "Trending",
                              limit: int = 20, offset: int = 0) -> dict:
        """Browse MakerWorld designs by nav key. Standard nav keys (from
        homepage_nav): Following, Foryou, Trending, Household,
        Toys & Games, Art & Design, Tools, Education, Hobby & DIY, Sports &
        Outdoors, Fashion, Replacement Parts. Each entry is a design hit."""
        return self._authed_get(
            f"/v1/search-service/select/design/nav?navKey={urllib.parse.quote(nav_key)}"
            f"&limit={int(limit)}&offset={int(offset)}")

    def search_designs(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        """MakerWorld full-text search.

        Endpoint: `/v1/search-service/search/design?keyword=<q>`. (Note:
        the obvious-looking `/v1/search-service/select/design?query=` is
        a different endpoint that returns trending-feed-style results and
        IGNORES the query string — every query gets the same response.)

        Returns `{total, hits, suggest}` where each hit has id, title,
        cover, designCreator, likeCount, downloadCount, etc."""
        return self._authed_get(
            f"/v1/search-service/search/design?keyword={urllib.parse.quote(query)}"
            f"&limit={int(limit)}&offset={int(offset)}")

    def toggle_design_like(self, design_id: int | str) -> dict:
        """Toggle the like state on a MakerWorld design. The endpoint is
        idempotent-toggle: first POST likes, second POST un-likes.
        Returns empty 200 on success (body is empty)."""
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + \
              f"/v1/design-service/design/{design_id}/like"
        return _request("POST", url, body={}, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    def get_design_comments(self, design_id: int | str,
                            limit: int = 20, offset: int = 0) -> dict:
        """Comments + ratings for a MakerWorld design. Returns
        `{total, hits}` with each hit containing id, content,
        starRating, createTime, user info, and reply count."""
        return self._authed_get(
            f"/v1/comment-service/commentandrating?designId={design_id}"
            f"&limit={int(limit)}&offset={int(offset)}")

    def reply_to_comment(self, comment_id: int | str, text: str) -> dict:
        """Post a reply to an existing MakerWorld comment.

        Wraps POST `/v1/comment-service/comment/{commentId}/reply` with
        a `{"content": "..."}` body. Returns the new reply record on
        success (containing id, content, parentId, replyTo, createTime,
        user info). Empty text raises CloudError up-front; the server
        also rejects empties with 400 but we short-circuit so the user
        isn't confused by an opaque API error.
        """
        if not text or not text.strip():
            raise CloudError("comment reply text cannot be empty")
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + \
              f"/v1/comment-service/comment/{comment_id}/reply"
        return _request("POST", url, body={"content": text}, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    def get_instance_download_url(self, instance_id: int | str,
                                   kind: str = "download") -> dict:
        """Resolve the signed download URL for a MakerWorld design instance.
        `kind="download"` returns the full .3mf bundle; `kind="preview"`
        returns a preview-only .3mf (no plate gcode). The signed URL is
        valid for ~5 minutes (`exp` parameter in the URL).

        Response shape: `{"name": "<title>.3mf", "url": "https://makerworld.bblmw.com/...?exp=...&key=..."}`
        """
        return self._authed_get(
            f"/v1/design-service/instance/{instance_id}/f3mf?type={kind}")

    def pull_design_3mf(self, design_id: int | str, dest_dir: Path | str,
                        instance_index: int = 0) -> Path:
        """Convenience: given a MakerWorld designId, locate its default
        instance (or instance #instance_index), resolve its signed
        download URL, and `urlretrieve` the .3mf to `dest_dir/<title>.3mf`.

        Returns the Path of the downloaded file. Raises CloudError on
        any HTTP failure.

        Note on rate-limiting: Bambu's f3mf endpoint triggers an
        anti-bot captcha challenge (HTTP 418 with `captchaId` in the
        body) after roughly 10 programmatic downloads from the same
        IP in a short window. Real BS Studio + Handy don't hit this
        because they typically only download 1-2 bundles per session
        and humans solve captchas in the GUI. For now there's no
        non-interactive workaround — the caller should cache locally
        and avoid repeated downloads. If you hit 418, wait ~hour or
        log in via the Bambu Studio GUI which clears the flag."""
        d = self.get_design(design_id)
        instances = d.get("instances") or []
        if not instances:
            raise CloudError(f"design {design_id} has no instances")
        # Honour isDefault if present; otherwise use instance_index.
        chosen = None
        for inst in instances:
            if inst.get("isDefault"):
                chosen = inst; break
        if chosen is None:
            chosen = instances[min(instance_index, len(instances) - 1)]
        inst_id = chosen.get("id")
        if not inst_id:
            raise CloudError(f"instance for design {design_id} missing id field")
        meta = self.get_instance_download_url(inst_id)
        url = meta.get("url")
        name = meta.get("name") or f"design_{design_id}.3mf"
        if not url:
            raise CloudError(f"no signed download URL for instance {inst_id}")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize filename — Bambu's "name" field can contain spaces, slashes etc.
        safe = "".join(c if c.isalnum() or c in (".", "_", "-") else "_" for c in name)
        out = dest_dir / safe
        req = urllib.request.Request(url, headers={"User-Agent": "beambam-cloud/1.x"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out.write_bytes(r.read())
        return out

    # ------------------------------------------------------------------
    # Cloud-side MQTT broker — for cloud-mediated print control + state
    # streaming. Uses the same JWT we got from login().
    #
    # Auth scheme (verified against pybambu / openhab-addons / OrcaSlicer):
    #   broker:    {us,cn}.mqtt.bambulab.com:8883
    #   tls:       standard (Let's-Encrypt root chain — no per-installation
    #              cert needed, which is what blocks LAN-direct `print.*`)
    #   username:  "u_<numeric_user_id>"
    #   password:  "<access_token>" (the JWT, sent in cleartext over TLS)
    #   topics:    device/<SN>/report (subscribe — printer state)
    #              device/<SN>/request (publish — control commands)
    #
    # The cloud broker accepts `print.project_file` with `print_type=cloud`,
    # which the LAN-direct path rejects with `mqtt message verify failed`
    # (#65). Cloud uploads via OSS first (separate endpoint, see
    # cloud_get_upload_token below) and the printer pulls from there.
    # ------------------------------------------------------------------

    def mqtt_broker(self) -> str:
        """Hostname of the cloud-side MQTT broker for this session's region."""
        return REGIONS[self.session.region]["mqtt"]

    def mqtt_credentials(self) -> tuple[str, str]:
        """Returns (username, password) suitable for the cloud broker.
        Auto-refreshes the access_token if it's about to expire."""
        self._ensure_fresh()
        uid = self.get_user_id() or self.session.user_id
        if not uid:
            raise CloudError("cannot derive MQTT username — user_id missing")
        return f"u_{uid}", self.session.access_token

    def cloud_get_upload_token(self, count: int = 1) -> dict:
        """Get a one-shot OSS upload credential the printer can pull from.

        Two response shapes have been observed in the wild (Bambu's API
        versioning is murky; pybambu / openhab-addons handle both):

          shape A (presigned PUT URL — easier path):
            {
              "url":      "https://<bucket>.oss-<region>.aliyuncs.com/<path>?<sig>",
              "fileName": "<fileSavePath>",
              "size":     <bytes>,                 # optional max
              "expireAt": <unix_seconds>
            }
          shape B (STS-style — caller must HMAC-SHA1 sign):
            {
              "accessKeyId":     "<sts_key>",
              "accessKeySecret": "<sts_secret>",
              "securityToken":   "<sts_token>",
              "expiration":      "<iso8601>",
              "bucket":          "<bucket>",
              "region":          "<region>",
              "fileSavePath":    "<dest_path>"
            }

        Caller can dispatch on `"url" in r` to pick the upload method.

        Once uploaded, send the printer `print.project_file` with
        `print_type=cloud` and `url=cloud://<bucket>/<fileSavePath>`.
        Bambu's firmware then pulls via the cloud channel — sidesteps
        the LAN-direct verify-failure entirely."""
        return self._authed_get(
            f"/v1/iot-service/api/user/file/oss/upload-token?count={int(count)}"
        )

    def cloud_upload_file(self, file_path: str | Path, *,
                          token: dict | None = None) -> dict:
        """End-to-end: get an OSS upload token, PUT the local file to OSS,
        return the cloud://<bucket>/<path> URL the printer will pull from
        plus MD5 + size for the print.project_file payload.

        Returns a dict ready to merge into the MQTT payload:
            {
              "url":  "cloud://<bucket>/<file_save_path>",
              "md5":  "<hex>",
              "size": <bytes>,
              "remote_name": "<basename>"
            }
        """
        import hashlib
        path = Path(str(file_path))
        if not path.is_file():
            raise CloudError(f"file not found: {path}")
        body = path.read_bytes()
        md5 = hashlib.md5(body).hexdigest()

        if token is None:
            token = self.cloud_get_upload_token(count=1)

        # Shape A — presigned URL. Just PUT to it.
        if isinstance(token, dict) and token.get("url") and "Signature" in token.get("url", ""):
            put_url = token["url"]
            file_save_path = token.get("fileName") or token.get("fileSavePath") or path.name
            req = urllib.request.Request(
                put_url, data=body, method="PUT",
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(len(body))},
            )
            try:
                resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT * 4)
                if resp.status not in (200, 201, 204):
                    raise CloudError(f"OSS PUT returned {resp.status}",
                                     status=resp.status)
            except urllib.error.HTTPError as e:
                raise CloudError(f"OSS PUT failed: {e}", status=e.code,
                                 body=e.read().decode("utf-8", errors="replace")) from None

            # Bucket is embedded in the URL host: <bucket>.oss-<region>.aliyuncs.com
            bucket = ""
            try:
                host = urllib.parse.urlparse(put_url).hostname or ""
                if host.endswith(".aliyuncs.com"):
                    bucket = host.split(".", 1)[0]
            except Exception:
                pass
            return {
                "url":          f"cloud://{bucket}/{file_save_path}" if bucket else put_url,
                "md5":          md5,
                "size":         len(body),
                "remote_name":  path.name,
            }

        # Shape B — STS credentials. Need HMAC-SHA1 signed Authorization.
        # Aliyun OSS V1 signing: per the Aliyun docs, the canonical string is:
        #   VERB + "\n" + Content-MD5 + "\n" + Content-Type + "\n" +
        #   Date + "\n" + CanonicalizedOSSHeaders + CanonicalizedResource
        # where CanonicalizedOSSHeaders includes lowercase x-oss-* headers
        # sorted by header name; for STS, x-oss-security-token is required.
        import hmac as _hmac, hashlib as _hashlib, base64 as _base64
        from email.utils import formatdate as _formatdate
        if not all(k in token for k in ("accessKeyId", "accessKeySecret",
                                        "bucket", "fileSavePath")):
            raise CloudError(f"upload-token shape unrecognised: {sorted(token.keys())}")
        ak    = token["accessKeyId"]
        sk    = token["accessKeySecret"]
        sts   = token.get("securityToken", "")
        bucket = token["bucket"]
        region = token.get("region", "cn-shanghai")
        file_save_path = token["fileSavePath"]
        date  = _formatdate(usegmt=True)
        ctype = "application/octet-stream"
        cmd5  = _base64.b64encode(_hashlib.md5(body).digest()).decode()
        oss_hdrs = ""
        if sts:
            oss_hdrs = f"x-oss-security-token:{sts}\n"
        canon = (f"PUT\n{cmd5}\n{ctype}\n{date}\n{oss_hdrs}"
                 f"/{bucket}/{file_save_path}")
        sig = _base64.b64encode(_hmac.new(sk.encode(), canon.encode(),
                                          _hashlib.sha1).digest()).decode()
        host = f"{bucket}.oss-{region}.aliyuncs.com"
        url  = f"https://{host}/{file_save_path}"
        headers = {
            "Date":          date,
            "Content-Type":  ctype,
            "Content-MD5":   cmd5,
            "Authorization": f"OSS {ak}:{sig}",
        }
        if sts:
            headers["x-oss-security-token"] = sts
        req = urllib.request.Request(url, data=body, method="PUT", headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT * 4)
            if resp.status not in (200, 201, 204):
                raise CloudError(f"OSS PUT returned {resp.status}", status=resp.status)
        except urllib.error.HTTPError as e:
            raise CloudError(f"OSS PUT failed: {e}", status=e.code,
                             body=e.read().decode("utf-8", errors="replace")) from None
        return {
            "url":          f"cloud://{bucket}/{file_save_path}",
            "md5":          md5,
            "size":         len(body),
            "remote_name":  path.name,
        }
