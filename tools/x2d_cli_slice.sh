#!/data/data/com.termux/files/usr/bin/bash
# tools/x2d_cli_slice.sh — re-slice a model (.3mf) for a Bambu Lab X2D using the
# bundled bs-bionic BambuStudio, PRESERVING the model's own process settings
# (walls / infill / supports / layer height) while re-targeting the X2D machine
# and (optionally) a build plate + filament.
#
# Fills the gaps that block headless slicing on Termux (all discovered the hard
# way — see tools/SLICER_SETUP.md):
#   1. LD_LIBRARY_PATH → bs-bionic libs + the ffmpeg stubs (runtime/ffmpeg-stubs)
#      because the binary is BIND_NOW-linked against ffmpeg 7.0 (libavcodec.61)
#      which Termux no longer ships; ffmpeg is only used for GUI camera, never
#      on the slice path.
#   2. Headless software-GL env — the GL context is only for optional thumbnails
#      and degrades gracefully, so slicing needs no X server.
#   3. Auto-generates the flattened resources/profiles/BBL/*_full dirs (the BS
#      CLI loads system presets by name from there) if they're missing.
#   4. --load-settings "<X2D machine.json>" re-targets the machine while the
#      model's embedded process survives (only keys present in the JSON are
#      overlaid).
#
# Usage:
#   tools/x2d_cli_slice.sh INPUT.3mf OUTPUT_NAME.gcode.3mf [OUTDIR] \
#       [--filament "Bambu PETG HF @BBL X2D 0.4 nozzle"] \
#       [--bed "Supertack Plate"] [--nozzle 0.4]
set -euo pipefail

ROOT="${X2D_ROOT:-/data/data/com.termux/files/home/git/x2d}"
RES="$ROOT/bs-bionic/resources/profiles/BBL"
BS="$ROOT/bs-bionic/build/src/bambu-studio"

[[ $# -ge 2 ]] || { echo "usage: $0 INPUT.3mf OUTPUT_NAME.gcode.3mf [OUTDIR] [--filament NAME] [--bed NAME] [--nozzle 0.4]" >&2; exit 2; }
SRC="$1"; OUTNAME="$2"; shift 2
OUTDIR="${PWD}"
NOZZLE="0.4"; FILAMENT=""; BED=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --filament) FILAMENT="$2"; shift 2;;
        --bed)      BED="$2";      shift 2;;
        --nozzle)   NOZZLE="$2";   shift 2;;
        --*)        echo "unknown flag $1" >&2; exit 2;;
        *)          OUTDIR="$1";   shift;;
    esac
done
[[ -f "$SRC" ]] || { echo "input not found: $SRC" >&2; exit 1; }
[[ -x "$BS" ]]  || { echo "bs-bionic binary missing: $BS" >&2; exit 1; }
mkdir -p "$OUTDIR"

MACH="$RES/machine/Bambu Lab X2D ${NOZZLE} nozzle.json"
[[ -f "$MACH" ]] || { echo "X2D machine profile missing: $MACH" >&2; exit 1; }
[[ -n "$FILAMENT" ]] || FILAMENT="Bambu PETG HF @BBL X2D ${NOZZLE} nozzle"
FIL="$RES/filament/${FILAMENT}.json"
[[ -f "$FIL" ]] || { echo "filament profile missing: $FIL" >&2; exit 1; }

# (3) ensure flattened profiles exist (BS loads presets by name from *_full/)
if [[ ! -d "$RES/process_full" ]]; then
    echo "[x2d-slice] generating flattened *_full profiles…" >&2
    python3 "$ROOT/tools/flatten_bbl_profiles.py" "$RES" >&2
fi
# (1) ensure ffmpeg stubs exist
if [[ ! -f "$ROOT/runtime/ffmpeg-stubs/libavcodec.so.61" ]]; then
    echo "[x2d-slice] building ffmpeg stubs…" >&2
    python3 "$ROOT/runtime/ffmpeg-stubs/build_stubs.py" "$BS" >&2
fi

# (optional) bed override — patch the model's curr_bed_type before slicing
if [[ -n "$BED" ]]; then
    PATCHED="$OUTDIR/.$(basename "$SRC" .3mf).bed.3mf"
    python3 - "$SRC" "$PATCHED" "$BED" <<'PYEOF'
import sys, zipfile, json
src, dst, bed = sys.argv[1], sys.argv[2], sys.argv[3]
zin = zipfile.ZipFile(src)
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
    for it in zin.infolist():
        data = zin.read(it.filename)
        if it.filename == "Metadata/project_settings.config":
            ps = json.loads(data); ps["curr_bed_type"] = bed
            data = json.dumps(ps, ensure_ascii=False).encode()
        zo.writestr(it, data)
PYEOF
    SRC="$PATCHED"
fi

# (1)+(2) env: ffmpeg stubs first, bs-bionic libs, software GL, no X server.
export LD_LIBRARY_PATH="$ROOT/runtime/ffmpeg-stubs:$ROOT/bs-bionic/build/src:$ROOT/bs-bionic/build/src/local/lib:$ROOT/bs-bionic/deps/build/destdir/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe EGL_PLATFORM=surfaceless MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
export LC_ALL=C LANG=C
unset DISPLAY

echo "[x2d-slice] slicing $(basename "$SRC") → $OUTNAME (X2D ${NOZZLE}, ${FILAMENT}${BED:+, bed=$BED})" >&2
exec "$BS" --slice 0 \
    --load-settings "$MACH" \
    --load-filaments "$FIL" \
    --allow-newer-file=1 \
    --outputdir "$OUTDIR" \
    --export-3mf "$OUTNAME" \
    "$SRC"
