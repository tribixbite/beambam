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
    explicit = clean_env / "project.env"
    explicit.write_text(
        "X2D_IP=10.0.0.1\nOPENAI_API_KEY=leak\nX2D_CODE=fromexplicit\n", encoding="utf-8")
    monkeypatch.setenv("BEAMBAM_ENV_FILE", str(explicit))
    cfg = clean_env / "home" / ".x2d"
    cfg.mkdir()
    (cfg / ".env").write_text("X2D_CODE=fromcfg\nX2D_SERIAL=SER\n", encoding="utf-8")
    monkeypatch.setenv("X2D_IP", "already-set")

    read = paths.load_env_files()

    assert [p.name for p in read] == ["project.env", ".env"]
    assert os.environ["X2D_IP"] == "already-set"     # real env wins
    assert os.environ["X2D_CODE"] == "fromexplicit"  # first file wins
    assert os.environ["X2D_SERIAL"] == "SER"
    assert os.environ.get("OPENAI_API_KEY") != "leak"   # foreign keys never imported


def test_cwd_env_is_not_read_automatically(clean_env: Path) -> None:
    """A .env in whatever directory beambam runs from must not redirect the
    printer (its access code would go to that host) or swap the slicer binary."""
    (clean_env / "work" / ".env").write_text(
        "X2D_IP=6.6.6.6\nX2D_BS_BIN=C:/Windows/System32/notepad.exe\n", encoding="utf-8")
    read = paths.load_env_files()
    assert read == []
    assert "X2D_IP" not in os.environ and "X2D_BS_BIN" not in os.environ


def test_beambam_home_from_env_file_moves_config_env(clean_env: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    other = clean_env / "elsewhere"
    other.mkdir()
    (other / ".env").write_text("X2D_SERIAL=from-elsewhere\n", encoding="utf-8")
    explicit = clean_env / "project.env"
    explicit.write_text(f"BEAMBAM_HOME={other}\n", encoding="utf-8")
    monkeypatch.setenv("BEAMBAM_ENV_FILE", str(explicit))

    paths.load_env_files()

    assert os.environ["X2D_SERIAL"] == "from-elsewhere"


def test_creds_resolve_reads_env_file_values(clean_env: Path) -> None:
    cfg = clean_env / "home" / ".x2d"
    cfg.mkdir()
    (cfg / ".env").write_text(
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


def test_named_section_beats_env_vars(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """X2D_IP for printer A must not leak into an explicit --printer B."""
    ini = clean_env / "creds.ini"
    ini.write_text("[printer:a]\nip = 10.0.0.1\ncode = aaaa\nserial = SA\n"
                   "[printer:b]\nip = 10.0.0.2\ncode = bbbb\nserial = SB\n", encoding="utf-8")
    monkeypatch.setenv("X2D_CREDS", str(ini))
    monkeypatch.setenv("X2D_IP", "10.0.0.1")
    b = Creds.resolve(argparse.Namespace(ip=None, code=None, serial=None, printer="b"))
    assert (b.ip, b.code, b.serial) == ("10.0.0.2", "bbbb", "SB")
    # Explicit flags still win over the named section.
    b2 = Creds.resolve(argparse.Namespace(ip="10.0.0.9", code=None, serial=None, printer="b"))
    assert b2.ip == "10.0.0.9" and b2.code == "bbbb"


def test_signing_path_prefers_per_serial_then_global(clean_env: Path) -> None:
    cfg = clean_env / "home" / ".x2d"
    cfg.mkdir(parents=True)
    # No files yet -> resolves to the global path (back-compat).
    assert paths.signing_path("printer_sign_key.pem", "SN-A") == cfg / "printer_sign_key.pem"
    # A global file exists but no per-serial one -> global.
    (cfg / "printer_sign_key.pem").write_text("global")
    assert paths.signing_path("printer_sign_key.pem", "SN-A") == cfg / "printer_sign_key.pem"
    # A per-serial file exists -> it wins.
    (cfg / "SN-A").mkdir()
    (cfg / "SN-A" / "printer_sign_key.pem").write_text("per-serial")
    assert paths.signing_path("printer_sign_key.pem", "SN-A") == cfg / "SN-A" / "printer_sign_key.pem"
    # A different serial with no per-serial file falls back to global.
    assert paths.signing_path("printer_sign_key.pem", "SN-B") == cfg / "printer_sign_key.pem"
    # No serial -> global.
    assert paths.signing_path("printer_sign_key.pem", None) == cfg / "printer_sign_key.pem"


def test_signing_write_path_and_serial_dir(clean_env: Path) -> None:
    cfg = clean_env / "home" / ".x2d"
    assert paths.signing_write_path("printer_cert_id.txt", "SN-A") == cfg / "SN-A" / "printer_cert_id.txt"
    assert paths.signing_write_path("printer_cert_id.txt", "") == cfg / "printer_cert_id.txt"
    # Serials are sanitised for use as a directory name.
    assert paths.serial_dir("00M/09:A") == cfg / "00M_09_A"
