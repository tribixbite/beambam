#!/usr/bin/env python3
"""runtime/ffmpeg-stubs/build_stubs.py — build empty ffmpeg stub libs so the
bs-bionic BambuStudio binary loads for HEADLESS SLICING.

The bs-bionic GUI build is linked (BIND_NOW) against ffmpeg 7.0 — needs
`libavcodec.so.61` / `libavutil.so.59` / `libswscale.so.8` — but the bundled
copies are dangling symlinks and Termux ships ffmpeg 7.1 (`libavcodec.so.62`,
wrong SONAME). ffmpeg is only used for the GUI's camera/media playback, NEVER
on the slice path, so empty stubs that merely export the ~15 versioned symbols
the binary imports satisfy the loader and let slicing proceed.

This regenerates the stubs by reading the binary's UND ffmpeg symbols (with
their `@LIBAVCODEC_61` version tags) and emitting one tiny versioned .so per
lib. Re-run if the bs-bionic binary is rebuilt.

Usage: python3 runtime/ffmpeg-stubs/build_stubs.py [path/to/bambu-studio]
"""
from __future__ import annotations

import os
import subprocess
import sys

_VER_TO_LIB = {
    "LIBAVCODEC_61": "libavcodec.so.61",
    "LIBAVUTIL_59": "libavutil.so.59",
    "LIBSWSCALE_8": "libswscale.so.8",
    "LIBSWRESAMPLE_5": "libswresample.so.5",
    "LIBAVFORMAT_61": "libavformat.so.61",
}


def _undefined_ffmpeg_syms(binary: str) -> dict[str, set[str]]:
    """version-tag -> {symbol names} for every UND av*/sws_*/swr_* symbol."""
    out = subprocess.run(["readelf", "--dyn-syms", "--wide", binary],
                         capture_output=True, text=True).stdout
    by_ver: dict[str, set[str]] = {}
    for line in out.splitlines():
        if " UND " not in line:
            continue
        token = line.split()[-1]
        if "@" not in token:
            continue
        name, ver = token.split("@", 1)
        ver = ver.lstrip("@")
        if name.startswith(("av", "sws_", "swr_")) and ver in _VER_TO_LIB:
            by_ver.setdefault(ver, set()).add(name)
    return by_ver


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    binary = argv[1] if len(argv) > 1 else os.path.join(
        root, "bs-bionic", "build", "src", "bambu-studio")
    if not os.path.isfile(binary):
        print(f"binary not found: {binary}", file=sys.stderr)
        return 1
    cc = subprocess.run(["command", "-v", "clang"], capture_output=True,
                        text=True, shell=False).stdout.strip() or "clang"

    by_ver = _undefined_ffmpeg_syms(binary)
    if not by_ver:
        print("no UND ffmpeg symbols found — nothing to stub", file=sys.stderr)
        return 0
    for ver, names in sorted(by_ver.items()):
        lib = _VER_TO_LIB[ver]
        cfile = os.path.join(here, f"{ver}.c")
        mapfile = os.path.join(here, f"{ver}.map")
        with open(cfile, "w") as f:
            f.write("/* Auto-generated empty ffmpeg stub — never called on the "
                    "slice path; the GUI build links ffmpeg only for "
                    "camera/media. Regenerate via build_stubs.py. */\n")
            for n in sorted(names):
                f.write(f"void {n}(void) {{}}\n")
        with open(mapfile, "w") as f:
            f.write(f"{ver} {{\n  global:\n")
            for n in sorted(names):
                f.write(f"    {n};\n")
            f.write("  local: *;\n};\n")
        so = os.path.join(here, lib)
        r = subprocess.run(
            [cc, "-shared", "-fPIC", "-nostdlib",
             f"-Wl,-soname,{lib}", f"-Wl,--version-script={mapfile}",
             "-o", so, cfile], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"build FAILED {lib}: {r.stderr[:300]}", file=sys.stderr)
            return 1
        print(f"  built {lib}  ({len(names)} symbols)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
