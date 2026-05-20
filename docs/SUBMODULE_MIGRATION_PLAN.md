# Converting `BambuStudio/` to a submodule (task #15) — migration plan

**Status:** parked, BLOCKED-on-user-review. The user's working tree has
**9 uncommitted modifications** in `BambuStudio/` on top of upstream
`v02.06.00.51` (commit `b506005`) that are NOT captured by anything in
`patches/`. Converting to a submodule before exporting these as patches
would erase them.

## What we want

```
.gitmodules              # tracks Bambulab/BambuStudio @ v02.06.00.51
BambuStudio/             # ← git submodule, vanilla upstream
patches/                 # 61 *.patch files applied via apply-patches.sh
scripts/apply-patches.sh # idempotent: applies every *.patch, refuses if dirty
scripts/refresh-patches.sh # exports BambuStudio HEAD diff back into patches/
```

## What's blocking us

The 9 uncommitted mods are real Termux/build adaptations that aren't yet
in `patches/`:

| File | LoC changed | What it's likely doing |
|---|---|---|
| `CMakeLists.txt` | 28 | top-level build flags (probably `SLIC3R_GUI=OFF`, install dirs) |
| `deps/CMakeLists.txt` | 41 | dep selection / target overrides for aarch64-Termux |
| `deps/GMP/GMP.cmake` | 2 | likely a `--host` triplet fix |
| `deps/OCCT/OCCT.cmake` | 5 | OCCT autotools quirk on Termux |
| `deps/OpenSSL/OpenSSL.cmake` | 10 | OpenSSL build config (Android NDK?) |
| `deps/TIFF/TIFF.cmake` | -2 | dropping an option that doesn't work here |
| `src/BambuStudio.cpp` | 15 | runtime init shim — Termux startup hook? |
| `src/CMakeLists.txt` | 4 | install dir override |

These are all build-system patches — runtime patches live in
`patches/*.cpp.termux.patch` already.

## Migration steps (when ready)

1. **Capture the live mods as patches:**
   ```bash
   cd BambuStudio
   git diff > ../patches/00_build_system.termux.patch
   git reset --hard v02.06.00.51   # safe — work is captured
   ```

2. **Verify patches apply cleanly to a vanilla checkout:**
   ```bash
   cd /tmp && git clone --depth 1 -b v02.06.00.51 \
     https://github.com/bambulab/BambuStudio.git
   cd BambuStudio
   for p in /path/to/x2d/patches/*.patch; do git apply --check "$p"; done
   ```
   Patches that don't apply cleanly need updating before submodule
   conversion (the runtime patches were applied against the local tree
   which may have drifted).

3. **Add as submodule:**
   ```bash
   cd /path/to/x2d
   # Remove BambuStudio from .gitignore first
   sed -i '/^BambuStudio\/$/d' .gitignore
   # Remove the existing checkout (work is now in patches/)
   rm -rf BambuStudio
   # Add as submodule pinned at v02.06.00.51
   git submodule add -b v02.06.00.51 \
     https://github.com/bambulab/BambuStudio.git BambuStudio
   git -C BambuStudio checkout v02.06.00.51   # ensure detached at tag
   ```

4. **Write `scripts/apply-patches.sh`:**
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   ROOT="$(cd "$(dirname "$0")/.." && pwd)"
   cd "$ROOT/BambuStudio"
   if [ -n "$(git status --porcelain)" ]; then
     echo "BambuStudio/ has local mods; refusing to apply patches." >&2
     exit 1
   fi
   for p in "$ROOT"/patches/*.patch; do
     [ -f "$p" ] || continue
     git apply "$p"
   done
   echo "applied $(ls "$ROOT"/patches/*.patch | wc -l) patches"
   ```

5. **Update build-cli.sh / build-app.sh** to call
   `scripts/apply-patches.sh` before `cmake`. Also update CI if it ever
   builds BambuStudio (currently it doesn't — only the Python bridge).

6. **Update README + CONTRIBUTING:** clone command becomes
   `git clone --recurse-submodules` or
   `git submodule update --init --recursive` post-clone.

## Why this isn't done in v1.1.0

Task #15 (submodule conversion) is foundational but the user's 9 in-flight
mods need triage first — copy them verbatim into a patch may not be
ideal if some are debug-only / experimental. Splitting them by purpose
(build-system, runtime, debug) takes a human eye. The patches/ tree
should stay coherent and reviewable.

When the user is ready: read this file, do step 1, decide which mods are
keepers, then proceed.
