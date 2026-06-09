"""tests/test_cloud_client_new_endpoints.py — coverage for the 16 new
read-only cloud endpoints added this session, plus 3 write endpoints.

Validates URL shape + parameter ordering + response unwrapping for:
  - get_user_tasks (the corrected /v1/user-service/my/tasks one)
  - get_task / get_design / get_design_remixes
  - get_message_count / get_messages
  - get_trouble_tickets / get_trouble_unread_count / get_makerworld_unread_count
  - get_for_you / get_homepage_nav / get_search_suggestions
  - get_my_profile / get_points_progress
  - get_device_firmware_versions / get_device_status_cached
  - get_filament_inventory / get_app_configuration
  - get_favorites / get_liked_designs / get_slicer_presets
  - browse_designs_by_nav / search_designs
  - get_design_comments / toggle_design_like
  - get_instance_download_url
  - login_code_only

HTTP is mocked — no real Bambu account needed. Validates we send the
RIGHT URLs and unwrap the responses correctly.
"""
from __future__ import annotations
import json
import sys
import time
import unittest.mock as mock
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cloud_client


def _logged_in_client(monkeypatch) -> cloud_client.CloudClient:
    """Build a CloudClient with a non-empty session so _ensure_fresh
    + _authed_get don't bail. Token never hits the network."""
    s = cloud_client.Session(
        access_token="AT_test", refresh_token="RT_test",
        expires_at=time.time() + 3600, user_id="2000000001", region="us",
    )
    c = cloud_client.CloudClient(session=s)
    # Disable refresh so _ensure_fresh is a no-op
    monkeypatch.setattr(c, "_ensure_fresh", lambda: None)
    return c


@pytest.fixture
def cli(monkeypatch):
    return _logged_in_client(monkeypatch)


# ----- helper: capture the last URL passed to _request --------------------


class _Capturer:
    """Replacement for cloud_client._request that records the URL and
    returns a canned dict. One instance per test."""

    def __init__(self, response=None):
        self.calls: list[dict] = []
        self.response = response or {}

    def __call__(self, method, url, *, body=None, headers=None,
                 timeout=None, return_cookies=False):
        self.calls.append({"method": method, "url": url,
                           "body": body, "headers": headers})
        if callable(self.response):
            return self.response(method, url, body)
        return self.response


def _patch_request(monkeypatch, response=None) -> _Capturer:
    cap = _Capturer(response=response)
    monkeypatch.setattr(cloud_client, "_request", cap)
    return cap


# ----- task / history -----------------------------------------------------


def test_get_user_tasks_uses_correct_endpoint(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"total": 94, "hits": [{"id": 1}]})
    out = cli.get_user_tasks(limit=20)
    assert cap.calls[0]["url"].endswith("/v1/user-service/my/tasks?limit=20")
    assert out == [{"id": 1}]


def test_get_task_by_id(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"task": "details"})
    cli.get_task(961930281)
    assert cap.calls[0]["url"].endswith("/v1/iot-service/api/user/task/961930281")


# ----- messages + inbox ---------------------------------------------------


def test_get_message_count(cli, monkeypatch):
    cap = _patch_request(monkeypatch,
                          {"unreadTotal": 24, "designCount": 21, "deviceCount": 3})
    out = cli.get_message_count()
    assert cap.calls[0]["url"].endswith("/v1/user-service/my/message/count")
    assert out["unreadTotal"] == 24


def test_get_messages_includes_limit(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"hits": []})
    cli.get_messages(limit=10)
    assert "limit=10" in cap.calls[0]["url"]


# ----- support tickets ----------------------------------------------------


def test_get_trouble_tickets(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"total": 1, "hits": []})
    cli.get_trouble_tickets(limit=10)
    assert "/v1/aftersale-service/trouble/list" in cap.calls[0]["url"]
    assert "limit=10" in cap.calls[0]["url"]


def test_get_trouble_unread_count_strict_int(cli, monkeypatch):
    _patch_request(monkeypatch, {"count": 7})
    assert cli.get_trouble_unread_count() == 7


def test_get_makerworld_unread_count(cli, monkeypatch):
    _patch_request(monkeypatch, {"count": 3})
    assert cli.get_makerworld_unread_count() == 3


# ----- MakerWorld surface -------------------------------------------------


def test_get_for_you_includes_seed(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"hits": [], "seed": 42})
    cli.get_for_you(seed=42, limit=10)
    url = cap.calls[0]["url"]
    assert "/v1/design-recommend-service/my/for-you" in url
    assert "seed=42" in url
    assert "limit=10" in url


def test_get_homepage_nav(cli, monkeypatch):
    _patch_request(monkeypatch, {"navs": []})
    cli.get_homepage_nav()


def test_get_search_suggestions_returns_list(cli, monkeypatch):
    _patch_request(monkeypatch, {"sugList": ["bunny", "p1s"]})
    assert cli.get_search_suggestions() == ["bunny", "p1s"]


def test_get_my_profile(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"uid": 1})
    cli.get_my_profile()
    assert cap.calls[0]["url"].endswith("/v1/design-user-service/my/profile")


def test_get_points_progress(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"totalPoint": 0})
    cli.get_points_progress()
    assert cap.calls[0]["url"].endswith("/v1/point-service/point-bill/progress")


# ----- device firmware + status ------------------------------------------


def test_get_device_firmware_versions_returns_devices(cli, monkeypatch):
    _patch_request(monkeypatch,
                    {"devices": [{"dev_id": "X", "version": "01.01.00.65"}]})
    devs = cli.get_device_firmware_versions()
    assert len(devs) == 1
    assert devs[0]["version"] == "01.01.00.65"


def test_get_device_status_cached_uses_force_param(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"devices": []})
    cli.get_device_status_cached()
    assert "force=true" in cap.calls[0]["url"]


# ----- filament inventory + app config ------------------------------------


def test_get_filament_inventory(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"hits": [{"filamentVendor": "Bambu"}]})
    rows = cli.get_filament_inventory()
    assert "/v1/design-user-service/my/filament/v2" in cap.calls[0]["url"]
    assert rows[0]["filamentVendor"] == "Bambu"


def test_get_app_configuration(cli, monkeypatch):
    _patch_request(monkeypatch, {"ratingLowScoreReason": []})
    cli.get_app_configuration()


# ----- favorites + liked + presets ----------------------------------------


def test_get_favorites(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"hits": []})
    cli.get_favorites()
    assert cap.calls[0]["url"].endswith("/v1/design-service/my/favorites/listlite")


def test_get_liked_designs(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"total": 0, "hits": []})
    cli.get_liked_designs()
    assert cap.calls[0]["url"].endswith("/v1/design-service/my/design/like")


def test_get_slicer_presets_passes_version(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"print": {}})
    cli.get_slicer_presets(version="01.10.00.69", public=False)
    url = cap.calls[0]["url"]
    assert "public=false" in url
    assert "version=01.10.00.69" in url


# ----- search + browse ----------------------------------------------------


def test_search_designs_uses_keyword_param_not_query(cli, monkeypatch):
    """Critical regression catch — `/select/design?query=` was the WRONG
    endpoint (ignored input). Real endpoint is `/search/design?keyword=`."""
    cap = _patch_request(monkeypatch, {"hits": []})
    cli.search_designs("calibration cube", limit=5)
    url = cap.calls[0]["url"]
    assert "/v1/search-service/search/design" in url, url
    assert "keyword=calibration%20cube" in url, url
    # The OLD (broken) endpoint must NOT be hit
    assert "/select/design" not in url
    assert "query=" not in url


def test_browse_designs_by_nav(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"hits": []})
    cli.browse_designs_by_nav("Trending", limit=10)
    url = cap.calls[0]["url"]
    assert "/v1/search-service/select/design/nav" in url
    assert "navKey=Trending" in url


# ----- design details + remixes -------------------------------------------


def test_get_design(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"id": 1623016})
    cli.get_design(1623016)
    assert cap.calls[0]["url"].endswith("/v1/design-service/design/1623016")


def test_get_design_remixes(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"total": 0, "hits": []})
    cli.get_design_remixes(1623016)
    assert cap.calls[0]["url"].endswith("/v1/design-service/design/1623016/remixed")


# ----- comments + like ---------------------------------------------------


def test_get_design_comments(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"total": 0, "hits": []})
    cli.get_design_comments(1623016, limit=20)
    url = cap.calls[0]["url"]
    assert "/v1/comment-service/commentandrating" in url
    assert "designId=1623016" in url


def test_toggle_design_like_uses_POST(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {})
    cli.toggle_design_like(1623016)
    assert cap.calls[0]["method"] == "POST"
    assert cap.calls[0]["url"].endswith("/v1/design-service/design/1623016/like")


def test_reply_to_comment_posts_correct_body(cli, monkeypatch):
    """POST to /v1/comment-service/comment/<id>/reply with `content` in body."""
    cap = _patch_request(monkeypatch, {"id": 999, "content": "thanks!"})
    cli.reply_to_comment(987654, "thanks!")
    assert cap.calls[0]["method"] == "POST"
    assert cap.calls[0]["url"].endswith(
        "/v1/comment-service/comment/987654/reply")
    assert cap.calls[0]["body"] == {"content": "thanks!"}


def test_reply_to_comment_rejects_empty_text(cli):
    """Empty / whitespace-only text raises CloudError before the network
    call — we short-circuit so the server's opaque 400 doesn't surface."""
    with pytest.raises(cloud_client.CloudError, match="empty"):
        cli.reply_to_comment(1, "")
    with pytest.raises(cloud_client.CloudError, match="empty"):
        cli.reply_to_comment(1, "   ")


# ----- f3mf download URL --------------------------------------------------


def test_get_instance_download_url(cli, monkeypatch):
    cap = _patch_request(monkeypatch,
                          {"name": "x.3mf", "url": "https://bblmw.com/..."})
    cli.get_instance_download_url(1713546, kind="download")
    url = cap.calls[0]["url"]
    assert "/v1/design-service/instance/1713546/f3mf" in url
    assert "type=download" in url


# ----- login_code_only ----------------------------------------------------


def test_login_code_only_calls_sendemail_then_login(cli, monkeypatch):
    """Email-code flow: 2 POSTs (sendemail/code then login with code) +
    no password field."""
    calls = []

    def fake_send(self, region, email):
        calls.append(("sendemail", region, email))

    def fake_submit(self, region, email, code):
        calls.append(("submit", region, email, code))

    monkeypatch.setattr(cloud_client.CloudClient, "_send_email_code", fake_send)
    monkeypatch.setattr(cloud_client.CloudClient, "_submit_email_code", fake_submit)
    cli.login_code_only("user@example.com", region="us",
                        code_resolver=lambda _e: "123456")
    assert len(calls) == 2
    assert calls[0][0] == "sendemail"
    assert calls[0][2] == "user@example.com"
    assert calls[1][0] == "submit"
    assert calls[1][3] == "123456"


def test_login_code_only_without_resolver_raises(cli):
    """Missing resolver should fail-fast, not silently no-op."""
    with pytest.raises(cloud_client.CloudError, match="code_resolver"):
        cli.login_code_only("user@example.com", region="us", code_resolver=None)


# ----- /f3mf robot wall (GeeTest v4 captcha) ------------------------------


def test_get_instance_download_url_passes_captcha_result(cli, monkeypatch):
    """A solved captcha is forwarded as the X-BBL-Captcha-Result header."""
    cap = _patch_request(monkeypatch, {"name": "m.3mf", "url": "https://x"})
    cli.get_instance_download_url(1116947, kind="preview",
                                  captcha_result="BASE64RESULT")
    call = cap.calls[0]
    assert call["url"].endswith(
        "/v1/design-service/instance/1116947/f3mf?type=preview")
    assert call["headers"]["X-BBL-Captcha-Result"] == "BASE64RESULT"
    assert "Bearer " in call["headers"]["Authorization"]


def test_get_instance_download_url_no_captcha_header_when_absent(cli, monkeypatch):
    cap = _patch_request(monkeypatch, {"name": "m.3mf", "url": "https://x"})
    cli.get_instance_download_url(42)
    assert "X-BBL-Captcha-Result" not in cap.calls[0]["headers"]


def test_get_instance_download_url_418_raises_captcha_required(cli, monkeypatch):
    """A 418 robot wall surfaces as CaptchaRequired carrying the captchaId."""
    def _raise_418(method, url, *, headers=None, body=None,
                   timeout=None, return_cookies=False):
        raise cloud_client.CloudError(
            "HTTP 418", status=418,
            body=json.dumps({"code": 1, "captchaId": "abc123",
                             "error": "not a robot"}))
    monkeypatch.setattr(cloud_client, "_request", _raise_418)
    with pytest.raises(cloud_client.CaptchaRequired) as ei:
        cli.get_instance_download_url(7)
    assert ei.value.captcha_id == "abc123"
    assert ei.value.status == 418


def test_pull_design_3mf_threads_captcha_result(cli, monkeypatch, tmp_path):
    """pull_design_3mf forwards captcha_result down to the instance call."""
    monkeypatch.setattr(cli, "get_design",
                        lambda d: {"instances": [{"id": 555, "isDefault": True}]})
    seen = {}
    monkeypatch.setattr(cli, "get_instance_download_url",
                        lambda inst, captcha_result=None: seen.update(
                            inst=inst, cr=captcha_result)
                        or {"name": "m.3mf", "url": "https://oss/m.3mf"})

    class _Resp:
        def read(self): return b"PK\x03\x04zip"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(cloud_client.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    out = cli.pull_design_3mf(1, tmp_path, captcha_result="CR")
    assert seen == {"inst": 555, "cr": "CR"}
    assert out.read_bytes().startswith(b"PK\x03\x04")
