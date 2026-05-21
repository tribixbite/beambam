"""beambam.cli — CLI handlers being migrated out of x2d_bridge.py.

Phase 5 of `docs/BRIDGE_SPLIT_PLAN.md`. Each sub-package groups related
handlers:

  beambam.cli.cloud   — every `cmd_cloud_*` handler
  (more to come)

Handlers in this package expose two things to x2d_bridge's `main()`:

  add_subparser(subparsers, root_parser=None) -> None
      Register the subparser(s) the module owns.
  (each `cmd_*` is also importable directly for tests.)

x2d_bridge.py imports `add_subparser` from each module and calls it
inside `main()`. The bridge stays the single argparse-orchestration
point until Phase 5e collapses `main()` into `beambam/cli/__init__.py`
too.
"""
from __future__ import annotations
