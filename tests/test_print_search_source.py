"""tests/test_print_search_source.py — `print-search --source` branching.

Exercises the new `--source {makerworld,printables}` flag without
hitting either external API. We mock the GraphQL POST + the MW
CloudClient and assert each backend is invoked for the correct
--source value.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _ns(**kw) -> argparse.Namespace:
    """argparse.Namespace mirroring `print-search`'s args."""
    defaults = dict(
        source="makerworld", query="cube", limit=5, offset=0,
        pick=1, dry_run_pick=True,
        scale=1.0, scale_pct=None, mm=None, copies=1, color=None,
        slot=0, no_ams=False, dry_run=False,
        printer=None, ip=None, code=None, serial=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ----- source=printables routes to GraphQL backend -----------------------


def _fake_printables_response(items: list) -> bytes:
    return json.dumps(
        {"data": {"searchPrints2": {"items": items}}}).encode("utf-8")


def test_print_search_source_printables_hits_graphql(monkeypatch, capsys):
    import x2d_bridge

    captured_urls: list[str] = []

    class _FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    def _stub(req, timeout=None):
        captured_urls.append(req.full_url)
        return _FakeResp(_fake_printables_response([
            {"id": 12345, "name": "Pokeball", "slug": "pokeball",
             "likesCount": 999, "downloadCount": 5000,
             "user": {"publicUsername": "trainer"}},
        ]))

    monkeypatch.setattr(__import__("urllib.request").request, "urlopen", _stub)

    rc = x2d_bridge.cmd_print_search(
        _ns(source="printables", query="pokeball", pick=1,
            dry_run_pick=True))
    assert rc == 0
    assert any("api.printables.com" in u for u in captured_urls)
    out = capsys.readouterr().out
    assert "Pokeball" in out
    assert "printables.com/model/12345-pokeball" in out


def test_print_search_source_printables_no_results_returns_1(monkeypatch,
                                                              capsys):
    import x2d_bridge

    class _FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    monkeypatch.setattr(
        __import__("urllib.request").request, "urlopen",
        lambda req, timeout=None: _FakeResp(_fake_printables_response([])))

    rc = x2d_bridge.cmd_print_search(_ns(source="printables", query="xyz"))
    assert rc == 1
    assert "no Printables results" in capsys.readouterr().out


def test_print_search_source_printables_out_of_range_pick(monkeypatch,
                                                           capsys):
    import x2d_bridge

    class _FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    monkeypatch.setattr(
        __import__("urllib.request").request, "urlopen",
        lambda req, timeout=None: _FakeResp(_fake_printables_response([
            {"id": 1, "name": "x", "slug": "x", "likesCount": 0,
             "downloadCount": 0, "user": {"publicUsername": "u"}},
        ])))

    rc = x2d_bridge.cmd_print_search(
        _ns(source="printables", pick=99, dry_run_pick=True))
    assert rc == 2
    assert "out of range" in capsys.readouterr().out


# ----- source=makerworld stays on the old path --------------------------


def test_print_search_source_makerworld_uses_cloud_client(monkeypatch,
                                                           capsys):
    """Default --source makerworld must call CloudClient.search_designs,
    NOT urlopen against printables."""
    import x2d_bridge
    import cloud_client

    cli_session = cloud_client.Session(
        access_token="AT", refresh_token="RT", expires_at=9e9,
        user_id="1", region="us")

    class _FakeCli:
        def __init__(self): self.session = cli_session
        def search_designs(self, q, limit=10, offset=0):
            return {"total": 1, "hits": [{
                "id": 1623016, "title": "Calibration Cube",
                "designCreator": {"name": "tester"},
                "likeCount": 100, "downloadCount": 500,
            }]}

    monkeypatch.setattr(cloud_client.CloudClient, "load_or_anonymous",
                         classmethod(lambda cls: _FakeCli()))
    # Ensure urlopen would be a hard failure if called.
    monkeypatch.setattr(
        __import__("urllib.request").request, "urlopen",
        lambda *a, **kw: pytest.fail("makerworld path leaked into urlopen"))

    rc = x2d_bridge.cmd_print_search(
        _ns(source="makerworld", query="cube", pick=1, dry_run_pick=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Calibration Cube" in out
    assert "1623016" in out


# ----- subparser wiring smoke -------------------------------------------


def test_print_search_subparser_accepts_source_choices():
    """argparse must accept both choices + reject others."""
    import subprocess

    bridge = Path(__file__).resolve().parents[1] / "x2d_bridge.py"
    for src in ("makerworld", "printables"):
        r = subprocess.run(
            [sys.executable, str(bridge), "print-search", "--source", src,
             "x", "--help"],
            capture_output=True, text=True, timeout=15)
        assert r.returncode == 0, (src, r.stderr)

    r = subprocess.run(
        [sys.executable, str(bridge), "print-search",
         "--source", "thingiverse", "x", "--help"],
        capture_output=True, text=True, timeout=15)
    # argparse rejects invalid choices with exit 2.
    assert r.returncode == 2
    assert "invalid choice" in r.stderr or "invalid choice" in r.stdout
