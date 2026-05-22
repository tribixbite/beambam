"""beambam.slice — `beambam slice <stl>` standalone slice CLI.

Thin wrapper around x2d_slice.main() exposed as a top-level subcommand.
Slices an STL through BambuStudio CLI using a known-good X2D template
3mf as the settings carrier, then patches the result so the firmware
accepts it.

This is the "slice but don't print" path. For the chained workflow use
`beambam slice-print` or `beambam print` against the output.

Examples:

    beambam slice model.stl -o model.gcode.3mf
    beambam slice model.stl -o model.gcode.3mf --scale 0.8 --color Gold
    beambam slice model.stl -o model.gcode.3mf --template ref.gcode.3mf
    beambam slice model.stl -o model.gcode.3mf --bed supertack

Output: model.gcode.3mf ready to upload + print (or to feed
`beambam analyze` for a pre-print sanity check).
"""
from __future__ import annotations

import argparse
import sys


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Mirror x2d_slice.main's argparse but as a subparser."""
    from pathlib import Path
    try:
        from x2d_slice import DEFAULT_TEMPLATE
    except ImportError:
        DEFAULT_TEMPLATE = Path("~/.x2d/templates/x2d_default.gcode.3mf")

    p = sub.add_parser(
        "slice",
        help="Slice an STL/OBJ/STP through BambuStudio CLI using an X2D "
             "template profile. Produces a printer-ready .gcode.3mf.",
    )
    p.add_argument("stl", type=Path, help="Input STL / STP / OBJ")
    p.add_argument("--out", "-o", type=Path, required=True,
                   help="Output .gcode.3mf path")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                   help=f"Reference 3mf with embedded X2D profile "
                        f"(default: {DEFAULT_TEMPLATE})")
    p.add_argument("--plate", type=int, default=0,
                   help="Plate to slice (0 = all)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="Uniform STL scale (1.0 = original)")
    p.add_argument("--scale-pct", type=float, default=None,
                   help="Percentage scale (75 = 75%%); wins over --scale "
                        "if both given.")
    p.add_argument("--mm", type=float, default=None,
                   help="Target absolute size (mm) on the largest axis; "
                        "computes a uniform scale to match.")
    p.add_argument("--copies", "--quantity", "-n", type=int, default=1,
                   dest="copies",
                   help="Tile N copies of the model on the plate via 3MF "
                        "instance multipliers. Pre-validates that the "
                        "grid fits the 256x256mm X2D build volume.")
    p.add_argument("--color",
                   help="Primary filament color: #RRGGBB hex or Bambu name "
                        "(e.g. 'Gold', 'PLA Silk Gold')")
    p.add_argument("--bed",
                   help="Bed type: cool/engineering/high_temp/textured/supertack "
                        "or BambuBedType int 1..5")
    p.add_argument("--keep-graft", action="store_true",
                   help="Keep the intermediate grafted 3mf for debugging")
    p.add_argument("--orient", default="original",
                   choices=("original", "flat", "tall", "auto"),
                   help="Pre-slice mesh orientation: original / flat "
                        "(largest-area face on the bed) / tall (longest "
                        "bbox axis aligned with +Z) / auto (= flat).")
    p.set_defaults(fn=cmd_slice)
    return p


def cmd_slice(args: argparse.Namespace) -> int:
    """Delegate to x2d_slice.main() by rebuilding its expected argv."""
    import x2d_slice

    saved_argv = sys.argv[:]
    new_argv = ["x2d_slice.py", str(args.stl),
                "--out", str(args.out),
                "--template", str(args.template),
                "--plate", str(args.plate),
                "--scale", str(args.scale)]
    if getattr(args, "scale_pct", None) is not None:
        new_argv += ["--scale-pct", str(args.scale_pct)]
    if getattr(args, "mm", None) is not None:
        new_argv += ["--mm", str(args.mm)]
    copies = getattr(args, "copies", 1) or 1
    if copies != 1:
        new_argv += ["--copies", str(copies)]
    if args.color:
        new_argv += ["--color", args.color]
    if args.bed:
        new_argv += ["--bed", args.bed]
    if args.keep_graft:
        new_argv += ["--keep-graft"]
    if getattr(args, "orient", "original") != "original":
        new_argv += ["--orient", args.orient]
    try:
        sys.argv = new_argv
        return x2d_slice.main()
    finally:
        sys.argv = saved_argv
