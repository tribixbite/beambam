"""tests/test_upgrade.py — `beambam upgrade` self-upgrade subcommand.

Validates the pip self-upgrade flow without ever hitting PyPI or
subprocess. Tests:

  * latest_version() PyPI JSON parsing (stable vs pre-release filter)
  * compare() returns the right status string per case
  * cmd_upgrade --check: prints, never invokes pip
  * cmd_upgrade upgrade-available path → invokes pip with correct argv
  * cmd_upgrade up-to-date / dev-ahead / source-checkout: no pip call
  * is_uvx_environment() detects the uv-tools venv layout
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from beambam import upgrade


# ----- _version_key + _is_prerelease + compare ---------------------------


def test_version_key_orders_numerically():
    """1.10 must sort higher than 1.9 (string sort would get this wrong)."""
    assert upgrade._version_key("1.10.0") > upgrade._version_key("1.9.5")
    assert upgrade._version_key("2.0.0") > upgrade._version_key("1.99.99")


def test_is_prerelease_catches_common_markers():
    assert upgrade._is_prerelease("1.3.0rc1")
    assert upgrade._is_prerelease("1.3.0a2")
    assert upgrade._is_prerelease("1.3.0b1")
    assert upgrade._is_prerelease("1.3.0.dev3")
    assert not upgrade._is_prerelease("1.3.0")
    assert not upgrade._is_prerelease("2.0.5")


def test_compare_status_strings():
    assert upgrade.compare("1.3.0", "1.3.0") == "up-to-date"
    assert upgrade.compare("1.2.0", "1.3.0") == "upgrade-available"
    assert upgrade.compare("1.4.0", "1.3.0") == "dev-ahead"
    assert upgrade.compare(None, "1.3.0") == "source-checkout"


# ----- latest_version() PyPI parsing -------------------------------------


def _mock_pypi_response(monkeypatch, payload: dict):
    """Stub urllib.request.urlopen to return a fake response carrying
    `payload` (which will be JSON-encoded)."""
    raw = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __init__(self): self._body = raw
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    monkeypatch.setattr(
        upgrade.urllib.request, "urlopen",
        lambda req, timeout=None, context=None: _Resp(),
    )


def test_latest_version_picks_highest_stable(monkeypatch):
    """Pre-releases must NOT be picked when include_pre=False."""
    _mock_pypi_response(monkeypatch, {
        "info": {"version": "1.4.0rc1"},  # latest including pre
        "releases": {
            "1.2.0": [{"x": 1}],
            "1.3.0": [{"x": 1}],
            "1.4.0rc1": [{"x": 1}],   # pre-release
            "1.5.0a1": [{"x": 1}],    # pre-release
        },
    })
    v = upgrade.latest_version(include_pre=False)
    assert v == "1.3.0"


def test_latest_version_with_include_pre_uses_info_version(monkeypatch):
    _mock_pypi_response(monkeypatch, {
        "info": {"version": "1.4.0rc1"},
        "releases": {"1.3.0": [{}], "1.4.0rc1": [{}]},
    })
    v = upgrade.latest_version(include_pre=True)
    assert v == "1.4.0rc1"


def test_latest_version_filters_yanked_releases(monkeypatch):
    """A yanked release is represented as `releases[v] = []` (empty list
    of files). We must skip it — picking a yanked version would tell
    users to install something PyPI has actively pulled."""
    _mock_pypi_response(monkeypatch, {
        "info": {"version": "1.5.0"},
        "releases": {
            "1.2.0": [{"x": 1}],
            "1.3.0": [],            # yanked
            "1.4.0": [{"x": 1}],
            "1.5.0": [],            # yanked
        },
    })
    v = upgrade.latest_version(include_pre=False)
    assert v == "1.4.0"


def test_latest_version_network_failure_raises(monkeypatch):
    def _raise(req, timeout=None, context=None):
        raise urllib.error.URLError("DNS failed")

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", _raise)
    with pytest.raises(RuntimeError, match="PyPI query failed"):
        upgrade.latest_version()


def test_latest_version_non_json_raises(monkeypatch):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>maintenance</html>"

    monkeypatch.setattr(
        upgrade.urllib.request, "urlopen",
        lambda req, timeout=None, context=None: _Resp(),
    )
    with pytest.raises(RuntimeError, match="non-JSON"):
        upgrade.latest_version()


# ----- is_uvx_environment ------------------------------------------------


def test_is_uvx_detects_uv_tools_venv(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV",
                        "/home/user/.local/share/uv/tools/beambam")
    assert upgrade.is_uvx_environment() is True


def test_is_uvx_detects_uv_tool_alt_path(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/var/lib/uv-tool/beambam-venv")
    assert upgrade.is_uvx_environment() is True


def test_is_uvx_false_for_regular_venv(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/home/user/venvs/myproject")
    assert upgrade.is_uvx_environment() is False


def test_is_uvx_false_when_no_virtual_env(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert upgrade.is_uvx_environment() is False


# ----- cmd_upgrade behaviour --------------------------------------------


def _args(**kw) -> argparse.Namespace:
    defaults = {"check": False, "pre": False}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_cmd_upgrade_check_does_not_invoke_pip(monkeypatch, capsys):
    """--check is dry-run by contract: pip is never invoked."""
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.2.0")
    monkeypatch.setattr(upgrade, "latest_version",
                         lambda include_pre=False: "1.3.0")
    pip_called = []
    monkeypatch.setattr(upgrade, "run_pip_upgrade",
                         lambda **kw: pip_called.append(kw) or 0)

    rc = upgrade.cmd_upgrade(_args(check=True))
    assert rc == 0
    assert pip_called == []
    out = capsys.readouterr().out
    assert "installed: 1.2.0" in out
    assert "latest:    1.3.0" in out
    assert "upgrade available" in out


def test_cmd_upgrade_up_to_date_no_pip(monkeypatch, capsys):
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.3.0")
    monkeypatch.setattr(upgrade, "latest_version",
                         lambda include_pre=False: "1.3.0")
    pip_called = []
    monkeypatch.setattr(upgrade, "run_pip_upgrade",
                         lambda **kw: pip_called.append(kw) or 0)

    rc = upgrade.cmd_upgrade(_args())
    assert rc == 0
    assert pip_called == []
    assert "up to date" in capsys.readouterr().out


def test_cmd_upgrade_dev_ahead_no_pip(monkeypatch, capsys):
    """Local installed version > PyPI latest → don't downgrade."""
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.4.0")
    monkeypatch.setattr(upgrade, "latest_version",
                         lambda include_pre=False: "1.3.0")
    pip_called = []
    monkeypatch.setattr(upgrade, "run_pip_upgrade",
                         lambda **kw: pip_called.append(kw) or 0)

    rc = upgrade.cmd_upgrade(_args())
    assert rc == 0
    assert pip_called == []
    assert "AHEAD" in capsys.readouterr().out


def test_cmd_upgrade_source_checkout_no_pip(monkeypatch, capsys):
    """No installed package → don't `pip install` over the source tree."""
    monkeypatch.setattr(upgrade, "installed_version", lambda: None)
    monkeypatch.setattr(upgrade, "latest_version",
                         lambda include_pre=False: "1.3.0")
    pip_called = []
    monkeypatch.setattr(upgrade, "run_pip_upgrade",
                         lambda **kw: pip_called.append(kw) or 0)

    rc = upgrade.cmd_upgrade(_args())
    assert rc == 0
    assert pip_called == []
    out = capsys.readouterr().out
    assert "source checkout" in out
    assert "pip install beambam==1.3.0" in out


def test_cmd_upgrade_invokes_pip_with_correct_argv(monkeypatch, capsys):
    """Upgrade-available + no --check → pip install -U beambam."""
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.2.0")
    monkeypatch.setattr(upgrade, "latest_version",
                         lambda include_pre=False: "1.3.0")
    captured = {}

    def _stub_pip(*, pre=False, python=None):
        captured["pre"] = pre
        captured["python"] = python
        return 0

    monkeypatch.setattr(upgrade, "run_pip_upgrade", _stub_pip)

    rc = upgrade.cmd_upgrade(_args())
    assert rc == 0
    assert captured == {"pre": False, "python": None}


def test_cmd_upgrade_forwards_pre_flag(monkeypatch, capsys):
    """--pre must reach run_pip_upgrade so `pip install --pre` runs."""
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.2.0")
    monkeypatch.setattr(upgrade, "latest_version",
                         lambda include_pre=False: "1.3.0rc1"
                         if include_pre else "1.3.0")
    captured = {}

    def _stub_pip(*, pre=False, python=None):
        captured["pre"] = pre
        return 0

    monkeypatch.setattr(upgrade, "run_pip_upgrade", _stub_pip)

    rc = upgrade.cmd_upgrade(_args(pre=True))
    assert rc == 0
    assert captured["pre"] is True


def test_cmd_upgrade_pypi_failure_returns_1(monkeypatch, capsys):
    """If PyPI is unreachable, exit 1 with a clean error message —
    never crash with a raw urllib traceback."""
    def _boom(include_pre=False):
        raise RuntimeError("PyPI query failed: DNS failed")
    monkeypatch.setattr(upgrade, "latest_version", _boom)
    monkeypatch.setattr(upgrade, "installed_version", lambda: "1.3.0")

    rc = upgrade.cmd_upgrade(_args())
    assert rc == 1
    err = capsys.readouterr().err
    assert "PyPI query failed" in err


# ----- subparser wiring smoke -------------------------------------------


def test_add_subparser_wires_up_correctly():
    """`beambam upgrade --check` must parse cleanly with the right
    defaults and resolve to cmd_upgrade."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    upgrade.add_subparser(sub)
    ns = p.parse_args(["upgrade", "--check"])
    assert ns.check is True
    assert ns.pre is False
    assert ns.fn is upgrade.cmd_upgrade


def test_add_subparser_pre_flag():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    upgrade.add_subparser(sub)
    ns = p.parse_args(["upgrade", "--pre"])
    assert ns.pre is True
