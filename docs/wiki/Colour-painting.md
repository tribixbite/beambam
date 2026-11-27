# Colour painting

Paint parts of a model a different colour right in beambam, then print it multi-colour — no Bambu
Studio needed. Works on Bambu/Orca **project** files (`.3mf`) on the beambam.boo print page.

## Paint a model

1. Load your `.3mf` and look at it in the **Preview** (step 03).
2. Press **Paint colours**. The preview switches to paint mode (a crosshair cursor).
3. Pick how much a tap paints:
   - **Triangle** — just the facet you tap.
   - **Region** — one smooth patch, up to the nearest crease (use the **Crease** slider to flood
     further or stop sooner).
   - **Part** — the whole connected piece you tap (an ear, a lid, a letter).
   - **Fill** — a bucket fill: everything connected to the tap that is currently the same colour.
4. Pick a colour, or press **+** to add a new one (and tap the swatch to change it). **Pick** turns on
   the eyedropper — the next tap adopts the colour you tap.
5. **Tap the model** to paint. Use **Undo / Redo / Clear** as needed. Press **⌫** then tap to erase
   paint back to the part's own colour.
6. Press **Done painting** when finished.

## Print it

Each new colour you add becomes a filament. After painting, **slice** as usual — the floating status
bar shows progress — then in **Spools** (step 06) map each colour to a loaded AMS spool, and **Start**.

The **Download** button saves the project with your paint and colours baked in, to open elsewhere.

## Notes

- Painting needs an up-to-date beambam daemon. If **+ Add colour** is missing, update beambam
  (`pip install -U beambam`) — an older daemon can only repaint with the file's existing colours.
- The colour you paint with is a placeholder for a filament slot; the real printed colour is whichever
  spool you map it to in step 06.
- Very large models can be slow to paint/slice in the browser; this is best for figures and parts.
