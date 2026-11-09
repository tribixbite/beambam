"""beambam.paths: config-dir resolution and .env loading."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from beambam import paths
from beambam.config import Creds


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No beambam vars in the environment, HOME and CWD inside tmp_path."""
    for key in list(os.environ):
        if key.startswith(paths.ENV_PREFIXES):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path


def test_config_dir_default_and_overrides(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert paths.config_dir() == clean_env / "home" / ".x2d"
    monkeypatch.setenv("X2D_HOME", str(clean_env / "legacy"))
    assert paths.config_dir() == clean_env / "legacy"
    monkeypatch.setenv("BEAMBAM_HOME", str(clean_env / "canon"))
    assert paths.config_dir() == clean_env / "canon"          # canonical beats legacy
    assert paths.credentials_path() == clean_env / "canon" / "credentials"
    monkeypatch.setenv("X2D_CREDS", str(clean_env / "c.ini"))
    assert paths.credentials_path() == clean_env / "c.ini"


def test_parse_env_text_forms() -> None:
    text = (
        "# comment\n"
        "\n"
        "X2D_IP=192.168.1.42\n"
        "export X2D_CODE = '12345678'\n"
        'X2D_SERIAL="03AB\\"C"\n'
        "BEAMBAM_HOME=/tmp/x # trailing note\n"
        "X2D_HASH=a#b\n"
        "not a line\n"
        "=novalue\n"
    )
    assert paths.parse_env_text(text) == {
        "X2D_IP": "192.168.1.42",
        "X2D_CODE": "12345678",
        "X2D_SERIAL": '03AB"C',
        "BEAMBAM_HOME": "/tmp/x",
        "X2D_HASH": "a#b",
    }


def test_load_env_files_prefix_filter_and_precedence(clean_env: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    (clean_env / "work" / ".env").write_text(
        "X2D_IP=10.0.0.1\nOPENAI_API_KEY=leak\nX2D_CODE=fromcwd\n", encoding="utf-8")
    cfg = clean_env / "home" / ".x2d"
    cfg.mkdir()
    (cfg / ".env").write_text("X2D_CODE=fromcfg\nX2D_SERIAL=SER\n", encoding="utf-8")
    monkeypatch.setenv("X2D_IP", "already-set")

    read = paths.load_env_files()

    assert [p.name for p in read] == [".env", ".env"]
    assert os.environ["X2D_IP"] == "already-set"     # real env wins
    assert os.environ["X2D_CODE"] == "fromcwd"       # first file wins
    assert os.environ["X2D_SERIAL"] == "SER"
    assert os.environ.get("OPENAI_API_KEY") != "leak"   # foreign keys never imported


def test_beambam_home_from_cwd_env_moves_config_env(clean_env: Path) -> None:
    other = clean_env / "elsewhere"
    other.mkdir()
    (other / ".env").write_text("X2D_SERIAL=from-elsewhere\n", encoding="utf-8")
    (clean_env / "work" / ".env").write_text(f"BEAMBAM_HOME={other}\n", encoding="utf-8")

    paths.load_env_files()

    assert os.environ["X2D_SERIAL"] == "from-elsewhere"


def test_creds_resolve_reads_env_file_values(clean_env: Path) -> None:
    (clean_env / "work" / ".env").write_text(
        "X2D_IP=192.168.9.9\nX2D_CODE=87654321\nX2D_SERIAL=0309XX\n", encoding="utf-8")
    paths.load_env_files()
    creds = Creds.resolve(argparse.Namespace(ip=None, code=None, serial=None, printer=None))
    assert (creds.ip, creds.code, creds.serial) == ("192.168.9.9", "87654321", "0309XX")


def test_creds_and_configcli_share_x2d_creds(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ini = clean_env / "custom.ini"
    ini.write_text("[printer]\nip = 1.2.3.4\ncode = c\nserial = s\n", encoding="utf-8")
    monkeypatch.setenv("X2D_CREDS", str(ini))
    assert Creds.list_names() == [""]
    assert Creds.resolve_default().ip == "1.2.3.4"
