"""tests/test_cloud_client_error_paths.py — cloud_client._request error
handling.

`_request()` is the single HTTP-bottleneck for the entire CloudClient.
Its error paths govern how cleanly every cmd_cloud_* handler surfaces
network / API failures to the user. Audit (this session) found these
branches untested — they're P0 because they handle untrusted external
data (Bambu API responses).

What we pin down:
  * non-JSON 200 response → CloudError with status preserved
  * 4xx HTTPError → CloudError with status + Bambu's `message`/`error_msg`
    extracted from the JSON error body
  * 4xx with non-JSON body → CloudError using the raw body as message
  * URLError (DNS / connection refused) → CloudError (no status code)
  * gzip-encoded body → transparent decompression
  * deflate-encoded body → transparent decompression
  * empty body → returns {}  (TFA endpoint legitimate case)
  * Set-Cookie parsing on TFA path → cookies in `_cookies` key
"""
from __future__ import annotations

import gzip
import io
import sys
import urllib.error
import zlib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cloud_client


# ----- helpers ------------------------------------------------------------


class _FakeResponse:
    """Stand-in for `urlopen()`'s context manager response. Mimics the
    .read() / .headers / .status surface that _request consumes."""

    def __init__(self, body: bytes, *, status: int = 200,
                 headers: dict | None = None,
                 set_cookies: list[str] | None = None):
        self._body = body
        self.status = status
        self._headers = headers or {}
        self._set_cookies = set_cookies or []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def read(self) -> bytes: return self._body

    # Mimic the bits of urllib.response.addinfourl.headers we use.
    @property
    def headers(self):
        outer = self

        class _H:
            def get(self, key, default=None):
                return outer._headers.get(key, default)

            def get_all(self, key):
                if key == "Set-Cookie":
                    return outer._set_cookies
                return [outer._headers[key]] if key in outer._headers else []

        return _H()


def _patch_urlopen(monkeypatch, response_or_callable):
    """Replace urllib.request.urlopen with a stub. Pass either a
    _FakeResponse instance or a callable that takes the Request and
    returns one (so different tests can branch on URL)."""
    def _stub(req, timeout=None, context=None):
        if callable(response_or_callable):
            return response_or_callable(req)
        return response_or_callable

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", _stub)


# ===== 2xx happy path =====================================================


def test_request_parses_json_body(monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResponse(b'{"ok": true, "n": 7}'))
    out = cloud_client._request("GET", "https://example.com/api")
    assert out == {"ok": True, "n": 7}


def test_request_empty_body_returns_empty_dict(monkeypatch):
    """TFA endpoint legitimately returns 200 with no body and the token
    in a Set-Cookie. Empty body must NOT raise."""
    _patch_urlopen(monkeypatch, _FakeResponse(b""))
    out = cloud_client._request("POST", "https://example.com/tfa")
    assert out == {}


def test_request_whitespace_only_body_returns_empty_dict(monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResponse(b"   \n\t  "))
    out = cloud_client._request("GET", "https://example.com/api")
    assert out == {}


# ===== gzip / deflate decoding ===========================================


def test_request_decodes_gzip_body(monkeypatch):
    """Bambu's CDN often serves gzip even when we don't ask for it
    (server-side default). urllib doesn't auto-decode → _request must."""
    inner = b'{"hello": "world"}'
    gz = gzip.compress(inner)
    _patch_urlopen(monkeypatch, _FakeResponse(
        gz, headers={"Content-Encoding": "gzip"}))
    out = cloud_client._request("GET", "https://example.com/api")
    assert out == {"hello": "world"}


def test_request_decodes_deflate_body(monkeypatch):
    inner = b'{"a": 1}'
    df = zlib.compress(inner)
    _patch_urlopen(monkeypatch, _FakeResponse(
        df, headers={"Content-Encoding": "deflate"}))
    out = cloud_client._request("GET", "https://example.com/api")
    assert out == {"a": 1}


# ===== non-JSON response (CloudError, status preserved) ==================


def test_request_non_json_2xx_raises_cloud_error(monkeypatch):
    """200 OK with HTML body → CloudError, NOT a raw JSONDecodeError."""
    _patch_urlopen(monkeypatch, _FakeResponse(
        b"<html>maintenance</html>", status=200))
    with pytest.raises(cloud_client.CloudError) as exc:
        cloud_client._request("GET", "https://example.com/x")
    assert "non-JSON" in str(exc.value)
    # Status preserved → callers can branch on it.
    assert exc.value.status == 200
    assert "maintenance" in exc.value.body


# ===== 4xx HTTPError extraction ==========================================


def _make_http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    """Build a real HTTPError instance — it's what urlopen raises on 4xx."""
    return urllib.error.HTTPError(
        url="https://example.com/x", code=code,
        msg="reason", hdrs=None, fp=io.BytesIO(body),
    )


def test_request_4xx_extracts_bambu_message_field(monkeypatch):
    """Bambu returns `{"message":"<human-readable error>"}` on most 4xx.
    The user-facing error string must contain that message verbatim
    (so prints like `cloud API failed: HTTP 403 on ...: Forbidden`
    are actually informative)."""
    body = b'{"message": "Account suspended", "code": 7}'

    def _raise(req, timeout=None, context=None):
        raise _make_http_error(403, body)

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", _raise)

    with pytest.raises(cloud_client.CloudError) as exc:
        cloud_client._request("GET", "https://example.com/x")
    assert exc.value.status == 403
    assert "Account suspended" in str(exc.value)


def test_request_4xx_falls_back_to_error_msg_field(monkeypatch):
    """Older Bambu endpoints use `error_msg` instead of `message`."""
    body = b'{"error_msg": "token expired", "ec": 1}'

    def _raise(req, timeout=None, context=None):
        raise _make_http_error(401, body)

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", _raise)

    with pytest.raises(cloud_client.CloudError) as exc:
        cloud_client._request("GET", "https://example.com/x")
    assert exc.value.status == 401
    assert "token expired" in str(exc.value)


def test_request_4xx_with_non_json_body_uses_raw_text(monkeypatch):
    """5xx from a load balancer often returns plain text ("Bad Gateway").
    The error should still surface that text rather than crashing the
    JSON-extraction try/except."""
    def _raise(req, timeout=None, context=None):
        raise _make_http_error(502, b"Bad Gateway")

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", _raise)

    with pytest.raises(cloud_client.CloudError) as exc:
        cloud_client._request("GET", "https://example.com/x")
    assert exc.value.status == 502
    assert "Bad Gateway" in str(exc.value)


# ===== URLError (network layer) ==========================================


def test_request_url_error_raises_cloud_error_with_no_status(monkeypatch):
    """DNS failure / connection refused → CloudError with status==0."""
    def _raise(req, timeout=None, context=None):
        raise urllib.error.URLError("no host")

    monkeypatch.setattr(cloud_client.urllib.request, "urlopen", _raise)

    with pytest.raises(cloud_client.CloudError) as exc:
        cloud_client._request("GET", "https://nowhere.invalid/x")
    # Default status when CloudError constructed without one.
    assert exc.value.status == 0
    assert "network failure" in str(exc.value)
    assert "no host" in str(exc.value)


# ===== Set-Cookie parsing (TFA flow) ====================================


def test_request_return_cookies_extracts_set_cookie_values(monkeypatch):
    """`return_cookies=True` is used by the TFA flow: the token lands
    in a Set-Cookie header rather than the JSON body."""
    cookies = [
        "token=ABC123; Path=/; HttpOnly",
        "session=XYZ456; Secure",
    ]
    _patch_urlopen(monkeypatch, _FakeResponse(
        b'{"ok": true}', set_cookies=cookies))
    out = cloud_client._request("POST", "https://example.com/tfa",
                                return_cookies=True)
    assert "_cookies" in out
    assert out["_cookies"]["token"] == "ABC123"
    assert out["_cookies"]["session"] == "XYZ456"


def test_request_return_cookies_with_empty_body(monkeypatch):
    """TFA returns 200 with NO json body — just the cookie. We must
    return a dict containing only `_cookies`."""
    _patch_urlopen(monkeypatch, _FakeResponse(
        b"", set_cookies=["token=ABC; Path=/"]))
    out = cloud_client._request("POST", "https://example.com/tfa",
                                return_cookies=True)
    assert out == {"_cookies": {"token": "ABC"}}


# ===== CloudError __init__ ===============================================


def test_cloud_error_carries_status_and_body():
    e = cloud_client.CloudError("boom", status=418, body="i am a teapot")
    assert e.status == 418
    assert e.body == "i am a teapot"
    assert str(e) == "boom"


def test_cloud_error_default_status_zero():
    e = cloud_client.CloudError("network problem")
    assert e.status == 0
    assert e.body == ""
