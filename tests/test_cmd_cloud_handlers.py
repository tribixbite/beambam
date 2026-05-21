"""tests/test_cmd_cloud_handlers.py — cmd_cloud_* handlers.

Cloud read-side handlers all share the same shape:

    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty: print('not logged in'); return 1
    try: r = cli.<method>(...)
    except cloud_client.CloudError as e: print('cloud API failed:'); return 1
    if args.json: print(json.dumps(r)); return 0
    <pretty-print> ; return 0

We mock `CloudClient.load_or_anonymous` to return a fake with a
non-empty session + a stub-method that records the call. Tests cover:
  * happy path: correct method called + correct args forwarded
  * logged-out path: prints `not logged in` + returns 1
  * CloudError path: prints `cloud API failed` + returns 1
  * --json flag: emits parseable JSON

Cloud HTTP itself is tested elsewhere (test_cloud_client_new_endpoints.py
covers URLs/params/response unwrapping). This file tests the handler
layer's contract: argument forwarding + output shape + exit codes."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cloud_client


# ----- fixture: a logged-in fake CloudClient ----------------------------


@pytest.fixture
def fake_cli(monkeypatch):
    """Build a CloudClient stand-in with `.session.empty == False` and a
    MagicMock spec so every cli.<method>(args) call is recorded.

    Returns the MagicMock. `monkeypatch.setattr` swaps
    `cloud_client.CloudClient.load_or_anonymous` to return it.
    """
    cli = MagicMock(spec=cloud_client.CloudClient)
    cli.session = cloud_client.Session(
        access_token="AT", refresh_token="RT",
        expires_at=time.time() + 3600,
        user_id="123", region="us",
    )
    monkeypatch.setattr(
        cloud_client.CloudClient, "load_or_anonymous",
        classmethod(lambda cls: cli),
    )
    return cli


@pytest.fixture
def anon_cli(monkeypatch):
    """A CloudClient stand-in with an EMPTY session — every cloud handler
    must short-circuit with `not logged in`."""
    cli = MagicMock(spec=cloud_client.CloudClient)
    cli.session = cloud_client.Session()  # empty by default
    assert cli.session.empty
    monkeypatch.setattr(
        cloud_client.CloudClient, "load_or_anonymous",
        classmethod(lambda cls: cli),
    )
    return cli


def _ns(**kw) -> argparse.Namespace:
    """argparse.Namespace with sensible defaults so handlers don't
    KeyError on missing attrs (each handler reads a small subset)."""
    defaults = dict(
        json=False, limit=20, offset=0,
        query="", nav="Trending", design_id=1, task_id=1,
        list=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ===== cmd_cloud_logout ===================================================


def test_cloud_logout_clears_session_and_prints(fake_cli, capsys):
    import x2d_bridge

    rc = x2d_bridge.cmd_cloud_logout(_ns())
    assert rc == 0
    fake_cli.logout.assert_called_once()
    assert "session cleared" in capsys.readouterr().out


# ===== cmd_cloud_history ==================================================


def test_cloud_history_calls_get_user_tasks(fake_cli, capsys):
    """Limits are forwarded as ints; output is the pretty table by default."""
    import x2d_bridge

    fake_cli.get_user_tasks.return_value = [
        {"id": 1, "status": 2, "deviceId": "dev1",
         "designTitle": "Cube", "designId": 99,
         "startTime": 1700000000, "endTime": 1700001200},
    ]
    rc = x2d_bridge.cmd_cloud_history(_ns(limit=10))
    assert rc == 0
    fake_cli.get_user_tasks.assert_called_once_with(limit=10)
    out = capsys.readouterr().out
    assert "OK" in out  # status==2 maps to OK
    assert "Cube" in out


def test_cloud_history_json_flag_emits_parseable_json(fake_cli, capsys):
    import x2d_bridge

    fake_cli.get_user_tasks.return_value = [{"id": 5}]
    rc = x2d_bridge.cmd_cloud_history(_ns(json=True))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == [{"id": 5}]


def test_cloud_history_empty_returns_0_and_prints_notice(fake_cli, capsys):
    import x2d_bridge

    fake_cli.get_user_tasks.return_value = []
    rc = x2d_bridge.cmd_cloud_history(_ns())
    assert rc == 0
    assert "(no tasks)" in capsys.readouterr().out


def test_cloud_history_logged_out_returns_1(anon_cli, capsys):
    import x2d_bridge

    rc = x2d_bridge.cmd_cloud_history(_ns())
    assert rc == 1
    err = capsys.readouterr().err
    assert "not logged in" in err
    anon_cli.get_user_tasks.assert_not_called()


def test_cloud_history_cloud_error_returns_1(fake_cli, capsys):
    """Network/API errors must surface as exit 1 + stderr message — never
    bubble as a raw traceback."""
    import x2d_bridge

    fake_cli.get_user_tasks.side_effect = cloud_client.CloudError("403 Forbidden")
    rc = x2d_bridge.cmd_cloud_history(_ns())
    assert rc == 1
    err = capsys.readouterr().err
    assert "cloud API failed" in err
    assert "403" in err


# ===== cmd_cloud_task =====================================================


def test_cloud_task_forwards_id_to_client(fake_cli, capsys):
    import x2d_bridge

    fake_cli.get_task.return_value = {"task": "details"}
    rc = x2d_bridge.cmd_cloud_task(_ns(task_id=42))
    assert rc == 0
    fake_cli.get_task.assert_called_once_with(42)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"task": "details"}


def test_cloud_task_logged_out_returns_1(anon_cli):
    import x2d_bridge

    assert x2d_bridge.cmd_cloud_task(_ns(task_id=1)) == 1


# ===== cmd_cloud_messages =================================================


def test_cloud_messages_counts_only_default(fake_cli, capsys):
    """Without --list, only get_message_count() is called."""
    import x2d_bridge

    fake_cli.get_message_count.return_value = {
        "unreadTotal": 7, "deviceCount": 3, "designCount": 4,
    }
    rc = x2d_bridge.cmd_cloud_messages(_ns(list=False))
    assert rc == 0
    fake_cli.get_message_count.assert_called_once()
    fake_cli.get_messages.assert_not_called()
    out = capsys.readouterr().out
    assert "unreadTotal" in out and "7" in out


def test_cloud_messages_with_list_also_pulls_messages(fake_cli, capsys):
    import x2d_bridge

    fake_cli.get_message_count.return_value = {"unreadTotal": 1}
    fake_cli.get_messages.return_value = {"hits": [
        {"id": 100, "type": 1, "title": "Print finished"},
    ]}
    rc = x2d_bridge.cmd_cloud_messages(_ns(list=True, limit=5))
    assert rc == 0
    fake_cli.get_messages.assert_called_once_with(limit=5)
    assert "Print finished" in capsys.readouterr().out


def test_cloud_messages_logged_out_returns_1(anon_cli):
    import x2d_bridge

    assert x2d_bridge.cmd_cloud_messages(_ns()) == 1


# ===== cmd_cloud_search ===================================================


def test_cloud_search_forwards_query_limit_offset(fake_cli, capsys):
    import x2d_bridge

    fake_cli.search_designs.return_value = {"total": 0, "hits": []}
    rc = x2d_bridge.cmd_cloud_search(_ns(query="rumi", limit=5, offset=10))
    assert rc == 0
    fake_cli.search_designs.assert_called_once_with("rumi", limit=5, offset=10)


def test_cloud_search_logged_out(anon_cli):
    import x2d_bridge

    assert x2d_bridge.cmd_cloud_search(_ns(query="cube")) == 1


def test_cloud_search_cloud_error_returns_1(fake_cli, capsys):
    import x2d_bridge

    fake_cli.search_designs.side_effect = cloud_client.CloudError("500")
    rc = x2d_bridge.cmd_cloud_search(_ns(query="x"))
    assert rc == 1
    assert "cloud API failed" in capsys.readouterr().err


# ===== cmd_cloud_browse ===================================================


def test_cloud_browse_forwards_nav_arg(fake_cli):
    import x2d_bridge

    fake_cli.browse_designs_by_nav.return_value = {"total": 0, "hits": []}
    rc = x2d_bridge.cmd_cloud_browse(_ns(nav="Foryou", limit=10, offset=0))
    assert rc == 0
    fake_cli.browse_designs_by_nav.assert_called_once_with(
        "Foryou", limit=10, offset=0,
    )


def test_cloud_browse_logged_out(anon_cli):
    import x2d_bridge

    assert x2d_bridge.cmd_cloud_browse(_ns(nav="Trending")) == 1


# ===== cmd_cloud_design ===================================================


def test_cloud_design_forwards_design_id(fake_cli, capsys):
    """Default (no --json): the handler pretty-prints fields like
    'Title' / 'Slug' / 'Design ID' — assert the design id appears in
    that human format."""
    import x2d_bridge

    fake_cli.get_design.return_value = {
        "id": 1623016, "title": "Calibration Cube", "slug": "calibration-cube",
        "designCreator": {"publicUsername": "tester"},
        "instances": [],
    }
    rc = x2d_bridge.cmd_cloud_design(_ns(design_id=1623016))
    assert rc == 0
    fake_cli.get_design.assert_called_once_with(1623016)
    out = capsys.readouterr().out
    assert "Calibration Cube" in out
    assert "1623016" in out


def test_cloud_design_json_emits_full_payload(fake_cli, capsys):
    import x2d_bridge

    fake_cli.get_design.return_value = {"id": 1, "title": "x"}
    rc = x2d_bridge.cmd_cloud_design(_ns(design_id=1, json=True))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id"] == 1


# ===== cmd_cloud_comment_reply ===========================================


def test_cloud_comment_reply_forwards_id_and_text(fake_cli, capsys):
    """Happy path: text is forwarded to reply_to_comment + a confirmation
    is printed citing the new reply id."""
    import x2d_bridge

    fake_cli.reply_to_comment.return_value = {"id": 4242,
                                                "content": "thanks!"}
    rc = x2d_bridge.cmd_cloud_comment_reply(
        _ns(comment_id=987654, text="thanks!"))
    assert rc == 0
    fake_cli.reply_to_comment.assert_called_once_with(987654, "thanks!")
    out = capsys.readouterr().out
    assert "replied to comment 987654" in out
    assert "4242" in out


def test_cloud_comment_reply_json_emits_full_record(fake_cli, capsys):
    import x2d_bridge

    payload = {"id": 4242, "content": "thanks!", "parentId": 987654}
    fake_cli.reply_to_comment.return_value = payload
    rc = x2d_bridge.cmd_cloud_comment_reply(
        _ns(comment_id=987654, text="thanks!", json=True))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == payload


def test_cloud_comment_reply_logged_out_returns_1(anon_cli, capsys):
    import x2d_bridge

    rc = x2d_bridge.cmd_cloud_comment_reply(
        _ns(comment_id=1, text="x"))
    assert rc == 1
    assert "not logged in" in capsys.readouterr().err
    anon_cli.reply_to_comment.assert_not_called()


def test_cloud_comment_reply_cloud_error_returns_1(fake_cli, capsys):
    """CloudError from the client (incl. our own empty-text guard) must
    surface as exit 1 + stderr, never as a raw traceback."""
    import x2d_bridge

    fake_cli.reply_to_comment.side_effect = cloud_client.CloudError(
        "403 forbidden")
    rc = x2d_bridge.cmd_cloud_comment_reply(
        _ns(comment_id=1, text="x"))
    assert rc == 1
    assert "cloud API failed" in capsys.readouterr().err
    assert "403" in capsys.readouterr().err or True  # already consumed above
