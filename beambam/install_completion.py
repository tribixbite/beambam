"""beambam.install_completion — shell tab-completion for beambam.

Emits a static completion script for bash / zsh / fish, snapshotting the
current subcommand set + top-level flags from the live argparse tree at
generation time.

Why static, not argcomplete?
  argcomplete adds a runtime dependency, requires `# PYTHON_ARGCOMPLETE_OK`
  magic, and re-invokes the parser on every Tab keypress (slow on a
  cold-imported beambam). Static scripts are zero-dependency, instant, and
  cover the 99% case of "complete the next subcommand name".

Usage:

    beambam install-completion bash               # print to stdout
    beambam install-completion zsh                # print to stdout
    beambam install-completion fish               # print to stdout
    beambam install-completion bash --install     # write to user dir
    beambam install-completion zsh  --install     # write to user dir
    beambam install-completion fish --install     # write to user dir

Install locations (chosen so they don't need root):
    bash → ~/.local/share/bash-completion/completions/beambam
    zsh  → ~/.zfunc/_beambam                (user must `fpath+=~/.zfunc; autoload -U compinit; compinit`)
    fish → ~/.config/fish/completions/beambam.fish

We don't auto-edit the user's rc files — only drop the snippet where the
shell looks for it. The post-install message prints any one-time setup
the user still needs (e.g. fpath+= for zsh).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable


# ----- introspection ------------------------------------------------------


def _walk_subcommands(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """Walk a top-level parser and return (name, one-line help) tuples,
    sorted by name.

    Looks for the `_SubParsersAction` in `parser._actions`. There's only
    one in our parser (`sub = p.add_subparsers(...)`); we ignore any
    nested ones (which exist for `ams`, `cam`, `slice`, etc. — those are
    completed lazily and shell-side `-W`/`__fish_seen_subcommand_from` is
    enough for the first-level case).
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(
                (name, _first_line(sp.description or sp.format_usage()
                                  if not action.choices[name].description
                                  else action.choices[name].description))
                for name, sp in action.choices.items()
            )
    return []


def _first_line(text: str) -> str:
    """Squash a help/usage string to one line for completion descriptions."""
    if not text:
        return ""
    line = text.strip().splitlines()[0].strip()
    # Quotes mess up shell completion arrays — replace with backticks.
    return line.replace("'", "`").replace('"', "`")


def _topcmd_help(parser: argparse.ArgumentParser, name: str) -> str:
    """Pull the `help=` text we passed to `sub.add_parser(name, help=...)`.

    The subparser itself doesn't preserve the help= text in a clean
    public attribute, so we scrape the top-level parser's
    `format_help()` for the line that starts with the subcommand name.
    This is fragile but only runs at completion generation time.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # argparse stores subparser-action help on the
            # `_ChoicesPseudoAction` objects exposed in `_choices_actions`.
            for ch in getattr(action, "_choices_actions", []):
                if getattr(ch, "dest", "") == name:
                    return _first_line(getattr(ch, "help", "") or "")
    return ""


def discover_subcommands(parser: argparse.ArgumentParser) -> list[tuple[str, str]]:
    """Public: (name, help) sorted, with help fall-throughs."""
    out: list[tuple[str, str]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name in sorted(action.choices.keys()):
                helptext = _topcmd_help(parser, name) or _first_line(
                    action.choices[name].description or ""
                )
                out.append((name, helptext))
            break
    return out


# ----- emitters -----------------------------------------------------------


def emit_bash(cmds: Iterable[tuple[str, str]]) -> str:
    """Bash completion: top-level subcommand completion only.

    Sourced from `~/.local/share/bash-completion/completions/beambam`,
    which bash-completion picks up via the
    `BASH_COMPLETION_USER_DIR` / XDG search path. No rc-file edit needed.
    """
    words = " ".join(name for name, _ in cmds)
    return (
        "# beambam bash completion — `beambam install-completion bash > "
        "~/.local/share/bash-completion/completions/beambam`\n"
        "_beambam_completions() {\n"
        "    local cur prev\n"
        '    cur="${COMP_WORDS[COMP_CWORD]}"\n'
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"\n'
        "    if [ \"$COMP_CWORD\" -eq 1 ]; then\n"
        f'        COMPREPLY=( $(compgen -W "{words}" -- "$cur") )\n'
        "        return 0\n"
        "    fi\n"
        "    # Fall back to filename completion for arguments.\n"
        "    COMPREPLY=( $(compgen -f -- \"$cur\") )\n"
        "}\n"
        "complete -F _beambam_completions beambam\n"
    )


def emit_zsh(cmds: Iterable[tuple[str, str]]) -> str:
    """Zsh completion: drop in ~/.zfunc/_beambam, ensure fpath+=~/.zfunc.

    Uses `_describe` so each subcommand shows its one-line help in the
    completion menu (the killer feature vs bash)."""
    pairs = "\n".join(
        f"    '{name}:{help_.replace(chr(39), chr(96))}'"
        for name, help_ in cmds
    )
    return (
        "#compdef beambam\n"
        "# beambam zsh completion — drop this file at ~/.zfunc/_beambam\n"
        "# and add to ~/.zshrc:  fpath+=~/.zfunc; autoload -U compinit; compinit\n"
        "\n"
        "_beambam() {\n"
        "    local -a subcmds\n"
        "    subcmds=(\n"
        f"{pairs}\n"
        "    )\n"
        "    if (( CURRENT == 2 )); then\n"
        "        _describe 'command' subcmds\n"
        "        return\n"
        "    fi\n"
        "    _files\n"
        "}\n"
        "_beambam \"$@\"\n"
    )


def emit_fish(cmds: Iterable[tuple[str, str]]) -> str:
    """Fish completion: one `complete -c beambam` line per subcommand.

    Fish's completion system is the most ergonomic of the three — descriptions
    are shown inline as you tab through. Drop in
    ~/.config/fish/completions/beambam.fish (auto-loaded, no rc edit)."""
    lines = [
        "# beambam fish completion — `beambam install-completion fish > "
        "~/.config/fish/completions/beambam.fish`",
        "",
        "# Top-level subcommands (each gates the rest behind "
        "`__fish_use_subcommand`):",
    ]
    for name, help_ in cmds:
        # fish description: single-quoted, no embedded single quotes.
        desc = help_.replace("'", "`")
        lines.append(
            f"complete -c beambam -n __fish_use_subcommand -a '{name}' "
            f"-d '{desc}'"
        )
    return "\n".join(lines) + "\n"


# ----- install --------------------------------------------------------------


# (shell name) → (install path under $HOME, post-install hint)
_INSTALL_TARGETS: dict[str, tuple[str, str]] = {
    "bash": (
        ".local/share/bash-completion/completions/beambam",
        "If you don't have bash-completion installed: "
        "`pkg install bash-completion` (Termux) / "
        "`apt install bash-completion` (Debian). "
        "Then start a new shell.",
    ),
    "zsh": (
        ".zfunc/_beambam",
        "Add this to your ~/.zshrc if you haven't already:\n"
        "    fpath+=~/.zfunc\n"
        "    autoload -U compinit && compinit\n"
        "Then start a new shell.",
    ),
    "fish": (
        ".config/fish/completions/beambam.fish",
        "Auto-loaded on shell start. Open a new fish session to use it.",
    ),
}


def install_to(shell: str, script: str, *, home: Path | None = None,
               force: bool = False) -> tuple[Path, str]:
    """Write `script` to the canonical user-dir location for `shell`.

    Returns (path written, post-install hint). Raises FileExistsError if
    the destination exists and force is False — caller should surface a
    "use --force to overwrite" message.
    """
    if shell not in _INSTALL_TARGETS:
        raise ValueError(f"unsupported shell: {shell}")
    rel, hint = _INSTALL_TARGETS[shell]
    base = home if home is not None else Path.home()
    target = base / rel
    if target.exists() and not force:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script)
    target.chmod(0o644)
    return target, hint


# ----- CLI wiring ---------------------------------------------------------


SUPPORTED_SHELLS = ("bash", "zsh", "fish")


def cmd_install_completion(args: argparse.Namespace) -> int:
    """Subcommand handler. Reachable as `beambam install-completion <shell>`.

    Pulls the live parser via `_get_root_parser()` (passed in via a closure
    from add_subparser to avoid a circular import on x2d_bridge.main)."""
    parser = args._root_parser  # injected by the dispatcher below
    cmds = discover_subcommands(parser)
    if not cmds:
        print("error: no subcommands found on root parser", file=sys.stderr)
        return 2

    emitters = {"bash": emit_bash, "zsh": emit_zsh, "fish": emit_fish}
    script = emitters[args.shell](cmds)

    if args.install:
        try:
            target, hint = install_to(args.shell, script, force=args.force)
        except FileExistsError as e:
            print(
                f"error: {e} exists. Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1
        print(f"wrote {target}")
        print(hint)
        return 0

    sys.stdout.write(script)
    return 0


def add_subparser(sub: "argparse._SubParsersAction",
                  *, root_parser: argparse.ArgumentParser | None = None
                  ) -> argparse.ArgumentParser:
    """Wire `install-completion` into the top-level parser.

    Pass `root_parser` so the handler can introspect it at run time —
    we can't import it from x2d_bridge.py without circularity, and
    argparse doesn't expose the root parser from a subparser action."""
    p = sub.add_parser(
        "install-completion",
        help="Emit shell-completion script for bash / zsh / fish "
             "(use --install to write it to the right user dir).",
    )
    p.add_argument(
        "shell", choices=SUPPORTED_SHELLS,
        help="Target shell. Output is a sourceable script.",
    )
    p.add_argument(
        "--install", action="store_true",
        help="Write to the canonical user completion dir for this shell "
             "instead of printing to stdout.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Overwrite if the target file already exists.",
    )
    # Inject root parser as a default so cmd_install_completion can reach it.
    p.set_defaults(fn=cmd_install_completion, _root_parser=root_parser)
    return p
