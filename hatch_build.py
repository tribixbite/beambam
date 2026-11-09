"""Hatch build hook: keep top-level modules LIVE in editable installs.

`pyproject.toml` force-includes the repo's top-level modules
(`cloud_client.py`, `x2d_slice.py`, ...) so regular wheels ship them.
Hatchling's editable build honours `force-include` too, and it *copies*
those files into site-packages, while also putting the repo root on
`sys.path` through the editable `.pth`. site-packages comes first on
`sys.path`, so the copies win: run from any directory other than the repo
(pokefit runs beambam from its own folder), `import x2d_slice` loaded the
copy made at install time, not the edited source. On this dev box those
copies were weeks old.

For editable builds only, this hook narrows the forced files to the
non-Python assets (via hatchling's `force_include_editable` build data);
the Python modules are then imported from the repo root via the `.pth`.
Regular wheel and sdist builds are unchanged.
"""
from __future__ import annotations

from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Non-Python entries of [tool.hatch.build.targets.wheel.force-include].
# An EMPTY mapping would mean "use force-include unchanged" to hatchling,
# so editable builds keep exactly these (unused in editable mode, since the
# web UI is served from the repo, but harmless).
EDITABLE_FORCE_INCLUDE = {"web": "web"}


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name == "wheel" and version == "editable":
            build_data["force_include_editable"] = dict(EDITABLE_FORCE_INCLUDE)
