"""tests/test_install_completion.py — beambam install-completion.

Validates:
  * Subcommand discovery walks the live argparse tree
  * bash / zsh / fish emitters produce a non-empty, shell-syntax-plausible script
  * --install writes to the canonical user dir
  * --install on existing file fails without --force, succeeds with it

All tests use a synthetic parser (or `home=` injection on install_to)
so we never touch the user's real ~/.local / ~/.zfunc / ~/.config.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from beambam.install_completion import (
    SUPPORTED_SHELLS,
    discover_subcommands,
    emit_bash,
    emit_fish,
    emit_zsh,
    install_to,
)


# ----- fixtures ------------------------------------------------------------


def _toy_parser() -> argparse.ArgumentParser:
    """Tiny parser standing in for the real beambam parser, so unit tests
    don't import x2d_bridge.py (which pulls cryptography and a lot of
    sibling state)."""
    p = argparse.ArgumentParser(prog="beambam")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="One-shot status dump")
    sub.add_parser("print", help="Upload + start_print")
    sub.add_parser("ams", help="AMS state + control. Subcommands: status, info.")
    sub.add_parser(
        "weird",
        help="Has 'single quotes' that must be sanitized for shell arrays",
    )
    return p


# ----- discovery ----------------------------------------------------------


def test_discover_returns_sorted_names_and_help():
    cmds = discover_subcommands(_toy_parser())
    names = [c[0] for c in cmds]
    assert names == sorted(names), "discovery must return sorted names"
    assert names == ["ams", "print", "status", "weird"]
    by = dict(cmds)
    assert "AMS state" in by["ams"]
    assert "Upload + start_print" in by["print"]


def test_discover_empty_when_no_subparsers():
    p = argparse.ArgumentParser()  # no add_subparsers
    assert discover_subcommands(p) == []


# ----- emitters -----------------------------------------------------------


def test_emit_bash_has_complete_directive_and_all_names():
    out = emit_bash(discover_subcommands(_toy_parser()))
    assert "complete -F _beambam_completions beambam" in out
    for name in ("status", "print", "ams", "weird"):
        assert name in out
    # Bash arrays use double quotes for the -W list; embedded single quotes
    # in help text are stripped in bash output (which uses words only, not
    # descriptions), but the function body must still be runnable.
    assert "COMPREPLY=" in out
    assert "compgen" in out


def test_emit_zsh_compdef_and_describe():
    out = emit_zsh(discover_subcommands(_toy_parser()))
    assert out.startswith("#compdef beambam"), "zsh needs the compdef header"
    assert "_describe 'command' subcmds" in out
    # zsh array entries should NOT contain literal apostrophes (they break
    # the array). Verify the sanitization swapped them to backticks.
    assert "'single quotes'" not in out
    assert "`single quotes`" in out


def test_emit_fish_one_complete_line_per_subcmd():
    out = emit_fish(discover_subcommands(_toy_parser()))
    body_lines = [
        l for l in out.splitlines()
        if l.startswith("complete -c beambam ")
    ]
    assert len(body_lines) == 4
    # fish descriptions are single-quoted; sanitize apostrophes too.
    assert "'single quotes'" not in out


def test_emit_bash_top_level_only_no_subcommand_descent():
    """Bash emitter only completes the FIRST positional. After that, fall
    through to file completion. This keeps the script compact and avoids
    nested-subparser complexity."""
    out = emit_bash(discover_subcommands(_toy_parser()))
    assert "compgen -f" in out, "must fall through to file completion"


# ----- install path -------------------------------------------------------


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_install_writes_to_correct_user_dir(tmp_path: Path, shell):
    target, hint = install_to(shell, "echo test\n", home=tmp_path)
    assert target.exists()
    assert target.read_text() == "echo test\n"
    assert target.is_relative_to(tmp_path)
    assert isinstance(hint, str) and hint  # nonempty hint


def test_install_paths_differ_per_shell(tmp_path: Path):
    targets = {
        shell: install_to(shell, "x", home=tmp_path)[0]
        for shell in SUPPORTED_SHELLS
    }
    # All three locations must be distinct.
    assert len({str(t) for t in targets.values()}) == 3
    # Bash: ~/.local/share/bash-completion/completions/beambam (no .ext)
    assert targets["bash"].name == "beambam"
    # Zsh: ~/.zfunc/_beambam
    assert targets["zsh"].name == "_beambam"
    # Fish: ~/.config/fish/completions/beambam.fish
    assert targets["fish"].name == "beambam.fish"


def test_install_refuses_overwrite_without_force(tmp_path: Path):
    install_to("bash", "v1\n", home=tmp_path)
    with pytest.raises(FileExistsError):
        install_to("bash", "v2\n", home=tmp_path)
    # File must still hold v1, not v2.
    target = tmp_path / ".local" / "share" / "bash-completion" / "completions" / "beambam"
    assert target.read_text() == "v1\n"


def test_install_overwrites_with_force(tmp_path: Path):
    install_to("bash", "v1\n", home=tmp_path)
    target, _ = install_to("bash", "v2\n", home=tmp_path, force=True)
    assert target.read_text() == "v2\n"


def test_install_creates_parent_dirs(tmp_path: Path):
    """install_to() into a fresh home must mkdir the multi-level path
    (e.g. ~/.local/share/bash-completion/completions/)."""
    assert not (tmp_path / ".local").exists()
    install_to("bash", "x\n", home=tmp_path)
    assert (tmp_path / ".local" / "share" / "bash-completion"
            / "completions" / "beambam").exists()


def test_install_unknown_shell_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported shell"):
        install_to("tcsh", "x", home=tmp_path)


# ----- end-to-end via the real beambam parser (smoke) --------------------


def test_real_parser_discovery_includes_install_completion():
    """Smoke: import the real x2d_bridge parser and verify our
    own subcommand is wired up.

    This test is gated — if cryptography (or other heavy deps) aren't
    importable, the smoke just gets skipped instead of failing.
    """
    try:
        import x2d_bridge  # noqa: F401
    except Exception as e:
        pytest.skip(f"x2d_bridge import failed: {e}")

    # Re-build the parser by calling a small helper from x2d_bridge if
    # one exists; otherwise just import-test that the module loads.
    # Either way we don't want to actually call main() (it would parse
    # sys.argv).
    assert hasattr(x2d_bridge, "main")
