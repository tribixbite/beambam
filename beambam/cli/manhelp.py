"""beambam.cli.manhelp — man-page style help for every beambam command.

`ManParser` replaces argparse's default help/usage/error output for the
whole command tree. argparse creates every subparser with `type(parent)`,
so making the root a ManParser is enough for all ~140 commands —
including the ones registered by add_subparser() hooks in other modules.

Output is built in two steps so one source feeds every surface:

  parser tree ──build_page()──▶ Page (sections of paragraphs / items)
       Page ──render_terminal()──▶ `--help`, `beambam help …`, error pages
       Page ──render_roff()──────▶ `man beambam` (beambam.1)

Behaviour:
  * `beambam <cmd> --help` / `beambam help <cmd>` → the command's page,
    colourised on a TTY (honours NO_COLOR / FORCE_COLOR) and paged
    through $PAGER (default `less -R -F -X`) when taller than the screen.
  * A command run with missing / invalid arguments → that command's full
    page on stderr followed by the argparse error, exit status 2.
  * An unknown command name → a short "did you mean" list instead of the
    whole manual.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import date
from collections.abc import Iterable
from typing import IO, TYPE_CHECKING, Literal, NoReturn

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

from beambam._version import PACKAGE_VERSION

# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------

SpanKind = Literal["plain", "flag", "meta", "dim"]
Span = tuple[str, SpanKind]


@dataclass
class Para:
    """A paragraph. Reflowed to the page width unless `verbatim`."""
    text: str
    verbatim: bool = False


@dataclass
class Item:
    """A definition-list entry: a styled term and its explanation.

    `inline` asks the terminal renderer to put the body in a column next
    to the term (command tables) instead of on the following lines
    (option lists, man-page `.TP` style)."""
    term: list[Span]
    body: list[Para]
    inline: bool = False


@dataclass
class Heading:
    """A sub-heading inside a section (e.g. a command group)."""
    text: str


Block = Para | Item | Heading


@dataclass
class Section:
    title: str
    blocks: list[Block] = field(default_factory=list)


def _sec(title: str, blocks: Iterable[Block] = ()) -> Section:
    """Section from any iterable of blocks (a list[Para] is not a
    list[Block] to the type checker, since lists are invariant)."""
    return Section(title, list(blocks))


@dataclass
class Page:
    name: str                 # "beambam" or "beambam cam start"
    summary: str              # one line, for NAME
    synopsis: list[str]       # plain-text usage lines
    sections: list[Section]


# ---------------------------------------------------------------------------
# Static content for the root page
# ---------------------------------------------------------------------------

ROOT_SUMMARY = "control Bambu Lab printers over the LAN and the Bambu cloud"

ROOT_DESCRIPTION = """\
beambam drives a Bambu Lab printer (X2D, H2D, X1, P1 and A1 series) from the \
command line: query live state, upload and start prints, pause / resume / \
stop, set temperatures, run the AMS and the chamber camera, slice models \
headlessly, and browse or print MakerWorld and Printables designs.

LAN commands talk to the printer directly over MQTT (TLS port 8883) and \
FTPS (port 990) using the access code from the printer's touchscreen. \
Print-control commands on current firmware must be RSA-signed; beambam \
signs them with the key recovered by `beambam key` and routes them over \
the cloud broker when needed (pass --lan to force the LAN path).

Commands named cloud-* use your Bambu account (`beambam cloud-login` \
first). Run `beambam doctor` to check that everything is set up, and \
`beambam init` for a first-run wizard."""

ROOT_ENVIRONMENT: list[tuple[str, str]] = [
    ("X2D_IP, X2D_CODE, X2D_SERIAL",
     "Printer IP, access code and serial; override ~/.x2d/credentials, "
     "overridden by --ip / --code / --serial."),
    ("X2D_PRINTER", "Default [printer:NAME] section; overridden by --printer."),
    ("X2D_CREDS, BEAMBAM_CREDS",
     "Alternative credentials file path for `beambam config`."),
    ("X2D_FORCE_LAN", "When set, print-control verbs always use the LAN path (like --lan)."),
    ("X2D_AUTH_TOKEN", "Bearer token for the HTTP daemons when bound to a non-loopback address."),
    ("X2D_ADB", "adb serial (ip:port) of the phone running Bambu Handy, for `beambam key`."),
    ("BEAMBAM_CAM_URL", "Snapshot URL used by `beambam cam`."),
    ("BEAMBAM_DEBUG", "Show the full Python traceback when a command fails."),
    ("NO_COLOR, FORCE_COLOR", "Disable / force colour in help and status output."),
    ("PAGER, BEAMBAM_NO_PAGER",
     "Pager for long help pages (default `less -R -F -X`); set BEAMBAM_NO_PAGER=1 to print directly."),
]

ROOT_FILES: list[tuple[str, str]] = [
    ("~/.x2d/credentials",
     "INI file with a [printer] section (ip, code, serial) and optional "
     "[printer:NAME] sections; edit with `beambam config`."),
    ("~/.x2d/cloud_session.json", "Saved Bambu cloud session from `beambam cloud-login`."),
    ("~/.x2d/printer_sign_key.pem", "Printer-control RSA signing key from `beambam key`."),
    ("~/.x2d/printer_device_cert.pem", "Printer device certificate from `beambam device-cert`."),
    ("~/.x2d/queue.json", "Print queue state (`beambam queue`)."),
]

EXIT_STATUS: list[tuple[str, str]] = [
    ("0", "Success."),
    ("1", "The command ran but failed (refused print, failed check, not found …)."),
    ("2", "Usage error: unknown command, or missing / invalid arguments."),
    ("3", "Network error: printer or service unreachable, or a timeout."),
    ("130", "Interrupted with Ctrl-C."),
]

# Hand-written examples. Keys are command paths as typed after `beambam`.
# Every flag used here exists on that command (tests/test_cli_manhelp.py
# parses each example against the real parser to keep them honest).
EXAMPLES: dict[str, list[tuple[str, str]]] = {
    "": [
        ("beambam status", "Dump the printer's current state as JSON."),
        ("beambam print benchy.gcode.3mf --slot 2", "Upload a sliced file and print from AMS slot 2."),
        ("beambam slice-print part.stl --scale-pct 80", "Slice an STL for the X2D and print it."),
        ("beambam --printer garage pause", "Pause the printer saved as [printer:garage]."),
        ("beambam help print", "Show the full page for one command."),
        ("beambam help --all", "Read the manual for every command."),
    ],
    "status": [
        ("beambam status", "Default printer from ~/.x2d/credentials."),
        ("beambam --printer garage status --timeout 15", "A named printer, with a longer wait."),
    ],
    "print": [
        ("beambam print part.gcode.3mf", "Upload and print from AMS slot 0."),
        ("beambam print part.gcode.3mf --slot 5 --timelapse", "Slot 5 (AMS 1, tray 1), with a timelapse."),
        ("beambam print part.gcode.3mf --dry-run", "Only check purge waste; touch nothing."),
        ("beambam print part.gcode.3mf --no-upload", "The file is already on the printer."),
    ],
    "push": [("beambam push part.gcode.3mf --remote plate1.gcode.3mf", "Upload under another name.")],
    "pause": [("beambam pause", "Pause the running print.")],
    "resume": [("beambam resume", "Resume a paused print.")],
    "stop": [("beambam stop", "Abort the running print."),
             ("beambam stop --lan", "Abort using the LAN path only.")],
    "skip": [("beambam skip 3 7", "Stop printing objects 3 and 7 on the current plate.")],
    "reboot": [("beambam reboot", "Show what would be sent (dry run)."),
               ("beambam reboot --confirm", "Actually send M999.")],
    "gcode": [("beambam gcode 'M106 P1 S255'", "Part-cooling fan to full speed.")],
    "set-temp": [("beambam set-temp bed 60", "Bed to 60 °C."),
                 ("beambam set-temp nozzle 220 --idx 1", "Right nozzle to 220 °C.")],
    "led": [("beambam led on", "Chamber light on."),
            ("beambam led flashing --on-time 300 --off-time 300 --loops 5", "Blink five times.")],
    "jog": [("beambam jog Z -5", "Move Z 5 mm in the negative direction."),
            ("beambam jog X 10 --feed 3000", "Move X 10 mm at 3000 mm/min.")],
    "ams-load": [("beambam ams-load 0 2", "Load from AMS 0, tray 2.")],
    "slice-print": [("beambam slice-print part.stl --dry-run", "Slice only; keep the .gcode.3mf locally."),
                    ("beambam slice-print part.stl --mm 40 --copies 4 --slot 1", "40 mm tall, four copies, slot 1.")],
    "cloud-login": [("beambam cloud-login --email you@example.com", "Log in; prompts for the password / code.")],
    "cloud-search": [("beambam cloud-search 'cable clip' --limit 5", "Top five MakerWorld results.")],
    "fetch": [("beambam fetch https://www.printables.com/model/12345", "Download a model's files.")],
    "find": [("beambam find", "List Bambu printers that answer SSDP on this network."),
             ("beambam find --add garage", "Save the first one found as [printer:garage].")],
    "doctor": [("beambam doctor", "Check every prerequisite."),
               ("beambam doctor --fix", "Also apply the fixes it can.")],
    "cam snap": [("beambam cam snap shot.jpg", "Save one camera frame.")],
    "cam watch": [("beambam cam watch --hz 1", "Live view in the terminal, one frame per second.")],
    "config add": [("beambam config add garage --ip 192.168.1.50 --code 12345678 --serial 01P00A000000000",
                    "Add a second printer.")],
}

# Root options, shown as a reminder on every command page.
GLOBAL_OPTIONS_NOTE = (
    "The global options --ip, --code, --serial and --printer choose the "
    "printer and go before the command name, e.g. "
    "`beambam --printer garage status`. See `beambam --help`."
)


# ---------------------------------------------------------------------------
# Parser annotation (done once, after the whole tree is built)
# ---------------------------------------------------------------------------

def install(root: argparse.ArgumentParser) -> None:
    """Record each subparser's path, summary and aliases on the parser.

    argparse keeps a subcommand's `help=` only on a pseudo-action of the
    parent, so the page for a subparser could not find its own one-line
    description without this pass."""
    root._bb_path = ()            # type: ignore[attr-defined]
    root._bb_summary = ROOT_SUMMARY  # type: ignore[attr-defined]
    root._bb_aliases = []         # type: ignore[attr-defined]
    _annotate(root, ())


def _annotate(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {ca.dest: ca.help for ca in action._choices_actions}
        names_by_parser: dict[int, list[str]] = {}
        for name, sub in action.choices.items():
            names_by_parser.setdefault(id(sub), []).append(name)
        for names in names_by_parser.values():
            sub = action.choices[names[0]]
            sub._bb_path = path + (names[0],)
            sub._bb_summary = helps.get(names[0]) or ""
            sub._bb_aliases = names[1:]
            _annotate(sub, path + (names[0],))


def _path_of(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    path = getattr(parser, "_bb_path", None)
    if path is not None:
        return path
    return tuple(parser.prog.split()[1:])


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def find_parser(root: argparse.ArgumentParser, words: list[str]) -> argparse.ArgumentParser | None:
    """Walk `words` (e.g. ["cam", "start"]) down the tree; None if unknown."""
    parser = root
    for word in words:
        action = _subparsers_action(parser)
        if action is None or word not in action.choices:
            return None
        parser = action.choices[word]
    return parser


def iter_parsers(root: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    """Every command parser in manual order (depth-first), aliases once."""
    out: list[argparse.ArgumentParser] = []
    seen: set[int] = set()

    def walk(p: argparse.ArgumentParser) -> None:
        action = _subparsers_action(p)
        if action is None:
            return
        for sub in action.choices.values():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            out.append(sub)
            walk(sub)

    walk(root)
    return out


# ---------------------------------------------------------------------------
# Page construction
# ---------------------------------------------------------------------------

_BULLET = re.compile(r"\s*([-*•]|\d+[.)]|\$)\s")


def split_paragraphs(text: str) -> list[Para]:
    """Turn free text into paragraphs.

    Blank lines separate paragraphs. A multi-line block that is indented or
    bulleted keeps its line breaks (tables, lists, shell snippets); anything
    else is reflowed, which undoes the hard wraps of source-code strings."""
    text = textwrap.dedent(text).strip("\n")
    paras: list[Para] = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.splitlines()
        if not lines:
            continue
        structured = len(lines) > 1 and any(
            ln.startswith((" ", "\t")) or _BULLET.match(ln) for ln in lines)
        if structured:
            paras.append(Para(block.rstrip(), verbatim=True))
        else:
            paras.append(Para(" ".join(ln.strip() for ln in lines)))
    return paras


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = m.group(1) if m else text
    return sentence[:-1] if sentence.endswith(".") else sentence


def _formatter(parser: argparse.ArgumentParser) -> argparse.HelpFormatter:
    # A very wide formatter: we only use it for metavars, %-expansion and
    # an unwrapped usage line, then wrap everything ourselves.
    return argparse.HelpFormatter(parser.prog, width=10_000, max_help_position=10_000)


def _synopsis(parser: argparse.ArgumentParser) -> list[str]:
    fmt = _formatter(parser)
    fmt.add_usage(parser.usage, parser._actions, parser._mutually_exclusive_groups, prefix="")
    return [fmt.format_help().strip()]


def _option_term(action: argparse.Action, fmt: argparse.HelpFormatter) -> list[Span]:
    spans: list[Span] = []
    if action.option_strings:
        for i, opt in enumerate(action.option_strings):
            if i:
                spans.append((", ", "plain"))
            spans.append((opt, "flag"))
        if action.nargs != 0:
            meta = fmt._format_args(action, fmt._get_default_metavar_for_optional(action))
            spans += [(" ", "plain"), (meta, "meta")]
    else:
        meta = action.metavar if isinstance(action.metavar, str) else action.dest
        if action.nargs in ("*", argparse.REMAINDER):
            meta = f"[{meta} ...]"
        elif action.nargs == "+":
            meta = f"{meta} [{meta} ...]"
        elif action.nargs == "?":
            meta = f"[{meta}]"
        spans.append((meta, "meta"))
    return spans


def _fmt_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _option_body(action: argparse.Action, fmt: argparse.HelpFormatter,
                 exclusive_with: list[str]) -> list[Para]:
    raw = action.help or ""
    text = fmt._expand_help(action) if raw else ""
    if isinstance(action, argparse._HelpAction):
        text = "Show this help page and exit."
    elif isinstance(action, argparse._VersionAction):
        text = "Print the beambam version and exit."
    extras: list[str] = []
    if action.option_strings and action.required:
        text = "Required. " + text
    if action.choices and not isinstance(action, argparse._SubParsersAction):
        choices = [str(c) for c in action.choices]
        if not all(c in text for c in choices):
            extras.append("One of: " + ", ".join(choices) + ".")
    takes_value = action.nargs != 0 and not isinstance(
        action, (argparse._HelpAction, argparse._VersionAction))
    default = action.default
    # Identity / type checks, not `in (…, False)`: 0 == False would hide
    # meaningful zero defaults such as --loops 0.
    no_default = (default is None or default is False or default is argparse.SUPPRESS
                  or (isinstance(default, (str, list, tuple)) and not default))
    if (takes_value and not no_default
            and "%(default)" not in raw and "default" not in text.lower()):
        extras.append(f"Default: {_fmt_value(default)}.")
    if isinstance(action, (argparse._AppendAction, argparse._AppendConstAction)):
        extras.append("May be given more than once.")
    if exclusive_with:
        extras.append("Cannot be combined with " + ", ".join(exclusive_with) + ".")
    paras = split_paragraphs(text) if text else []
    if extras:
        paras.append(Para(" ".join(extras)))
    return paras


def _exclusive_map(parser: argparse.ArgumentParser) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for group in parser._mutually_exclusive_groups:
        for action in group._group_actions:
            others = [a.option_strings[-1] if a.option_strings else a.dest
                      for a in group._group_actions if a is not action
                      and (a.option_strings or a.dest) != (action.option_strings or action.dest)]
            # Two flags sharing one dest (--on / --off) are listed once each.
            out[id(action)] = sorted(set(others) - set(action.option_strings))
    return out


def _command_items(parser: argparse.ArgumentParser,
                   only: list[str] | None = None, brief: bool = False) -> list[Item]:
    """Items for the subcommands of `parser` (optionally a subset, in order).

    `brief` keeps only each help text's first sentence — used for the root
    overview, where ~140 rows of full descriptions would bury the list."""
    action = _subparsers_action(parser)
    if action is None:
        return []
    helps = {ca.dest: ca.help or "" for ca in action._choices_actions}
    items: list[Item] = []
    seen: set[int] = set()
    names = only if only is not None else list(action.choices)
    for name in names:
        sub = action.choices.get(name)
        if sub is None or id(sub) in seen:
            continue
        seen.add(id(sub))
        aliases = getattr(sub, "_bb_aliases", [])
        term: list[Span] = [(name, "flag")]
        if aliases:
            term.append((" (" + ", ".join(aliases) + ")", "dim"))
        text = " ".join(helps.get(name, "").split())
        items.append(Item(term, [Para(_first_sentence(text) if brief else text)], inline=True))
    return items


def _command_groups() -> list[tuple[str, list[tuple[str, str]]]]:
    # Imported lazily: beambam.cli imports this module.
    from beambam.cli import _COMMAND_GROUPS
    return _COMMAND_GROUPS


def _examples_section(path: tuple[str, ...]) -> Section | None:
    examples = EXAMPLES.get(" ".join(path))
    if not examples:
        return None
    sec = Section("EXAMPLES")
    for cmd, what in examples:
        sec.blocks.append(Item([("$ " + cmd, "flag")], [Para(what)]))
    return sec


def _definition_section(title: str, rows: list[tuple[str, str]]) -> Section:
    return Section(title, [Item([(term, "flag")], [Para(body)]) for term, body in rows])


def build_page(parser: argparse.ArgumentParser) -> Page:
    """Build the man page for one parser (root or any subcommand)."""
    path = _path_of(parser)
    is_root = not path
    name = " ".join(("beambam",) + path)
    summary_src = getattr(parser, "_bb_summary", "") or parser.description or ""
    fmt = _formatter(parser)
    sections: list[Section] = []

    if is_root:
        synopsis = ["beambam [global options] <command> [arguments ...]",
                    "beambam help [<command> [<subcommand>]]",
                    "beambam help --all | --man | --install-man"]
        sections.append(_sec("DESCRIPTION", split_paragraphs(ROOT_DESCRIPTION)))
        sec = Section("GLOBAL OPTIONS")
        for action in parser._actions:
            if action.help is argparse.SUPPRESS or isinstance(action, argparse._SubParsersAction):
                continue
            sec.blocks.append(Item(_option_term(action, fmt), _option_body(action, fmt, [])))
        sections.append(sec)
        sec = Section("COMMANDS")
        subs = _subparsers_action(parser)
        listed: set[str] = set()
        for group_title, cmds in _command_groups():
            present = [c for c, _ in cmds if subs is not None and c in subs.choices]
            if not present:
                continue
            sec.blocks.append(Heading(group_title))
            sec.blocks += _command_items(parser, present, brief=True)
            listed.update(present)
        if subs is not None:
            # Anything registered but not in a curated group still appears.
            canonical = {id(s): n for n, s in reversed(list(subs.choices.items()))}
            rest = [n for n, s in subs.choices.items()
                    if canonical[id(s)] == n and n not in listed
                    and not any(a in listed for a in getattr(s, "_bb_aliases", []))]
            if rest:
                sec.blocks.append(Heading("Other"))
                sec.blocks += _command_items(parser, rest, brief=True)
        sections.append(sec)
        ex = _examples_section(path)
        if ex:
            sections.append(ex)
        sections.append(_definition_section("ENVIRONMENT", ROOT_ENVIRONMENT))
        sections.append(_definition_section("FILES", ROOT_FILES))
        sections.append(_definition_section("EXIT STATUS", EXIT_STATUS))
        sections.append(_sec("SEE ALSO", [Para(
            "`beambam help <command>` for any command below, "
            "`beambam help --all` for every page, `man beambam` after "
            "`beambam help --install-man`. Project: https://beambam.boo")]))
        return Page(name, ROOT_SUMMARY, synopsis, sections)

    synopsis = _synopsis(parser)
    summary = _first_sentence(summary_src) if summary_src else ""
    desc_text = parser.description or summary_src
    desc_paras = split_paragraphs(desc_text) if desc_text else []
    # Skip DESCRIPTION when it would only repeat the NAME line.
    if desc_paras and not (len(desc_paras) == 1
                           and _first_sentence(desc_paras[0].text) == desc_paras[0].text.rstrip(".")):
        sections.append(_sec("DESCRIPTION", desc_paras))
    aliases = getattr(parser, "_bb_aliases", [])
    if aliases:
        sections.append(_sec("ALIASES", [Para(
            "Also available as: " + ", ".join(f"`beambam {' '.join(path[:-1] + (a,))}`"
                                              for a in aliases) + ".")]))

    sub_items = _command_items(parser)
    if sub_items:
        sections.append(_sec("COMMANDS", [*sub_items, Para(
            f"Run `{name} <command> --help` for a command's own options.")]))

    exclusive = _exclusive_map(parser)
    default_titles = {"positional arguments": "ARGUMENTS", "options": "OPTIONS",
                      "optional arguments": "OPTIONS"}
    for grp in parser._action_groups:
        actions = [a for a in grp._group_actions
                   if a.help is not argparse.SUPPRESS
                   and not isinstance(a, argparse._SubParsersAction)]
        if not actions:
            continue
        title = default_titles.get(grp.title or "", (grp.title or "OPTIONS").upper())
        sec = Section(title)
        if grp.description and grp.title not in default_titles:
            sec.blocks += split_paragraphs(grp.description)
        for a in actions:
            sec.blocks.append(Item(_option_term(a, fmt), _option_body(a, fmt, exclusive.get(id(a), []))))
        sections.append(sec)

    ex = _examples_section(path)
    if ex:
        sections.append(ex)
    if parser.epilog:
        sections.append(_sec("NOTES", split_paragraphs(parser.epilog)))
    sections.append(_sec("GLOBAL OPTIONS", [Para(GLOBAL_OPTIONS_NOTE)]))
    see = ["`beambam help`"]
    if len(path) > 1:
        see.insert(0, f"`beambam help {' '.join(path[:-1])}`")
    sections.append(_sec("SEE ALSO", [Para(", ".join(see) + ".")]))
    return Page(name, summary, synopsis, sections)


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------

def use_color(stream: object) -> bool:
    """Colour policy shared by help output: NO_COLOR > FORCE_COLOR > isatty."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty()) and os.environ.get("TERM") != "dumb"


@dataclass(frozen=True)
class Style:
    color: bool

    def _sgr(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color and s else s

    def heading(self, s: str) -> str: return self._sgr("1;36", s)
    def sub(self, s: str) -> str: return self._sgr("1;33", s)
    def flag(self, s: str) -> str: return self._sgr("1", s)
    def meta(self, s: str) -> str: return self._sgr("4", s)
    def dim(self, s: str) -> str: return self._sgr("2", s)
    def error(self, s: str) -> str: return self._sgr("1;31", s)

    def span(self, text: str, kind: SpanKind) -> str:
        return {"flag": self.flag, "meta": self.meta, "dim": self.dim}.get(kind, str)(text)


_CODE = re.compile(r"`([^`]+)`")
_FLAG_WORD = re.compile(r"(?<![\w/.-])(--?[A-Za-z][\w-]*)")


def _inline(line: str, st: Style) -> str:
    """Highlight `code` and --flags inside already-wrapped prose."""
    if not st.color:
        return line
    # Odd-indexed pieces are inside backticks: bold them whole. Flags are
    # highlighted only in the prose between, so an inner reset code can't
    # cut a code span's bold short.
    pieces = re.split(r"`([^`]+)`", line)
    return "".join(st.flag(piece) if i % 2 else
                   _FLAG_WORD.sub(lambda m: st.flag(m.group(1)), piece)
                   for i, piece in enumerate(pieces))


def _wrap(text: str, width: int, indent: int, first: int | None = None) -> list[str]:
    first = indent if first is None else first
    return textwrap.wrap(text, width=max(width, indent + 20),
                         initial_indent=" " * first, subsequent_indent=" " * indent,
                         break_on_hyphens=False, break_long_words=False) or [""]


def _render_para(p: Para, width: int, indent: int, st: Style) -> list[str]:
    if p.verbatim:
        out: list[str] = []
        for ln in p.text.splitlines():
            lead = len(ln) - len(ln.lstrip())
            out += [_inline(x, st) for x in _wrap(ln.strip(), width, indent + lead + 2, indent + lead)]
        return out
    return [_inline(x, st) for x in _wrap(p.text, width, indent)]


def page_width() -> int:
    """Terminal width clamped to a readable 40..100 columns."""
    cols = shutil.get_terminal_size((80, 24)).columns
    return max(40, min(cols, 100))


def _usage_tokens(usage: str) -> list[str]:
    """Split a usage string into tokens, keeping each bracketed group
    ("[--timeout TIMEOUT]", "[-h]", nested "[a [b]]") whole so a wrap never
    splits it. Spaces only separate tokens at bracket depth 0."""
    tokens: list[str] = []
    cur, depth = "", 0
    for ch in usage:
        if ch == " " and depth == 0:
            if cur:
                tokens.append(cur)
            cur = ""
            continue
        depth += {"[": 1, "]": -1}.get(ch, 0)
        cur += ch
    if cur:
        tokens.append(cur)
    return tokens


def _fill_tokens(tokens: list[str], first: int, rest: int) -> list[str]:
    """Greedy line fill: the first line holds `first` columns, the others
    `rest`. A group too wide for any line is split at its own spaces."""
    rows: list[str] = []
    cur = ""
    expanded: list[str] = []
    for tok in tokens:
        expanded += tok.split(" ") if len(tok) > rest else [tok]
    for tok in expanded:
        room = first if not rows else rest
        if cur and len(cur) + 1 + len(tok) > room:
            rows.append(cur)
            cur = tok
        else:
            cur = f"{cur} {tok}" if cur else tok
    if cur:
        rows.append(cur)
    return rows


def render_terminal(page: Page, st: Style, width: int = 80) -> str:
    narrow = width < 64
    ind = 2 if narrow else 4          # section body indent
    body_ind = ind + (4 if narrow else 6)  # option description indent
    lines: list[str] = []
    title = page.name.upper().replace(" ", "-") + "(1)"
    middle = "beambam manual"
    gap = width - 2 * len(title) - len(middle)
    if gap >= 2:
        left = gap // 2
        lines.append(st.dim(title + " " * left + middle + " " * (gap - left) + title))
    else:
        lines.append(st.dim(title))
    lines.append("")

    def section(title: str) -> None:
        lines.append(st.heading(title))

    section("NAME")
    lines += _wrap(f"{page.name} - {page.summary}" if page.summary else page.name, width, ind + 2, ind)
    lines.append("")
    section("SYNOPSIS")
    for syn in page.synopsis:
        # Keep the full command name together (bold); wrap the rest under it.
        head = page.name if syn.startswith(page.name) else syn.split(" ", 1)[0]
        tail = syn[len(head):].strip()
        # Continuation lines hang under the arguments, unless the command
        # name is so long that would leave a sliver; then indent a little.
        hang = ind + len(head) + 1
        if width - hang < 24:
            hang = ind + 4
        rows = _fill_tokens(_usage_tokens(tail), width - (ind + len(head) + 1), width - hang)
        lines.append(" " * ind + st.flag(head) + (" " + rows[0] if rows else ""))
        lines += [" " * hang + r for r in rows[1:]]
    lines.append("")

    for sec in page.sections:
        section(sec.title)
        inline_items = [b for b in sec.blocks if isinstance(b, Item) and b.inline]
        col = min(max((sum(len(t) for t, _ in b.term) for b in inline_items), default=0) + 2,
                  16 if narrow else 26)
        for block in sec.blocks:
            if isinstance(block, Heading):
                if lines[-1] != "" and lines[-1] != st.heading(sec.title):
                    lines.append("")
                lines.append(" " * (ind - 2) + st.sub(block.text))
            elif isinstance(block, Para):
                if lines[-1] != "" and lines[-1] != st.heading(sec.title):
                    lines.append("")
                lines += _render_para(block, width, ind, st)
                lines.append("")
            else:
                term_plain = "".join(t for t, _ in block.term)
                term = "".join(st.span(t, k) for t, k in block.term)
                if block.inline:
                    body = " ".join(p.text for p in block.body)
                    if len(term_plain) + 2 <= col:
                        wrapped = _wrap(body, width, ind + col, ind + col)
                        wrapped[0] = " " * ind + term + " " * (col - len(term_plain)) + _inline(wrapped[0][ind + col:], st)
                        lines += [wrapped[0]] + [_inline(x, st) for x in wrapped[1:]]
                    else:
                        lines.append(" " * ind + term)
                        lines += [_inline(x, st) for x in _wrap(body, width, ind + col)]
                else:
                    lines.append(" " * ind + term)
                    for p in block.body:
                        lines += _render_para(p, width, body_ind, st)
                    if sec.blocks[-1] is not block:
                        lines.append("")
        if lines[-1] != "":
            lines.append("")
    footer_l, footer_r = f"beambam {PACKAGE_VERSION}", date.today().isoformat()
    lines.append(st.dim(footer_l + " " * max(2, width - len(footer_l) - len(footer_r)) + footer_r))
    return "\n".join(lines) + "\n"


def emit(text: str, stream: "SupportsWrite[str] | None" = None) -> None:
    """Write help text, through a pager when it won't fit on screen.

    Only an implicit stdout that is a terminal gets paged; an explicit
    stream (argparse passes one for errors) is written to directly."""
    out: SupportsWrite[str] = stream if stream is not None else sys.stdout
    isatty = getattr(out, "isatty", None)
    rows = shutil.get_terminal_size((80, 24)).lines
    if (stream is not None and stream is not sys.stdout) or not (isatty and isatty()) \
            or os.environ.get("BEAMBAM_NO_PAGER") or text.count("\n") < rows - 1:
        try:
            out.write(text)
            flush = getattr(out, "flush", None)
            if flush is not None:
                flush()
        except BrokenPipeError:  # `beambam help --all | head`
            pass
        return
    argv = shlex.split(os.environ.get("PAGER") or "less")
    env = os.environ.copy()
    if os.path.basename(argv[0]) == "less" and "LESS" not in env:
        env["LESS"] = "-R -F -X"
    try:
        subprocess.run(argv, input=text, text=True, env=env, check=False)
    except OSError:
        out.write(text)


# ---------------------------------------------------------------------------
# roff rendering (man beambam)
# ---------------------------------------------------------------------------

def _roff_escape(s: str) -> str:
    s = s.replace("\\", "\\e").replace("-", "\\-")
    return s


def _roff_text(s: str) -> str:
    """Escape prose and turn `code` into bold."""
    out = _CODE.sub(lambda m: "\x00B" + m.group(1) + "\x00R", s)
    out = _roff_escape(out).replace("\x00B", "\\fB").replace("\x00R", "\\fR")
    return "\n".join(("\\&" + ln) if ln.startswith((".", "'")) else ln for ln in out.splitlines())


def _roff_span(text: str, kind: SpanKind) -> str:
    esc = _roff_escape(text)
    return {"flag": f"\\fB{esc}\\fR", "meta": f"\\fI{esc}\\fR"}.get(kind, esc)


def _roff_blocks(blocks: list[Block]) -> list[str]:
    out: list[str] = []
    for b in blocks:
        if isinstance(b, Heading):
            out += [".SS " + _roff_escape(b.text)]
        elif isinstance(b, Para):
            out += [".nf", _roff_text(b.text), ".fi"] if b.verbatim else [".PP", _roff_text(b.text)]
        else:
            out += [".TP", "".join(_roff_span(t, k) for t, k in b.term)]
            for i, p in enumerate(b.body):
                if i:
                    out.append(".IP")
                out.append(_roff_text(p.text))
    return out


def render_roff(root: argparse.ArgumentParser) -> str:
    """The complete manual as one roff document (root + every command)."""
    rootp = build_page(root)
    today = date.today()
    # man(7) dates are "Month day, year"; mandoc warns on ISO dates.
    out = [f'.TH BEAMBAM 1 "{today:%B} {today.day}, {today.year}" "beambam {PACKAGE_VERSION}" "beambam manual"',
           ".SH NAME", f"beambam \\- {_roff_escape(rootp.summary)}",
           ".SH SYNOPSIS", ".nf", *(_roff_text(s) for s in rootp.synopsis), ".fi"]
    for sec in rootp.sections:
        out += [".SH " + _roff_escape(sec.title), *_roff_blocks(sec.blocks)]
    out.append(".SH COMMAND REFERENCE")
    for p in iter_parsers(root):
        page = build_page(p)
        out += [".SS " + _roff_escape(page.name),
                _roff_text(page.summary) if page.summary else "",
                ".PP", ".nf", *(_roff_text(s) for s in page.synopsis), ".fi"]
        for sec in page.sections:
            if sec.title in ("GLOBAL OPTIONS", "SEE ALSO"):
                continue  # covered once at the top of the manual
            out += [".PP", f"\\fB{_roff_escape(sec.title.title())}\\fR", ".RS 4",
                    *_roff_blocks(sec.blocks), ".RE"]
    # A paragraph macro right after a heading is redundant (mandoc warns).
    cleaned: list[str] = []
    for line in out:
        if line == "" or (line == ".PP" and cleaned and cleaned[-1].startswith((".SH", ".SS"))):
            continue
        cleaned.append(line)
    return "\n".join(cleaned) + "\n"


def render_manual(root: argparse.ArgumentParser, st: Style, width: int) -> str:
    """Every page, root first, separated by a rule — `beambam help --all`."""
    rule = st.dim("─" * width)
    pages = [render_terminal(build_page(root), st, width)]
    pages += [render_terminal(build_page(p), st, width) for p in iter_parsers(root)]
    return ("\n" + rule + "\n\n").join(pages)


# ---------------------------------------------------------------------------
# The parser class
# ---------------------------------------------------------------------------

class ManParser(argparse.ArgumentParser):
    """ArgumentParser whose help, usage errors and unknown-command errors
    are rendered as man pages (see module docstring)."""

    def format_help(self) -> str:
        return render_terminal(build_page(self), Style(False), page_width())

    def print_help(self, file: "SupportsWrite[str] | None" = None) -> None:
        stream = file if file is not None else sys.stdout
        text = render_terminal(build_page(self), Style(use_color(stream)), page_width())
        emit(text, file)

    def error(self, message: str) -> NoReturn:
        err = sys.stderr
        st = Style(use_color(err))
        name = " ".join(("beambam",) + _path_of(self))
        action = _subparsers_action(self)
        m = re.search(r"invalid choice: '([^']*)'", message)
        if m and action is not None and m.group(1) not in action.choices:
            # Unknown (sub)command: a whole page would bury the mistake.
            bad = m.group(1)
            close = difflib.get_close_matches(bad, list(action.choices), n=4, cutoff=0.5)
            err.write(f"{st.error(name + ':')} unknown command '{bad}'\n")
            if close:
                err.write("  did you mean: " + ", ".join(st.flag(c) for c in close) + "?\n")
            err.write(f"  run `{name} --help` to list the commands\n")
            self.exit(2)
        target: argparse.ArgumentParser = self
        if message.startswith("unrecognized arguments") and not _path_of(self):
            # argparse reports leftovers from the ROOT parser even when they
            # were typed after a subcommand; show the page that was meant.
            target = self._deepest_command(sys.argv[1:])
            name = " ".join(("beambam",) + _path_of(target))
        err.write(render_terminal(build_page(target), st, page_width()))
        err.write(f"\n{st.error(name + ': error:')} {self._friendly(message)}\n")
        self.exit(2)

    def _deepest_command(self, argv: list[str]) -> argparse.ArgumentParser:
        """Follow subcommand names in argv as far down the tree as they go."""
        parser: argparse.ArgumentParser = self
        for word in argv:
            action = _subparsers_action(parser)
            if action is not None and word in action.choices:
                parser = action.choices[word]
        return parser

    def _friendly(self, message: str) -> str:
        """Replace argparse's internal dest names for a missing subcommand
        ("required: config_cmd") with wording a user can act on."""
        m = re.match(r"the following arguments are required: (.+)$", message)
        action = _subparsers_action(self)
        if not m or action is None:
            return message
        missing = [x.strip() for x in m.group(1).split(",")]
        sub_names = {action.dest, action.metavar,
                     "{" + ",".join(action.choices) + "}"} - {None}
        rest = [x for x in missing if x not in sub_names]
        if len(rest) == len(missing):
            return message
        text = "no command given — choose one from COMMANDS above"
        if rest:
            text += "; also required: " + ", ".join(rest)
        return text

