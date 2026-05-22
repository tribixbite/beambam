"""Parser-coverage floor: every CLI subcommand must register an `fn=cmd_*`
handler and pass `--help` without crashing.

This is the "at LEAST one test per subcommand" guarantee. Deeper
behavioural tests live in their own per-command test files
(test_tail.py, test_reboot.py, test_plate.py, etc.); this test exists
so a *new* subcommand can't be added without at least proving it parses.

The test enumerates subcommands at collect time by inspecting the
argparse `_SubParsersAction` directly — no subprocess, no shell.
Failure modes the test catches:

  * subparser is registered but doesn't set `fn=` → AttributeError
  * handler points at a removed function → import-time ImportError
  * handler isn't a callable (e.g. a dataclass) → assertion failure
  * `--help` raises (broken metavar / default reference) → SystemExit
    with a non-zero code instead of 0
"""
from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.cli import main


def _build_parser() -> argparse.ArgumentParser:
    """Re-run the same parser builder the CLI uses at startup, but capture
    the constructed parser instead of invoking it. The function inside
    x2d_bridge that owns this is the same one `main()` calls."""
    # `main()` builds the parser inside its own scope. The simplest way
    # to get hands on it is to monkey-patch `parse_args` to return a
    # sentinel so we can intercept the parser. Cleaner: walk the
    # function source for the builder call. Even cleaner: call main()
    # with argv=["--help"] and short-circuit at the subparser step.
    #
    # main signature: main() reading sys.argv. We monkey-patch
    # ArgumentParser.parse_args to capture self at first call.
    captured: dict[str, argparse.ArgumentParser] = {}
    orig = argparse.ArgumentParser.parse_args

    def trap(self, *a, **kw):
        if "parser" not in captured:
            captured["parser"] = self
        # Swallow with a SystemExit so main() returns immediately.
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = trap
    saved_argv = sys.argv
    sys.argv = ["x2d_bridge.py"]  # main() reads sys.argv directly
    try:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main()
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = orig
        sys.argv = saved_argv

    assert "parser" in captured, "couldn't intercept the CLI parser"
    return captured["parser"]


def _subparser_actions(parser: argparse.ArgumentParser,
                       parent_fn=None):
    """Return [(qualified_name, subparser_obj, effective_fn)] for every
    leaf subcommand.

    Two grouping styles in use:
      a) Some groups (`config`, `queue`, `plate`) register `fn=` on
         each LEAF (`config list`, `plate select`).
      b) Other groups (`ams`, `simulate`, `cam`, `mqtt`) register
         `fn=` on the PARENT (`cmd_ams`) and dispatch internally based
         on a `dest=` arg — the leaves carry no `fn=` of their own.

    `effective_fn` walks up so the test sees a handler either way."""
    out: list[tuple[str, argparse.ArgumentParser, object]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        # Dedupe aliases: action._name_parser_map maps every name AND
        # alias to the same parser object. We want the canonical entry
        # only.
        seen_parsers: dict[int, str] = {}
        for name, sub in action.choices.items():
            if id(sub) in seen_parsers:
                continue
            seen_parsers[id(sub)] = name
            own_fn = sub.get_default("fn")
            effective = own_fn or parent_fn
            children = _subparser_actions(sub, parent_fn=effective)
            if children:
                for child_name, child_sub, child_fn in children:
                    out.append((f"{name} {child_name}", child_sub, child_fn))
            else:
                out.append((name, sub, effective))
    return out


_PARSER = _build_parser()
_SUBCMDS = _subparser_actions(_PARSER)

# Sanity: we expect well over 50 subcommands at the time of writing.
# This guards against the introspection trick silently returning [].
assert len(_SUBCMDS) > 50, (
    f"expected >50 subcommands; got {len(_SUBCMDS)} — "
    "the parser-introspection trick probably broke")


@pytest.mark.parametrize("name,sp,fn",
                         _SUBCMDS,
                         ids=[name for name, _, _ in _SUBCMDS])
def test_subcommand_has_callable_handler(name: str,
                                         sp: argparse.ArgumentParser,
                                         fn) -> None:
    """Every subcommand must resolve to a `cmd_*` callable — either
    its own `fn=` default or a parent's that dispatches internally
    (e.g. `cmd_ams` handles `ams status` / `ams set` / etc.)."""
    assert fn is not None, (
        f"subcommand {name!r} has no `fn=` default anywhere up the "
        "parser tree; main() will crash when this subcommand is invoked")
    assert callable(fn), (
        f"subcommand {name!r} resolves to fn={fn!r} which is not callable")
    assert fn.__name__.startswith("cmd_"), (
        f"subcommand {name!r} handler is {fn.__name__!r}; "
        "expected a `cmd_*` function")


@pytest.mark.parametrize("name,sp,fn",
                         _SUBCMDS,
                         ids=[name for name, _, _ in _SUBCMDS])
def test_subcommand_help_does_not_crash(name: str,
                                        sp: argparse.ArgumentParser,
                                        fn) -> None:
    """`beambam SUB --help` must exit 0 with non-empty help text.

    argparse calls sys.exit(0) on --help; we catch SystemExit and
    assert the code is 0 (catches typos in metavar default references
    that raise at help-render time)."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            sp.parse_args(["--help"])
    except SystemExit as e:
        assert e.code == 0, (
            f"--help for {name!r} exited with code {e.code} "
            f"(expected 0). stdout:\n{buf.getvalue()[:500]}")
    out = buf.getvalue()
    assert out.strip(), f"--help for {name!r} produced no output"
    # Every help block should at least name the subcommand somewhere.
    # Loose check — some subcommands include underscores in their
    # description; group commands ("config list") may render just the
    # leaf name. Take the trailing token, normalize hyphens/underscores.
    leaf = name.rsplit(" ", 1)[-1]
    assert leaf in out or leaf.replace("-", "_") in out, (
        f"--help for {name!r} doesn't mention the subcommand leaf "
        f"{leaf!r}; output:\n{out[:300]}")
