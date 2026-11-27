# Printing a file

beambam slices and prints your own `.3mf`, `.stl` or `.obj` on the X2D through the local daemon. A file
made for another Bambu printer (an A1/P2S from MakerWorld) is re-targeted to the X2D automatically.

## Steps (on the beambam.boo print page)

1. **Connect** (step 01) to your daemon (`http://127.0.0.1:8765`). The printer and plate show up.
2. **Choose a file** (step 02) — drop a `.3mf`, `.stl` or `.obj`, or an already-sliced `.gcode.3mf`.
3. **Preview** (step 03) — rotate/zoom the model on the plate; pick the plate if the project has
   several. Optionally [paint colours](Colour-painting.md).
4. **Settings** (steps 04–05) — keep the project's settings or override layer height, walls, infill,
   supports, brim and the build plate, then **Slice**. The floating bar shows progress and the result.
5. **Spools** (step 06) — map each filament to a loaded AMS slot; on a two-nozzle print pick Left/Right
   per filament.
6. **Print** (step 07) — review the summary and **Start**, then watch it with the live camera.

## If it prints air (no filament)

Update beambam and **restart the daemon**. A profile fix (2026-11-27) makes the sliced file include the
AMS load step; before it, the nozzle was never fed. After updating, the daemon rebuilds its slicer
profiles on the next slice automatically.

Also check: the AMS slot you mapped actually has filament threaded, and the nozzle is clear.

## Re-targeted projects

A project made for an A1/P2S/X1C keeps its model, paint and filament assignment but is sliced with the
X2D's own profiles (its own printer profile would make a file for the wrong machine). Its materials are
kept; a painted new colour is added as a filament.
