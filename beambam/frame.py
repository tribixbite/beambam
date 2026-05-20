"""beambam.frame — picture-frame STL generator with built-in name presets.

Thin wrapper around the existing make_frame.py module so users can do:

    beambam frame --preset mira --out mira_frame.stl
    beambam frame --text "HUNTR/X" --out custom.stl

Presets are data (FRAME_PRESETS dict). To add a new preset, append to the
dict — no code changes needed.

The historical per-printer scripts (make_frame_mira.py, make_frame_rumi.py,
make_frame_zoey.py) were untracked one-line variants of make_frame.py that
hard-coded TOP_TEXT + OUT_PATH. Those can be deleted from any local
working tree — this module supersedes them.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass


@dataclass
class FramePreset:
    text: str
    top_text: str | None = None


# Built-in presets — each maps a `--preset NAME` flag to a (text, top_text)
# pair. Add new ones inline; the CLI auto-discovers them.
FRAME_PRESETS: dict[str, FramePreset] = {
    "mira":   FramePreset(text="MIRA"),
    "rumi":   FramePreset(text="RUMI"),
    "zoey":   FramePreset(text="ZOEY"),
    "huntrx": FramePreset(text="HUNTR/X"),
}


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Wire `beambam frame` into the bridge's argparse tree."""
    p = sub.add_parser(
        "frame",
        help="Generate a picture-frame STL with debossed text. "
             "Presets: " + ", ".join(sorted(FRAME_PRESETS.keys())),
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--preset", choices=sorted(FRAME_PRESETS.keys()),
                       help="Use a built-in name preset")
    group.add_argument("--text", help="Custom bottom-border text "
                                       "(mutually exclusive with --preset)")
    p.add_argument("--top-text", help="Optional text for the top border")
    p.add_argument("--out", required=True,
                   help="Output STL path (e.g. mira_frame.stl)")
    p.add_argument("--deboss-depth", type=float, default=0.6,
                   help="mm of stock removed by text glyphs (default 0.6)")
    p.add_argument("--height", type=float, default=1.2,
                   help="Total stock height in mm (default 1.2)")
    # Pass-through for advanced overrides — kept minimal here, defer to
    # the underlying make_frame.py for everything else.
    p.set_defaults(fn=cmd_frame)
    return p


def cmd_frame(args: argparse.Namespace) -> int:
    """Argparse handler. Resolves preset → text and delegates to make_frame."""
    import make_frame                                    # vendored at repo root
    if args.preset:
        preset = FRAME_PRESETS[args.preset]
        text = preset.text
        top_text = args.top_text if args.top_text is not None else preset.top_text
    elif args.text:
        text = args.text
        top_text = args.top_text
    else:
        print("error: pass --preset NAME or --text 'STRING'", file=sys.stderr)
        return 2

    # Build the inner argv that make_frame.parse_args() expects, then call
    # make_frame.main() with that argv overridden.
    saved_argv = sys.argv[:]
    new_argv = ["make_frame.py", "--text", text, "--out", args.out,
                "--deboss-depth", str(args.deboss_depth),
                "--height", str(args.height)]
    if top_text:
        new_argv += ["--top-text", top_text]
    try:
        sys.argv = new_argv
        return make_frame.main()
    finally:
        sys.argv = saved_argv
