"""Default CORS origins for the daemon's HTTP API.

The daemon binds loopback only and enforces the DNS-rebinding Host allow-list
separately, so the project's own hosted front-ends are allowed by default --
otherwise every user has to pass ``--cors-origin https://beambam.boo`` before
beambam.boo can drive their daemon. See ``resolve_cors_origins``.
"""
from __future__ import annotations

import argparse

import pytest

from beambam.cli.daemon import DEFAULT_CORS_ORIGINS, resolve_cors_origins


def _args(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {"cors_origin": None, "no_default_cors": False}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEAMBAM_CORS_ORIGINS", raising=False)


def test_first_party_sites_allowed_by_default() -> None:
    assert resolve_cors_origins(_args()) == list(DEFAULT_CORS_ORIGINS)
    assert "https://beambam.boo" in resolve_cors_origins(_args())
    assert "https://pokefit.party" in resolve_cors_origins(_args())


def test_no_default_cors_drops_the_first_party_sites() -> None:
    assert resolve_cors_origins(_args(no_default_cors=True)) == []
    assert resolve_cors_origins(
        _args(no_default_cors=True, cors_origin=["http://localhost:5173"])
    ) == ["http://localhost:5173"]


def test_cli_and_env_extend_the_defaults() -> None:
    got = resolve_cors_origins(_args(cors_origin=["http://localhost:5173"]))
    assert got[:2] == list(DEFAULT_CORS_ORIGINS)
    assert "http://localhost:5173" in got


def test_env_origins_are_added(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEAMBAM_CORS_ORIGINS", "https://a.example, https://b.example")
    got = resolve_cors_origins(_args())
    assert "https://a.example" in got and "https://b.example" in got


def test_duplicates_dropped_keeping_first_order() -> None:
    # A user re-listing a default (with a trailing slash) must not duplicate it.
    got = resolve_cors_origins(_args(cors_origin=["https://beambam.boo/"]))
    assert got.count("https://beambam.boo") == 1
    assert "https://beambam.boo/" not in got
