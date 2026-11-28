# beambam Technical Specifications

Canonical specs for beambam's subsystems — the design and invariants a maintainer needs, kept next to
the code. Routed from [`AGENTS.md`](../../AGENTS.md). User-facing pages are under [`../wiki/`](../wiki/).

Write a new spec from [`SPEC_TEMPLATE.md`](SPEC_TEMPLATE.md). When a plan is done, fold its enduring
design into the spec and delete the plan — don't let planning docs accumulate.

## Index

| Spec | What it covers |
|---|---|
| [slice-pipeline](slice-pipeline.md) | Slicing on-device: profiles/flattening, re-targeting, the start-G-code / AMS-load (M620) invariant that prevents air prints |
| [colour-painting](colour-painting.md) | Tap-to-paint on beambam.boo: paint_color codec, region detection, serialization, filament append |
| [filament-mapping](filament-mapping.md) | Project-sync (adopt the spools' filaments), appending filaments for painted colours, AMS slot / nozzle mapping |
| [multi-plate-slicing](multi-plate-slicing.md) | Slicing one chosen plate of a multi-plate project + per-object quantity: single-plate extraction (strip assemble, drop empty objects), why it fixes code -6/-50 |

## Related docs (not yet specs)

These older docs cover their areas well; promote to a spec when one needs deep maintenance:

- [`../HTTP_API.md`](../HTTP_API.md) — the daemon's HTTP API and `/healthz` capabilities.
- [`../WEB_UI.md`](../WEB_UI.md) — the beambam.boo web UI.
- [`../LOCAL_CONTROL_PATHS.md`](../LOCAL_CONTROL_PATHS.md), [`../SIGNED_VS_UNSIGNED.md`](../SIGNED_VS_UNSIGNED.md) — LAN control + RSA signing.
- [`../HA.md`](../HA.md), [`../HA_SETUP.md`](../HA_SETUP.md) — Home Assistant.
- [`../X2D_RUNTIME_PIPELINE.md`](../X2D_RUNTIME_PIPELINE.md) — bs-bionic runtime (libs, GUI shims).
- [`../MULTI_PRINTER.md`](../MULTI_PRINTER.md), [`../QUEUE.md`](../QUEUE.md) — multi-printer + print queue.
