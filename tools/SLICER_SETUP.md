# X2D headless slicing on Termux — setup + gaps filled

Goal: re-slice an arbitrary model (e.g. a MakerWorld `.3mf` authored for another
printer) for a **Bambu Lab X2D**, preserving the model's own process settings
(walls / infill / supports / layer height) while re-targeting the X2D machine +
a build plate + filament. Done headlessly via the bundled `bs-bionic`
BambuStudio (the GUI build — `SLIC3R_GUI=ON`; the `bs-cli` build is
`SLIC3R_GUI=OFF` and **refuses to slice**).

## Quick use

```sh
tools/x2d_cli_slice.sh INPUT.3mf OUTPUT.gcode.3mf OUTDIR \
    --filament "Bambu PETG HF @BBL X2D 0.4 nozzle" \
    --bed "Supertack Plate" --nozzle 0.4
```

Verified: a MakerWorld A1 model (Screw-On Snap Latch, PETG, 4 walls / 25%)
re-sliced to **Bambu Lab X2D + Supertack Plate** with the model's **4 walls /
25% / 0.2mm preserved** (not the X2D default 2 walls / 15%).

## Gaps that blocked it (all filled)

1. **ffmpeg libs missing / wrong arch.** The binary is `BIND_NOW`-linked against
   ffmpeg 7.0 (`libavcodec.so.61`, `libavutil.so.59`, `libswscale.so.8`); the
   bundled copies are dangling symlinks, Termux ships 7.1 (`libavcodec.so.62`,
   wrong SONAME), and `bs-appdir/bin` has them but for **x86_64**. ffmpeg is
   only used for the GUI's camera/media — never on the slice path — so empty
   **stub libs** exporting the ~15 versioned symbols satisfy the loader.
   → `runtime/ffmpeg-stubs/` (regenerate: `python3 runtime/ffmpeg-stubs/build_stubs.py`).

2. **Flattened `*_full` profiles missing.** The CLI loads system presets by name
   from `resources/profiles/BBL/{process,filament,machine}_full/` (the
   inheritance-resolved copies), but the build ships only the source
   (inheritance) profiles → `can not find setting file: …/process_full/<name>.json`.
   → `python3 tools/flatten_bbl_profiles.py` resolves the `inherits` chains and
   writes the `_full` dirs (gitignored — regenerable).

3. **`LD_LIBRARY_PATH` + headless GL.** The bionic linker needs the bs-bionic lib
   dirs + the stub dir; the GL context is only for optional thumbnails and
   degrades gracefully (`LIBGL_ALWAYS_SOFTWARE=1`, no X server). The launcher
   sets these internally (the parent shell filters `LD_*`).

4. **Cross-printer re-slice mechanism.** Pass ONLY the X2D **machine** JSON via
   `--load-settings`; the model's embedded process survives because BS overlays
   only the keys present in the JSON. Add a process JSON too to re-target the
   process. Bed type is overridden by patching `curr_bed_type` in the input 3mf
   (BS CLI has no per-key process flag).

## Notes

- The `update_values_to_printer_extruders … extruder_index 2` errors are benign
  — the X2D is dual-nozzle and the single-filament job leaves nozzle 2 unbound;
  the slice completes (exit 0, valid `Metadata/plate_1.gcode`).
- `bs-bionic/` is gitignored (large build); the committed durable pieces are the
  two generator scripts + this launcher + the ffmpeg stub sources.
