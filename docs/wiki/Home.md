# beambam Wiki

beambam prints your own files on a Bambu Lab printer (built for the **X2D**) from a local daemon — no
cloud at print time. These pages are the user guide. For how beambam works inside, see the
[technical specs](../specs/README.md).

## Guides

- **[Printing a file](Printing-a-file.md)** — bring a 3MF / STL / OBJ, slice it, map your spools, print.
- **[Colour painting](Colour-painting.md)** — tap a model to paint parts a different colour.

## Getting set up

Run the daemon and open the web UI:

```
beambam daemon --http 127.0.0.1:8765
```

Then open **https://beambam.boo** (or https://pokefit.party) and connect to `http://127.0.0.1:8765`.
Both sites are allowed by default; the daemon only listens on your own machine.

## Troubleshooting

- **"beambam does not allow this page"** — update beambam; beambam.boo and pokefit.party are allowed by
  default now. (Or start with `--cors-origin https://beambam.boo`.)
- **The print came out with no filament ("printed air")** — update beambam and restart the daemon; a
  profile fix (2026-11-27) ensures the sliced file loads filament. After updating, the daemon rebuilds
  its slicer profiles on the next slice.
- **The 3D preview is blank** — some devices/browsers lack WebGL; you can still slice and print.
