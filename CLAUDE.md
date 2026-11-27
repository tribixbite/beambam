# x2d — Termux notes

Running GUI apps (BambuStudio / wxWidgets 3.3) on termux-x11. These lessons were moved here from the global `~/.claude/CLAUDE.md` (2026-06-28) to keep the global file lean. Reference shim impl: `~/git/x2d/runtime/preload_gtkinit.c`.

## GUI on termux-x11 (EGL / Mesa / wxWidgets)

- **EGL on Termux fails silently when Mesa↔libllvm versions disagree.** Symptom: every `eglGetDisplay` / `eglGetPlatformDisplay` returns `NULL` with `eglGetError() == EGL_BAD_PARAMETER (0x300C)`, regardless of platform (X11/GBM/SURFACELESS/ANDROID/DEVICE), and `eglQueryString(NULL, EGL_EXTENSIONS)` returns NULL — i.e. libglvnd has zero EGL vendors registered. Root cause: libglvnd `dlopen`s the Mesa EGL vendor (`libEGL_mesa.so`), Mesa's `libgallium-X.Y.Z.so` references a versioned LLVM symbol (`LLVMLinkInMCJIT@LLVM_<MAJOR>.1`), and the installed `libllvm` package is one major behind. dlopen fails silently because libglvnd doesn't surface vendor-load errors. Mesa's pacman dep range allows the older libllvm so pacman shows no warning. **Fix: `pacman -S libllvm llvm`** to align versions. Diagnose with `nm -D /data/data/com.termux/files/usr/lib/libgallium-*.so | grep LLVMLinkInMCJIT` (shows expected version) vs `nm -D $PREFIX/lib/libLLVM.so | grep LLVMLinkInMCJIT` (shows installed version). Reference: termux/termux-packages #21642 + PR #21649 — same bug class fired on every LLVM major bump (18→19, 19→20, 20→21).
- **termux-x11 has no DRI/GLX**, so libGL falls back to llvmpipe software rendering and EGL needs `EGL_PLATFORM=surfaceless` or relies on Mesa's auto-detect. Once libllvm matches Mesa, software EGL works fine for GUI apps including wxGLCanvas. The `libEGL warning: DRI3 error: Could not get DRI3 device` line is informational, not an error — Mesa noticed there's no GPU passthrough and silently switched to swrast.
- **Mesa picks zink (Vulkan→GL) instead of llvmpipe by default on Termux** when a Vulkan ICD is present. zink uses kopper for swapchain image presentation, which **requires DRI3/Present** — termux-x11 has neither. The app initialises GL fine (zink reports OpenGL 4.6) but the moment any GL surface is actually drawn (wxGLCanvas viewport, embedded WebView with hardware compositor, etc.), zink_kopper.c:720 asserts `res->obj->dt_idx != UINT32_MAX` and aborts the process. Symptom: GUI launches, runs for tens of seconds, then dies the instant the user clicks a tab/dialog that activates GL. Fix: force llvmpipe in the launcher: `export GALLIUM_DRIVER=llvmpipe LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=llvmpipe EGL_PLATFORM=surfaceless`. llvmpipe renders to an offscreen pixmap and blits via XPutImage, which termux-x11 supports.
- **wxWidgets 3.3 GLCanvas requires EGL** (not GLX). On termux-x11 + bionic, wx 3.3 was built with `--enable-glcanvasegl`; if EGL fails, `glegl.cpp:664 InitConfig()` asserts and the GUI app aborts at first wxGLCanvas creation. There is no GLX fallback path in this build — fix is at the EGL layer (above LLVM lesson), not by toggling wx flags.
- **wxGLCanvasEGL::SwapBuffers() is gated on IsShownOnScreen() under X11.** wx 3.3 `src/unix/glegl.cpp:832` does `if (!IsShownOnScreen()) return false;` before `eglSwapBuffers()`. The default impl chains to `wxWindowBase::IsShownOnScreen()` → `gdk_window_is_viewable()` + `_NET_WM_STATE` queries — termux-x11 implements neither EWMH nor `_NET_WM_STATE`, so the gate is permanently false → SwapBuffers never swaps → 3D viewport renders into the EGL surface but is never blitted (looks blank even though every GL call succeeds and `glReadPixels` returns the rendered image). Fix: LD_PRELOAD shim overriding `_ZNK13wxGLCanvasEGL15IsShownOnScreenEv` to return true. Vtable interposition works because `libwx_gtk3u_gl-3.3.so` was linked WITHOUT `-Bsymbolic-functions` (`readelf -d` shows DT_FLAGS=BIND_NOW only), so R_AARCH64_ABS64 relocations against `_ZTV13wxGLCanvasEGL` slots resolve through the global symbol chain (LD_PRELOAD first). Verify with `readelf -d <lib> | grep SYMBOLIC` (must be empty) and `nm -D <lib> | grep IsShown` (must show `T`/global+default visibility, not `t`/local). Reference impl: `~/git/x2d/runtime/preload_gtkinit.c`.
- **wxLocale on Termux/bionic** triggers a "Switching language failed" modal then `std::exit(EXIT_FAILURE)` because wx 3.3's `wxUILocale::IsAvailable` is ICU-backed and Termux's libicu's locale registry doesn't include `en_US`/`en_GB`. Fix: `LD_PRELOAD` shim that overrides `_ZN8wxLocale11IsAvailableEi` and `_ZN10wxUILocale11IsAvailableEi` to always return `true`, plus `setlocale`/`newlocale` shim that retries with `".UTF-8"` suffix when bionic rejects bare names like `"en_US"` (bionic accepts `"en_US.UTF-8"` and aliases to `C.UTF-8`). Pre-seed `~/.config/<App>/<App>.conf` with `{"app":{"language":"en_US","first_run":false}}` to skip first-run picker. See `~/git/x2d/runtime/preload_gtkinit.c` for the full shim (BambuStudio reference impl).
- **GTK before wx static-init crash**: BambuStudio (and other apps that call `wxFont::AddPrivateFont` from a static initializer) crashes with `Gtk-ERROR: Can't create a GtkStyleContext without a display connection` because wx static-init runs before `gtk_init()`. Fix: same LD_PRELOAD shim with `__attribute__((constructor(101)))` calling `gtk_init_check()` to open the default GdkDisplay before any wx code runs.
- **Touchscreen taps on custom-drawn buttons silently ignored**: BambuStudio's custom widgets (`Button`, `AxisCtrlButton`, `SideButton`, `TabButton`) check `wxRect({0,0}, GetSize()).Contains(event.GetPosition())` on mouse-up before firing the click event. On termux-x11 with touchscreen input, finger drift between down and up takes the up coords outside the button rect, so every Cancel/AMS-spool/sidebar tap gets dropped. Fix: remove the bounds check from each `mouseReleased` and rebuild those 4 TUs. wxNotebook tabs and standard wxButton don't have this bug — they hit-test on down or use GTK's native event routing.

## Shared-library drift (the GUI dies in the linker, not in the app)

- **When x2d "exits ~1–16 s after launch", read the launcher's stderr first** — operad now keeps it in `~/.local/share/tmx/logs/bare-x2d.log`. A `CANNOT LINK EXECUTABLE` / `cannot locate symbol` line means a Termux or firmware update moved a library under the prebuilt `bs-bionic` binary; nothing in BS changed.
- **ffmpeg drift (2026-11, ffmpeg 7.1→8.1):** BS needs avcodec 61 / avutil 59 / swscale 8. The 7.1 libs (+ swresample 5, libvpx 11, x265 4.1) live in `$PREFIX/opt/x2d-ffmpeg7/lib`, prepended by `run_gui.sh`; recipe in its comment. Termux links some deps by **bare name** (`libx265.so`), so "the file exists" proves nothing — check undefined symbols against what the installed lib exports, over the full NEEDED closure. A bare-name compat lib also shadows the new one for every *other* library in the process (it broke GStreamer's ffmpeg-8 libav plugin): give it a private soname with `patchelf --set-soname` + `--replace-needed`, on a copy then `mv` (never patch a file a running process has mapped).
- **Android system libs can resolve against Termux libs of the same soname.** After the Oct 2026 One UI build, ANGLE's Vulkan path loads `/system/lib64/libdng_sdk.so`, which NEEDs `libjxl.so`; bionic reuses the already-loaded Termux `libjxl.so` (pulled in by webkit2gtk), which keeps `JxlResizableParallelRunner*` in `libjxl_threads.so`. Fix: preload Termux's `libjxl_threads.so` for BS only. The dlopen error names the referencing library — start there.

## Tests

- **The full pytest suite rewrites tracked screenshots** (`docs/webui-landscape-s25.png`,
  `docs/webui-portrait-s25.png`, `docs/webui-portrait-tablet.png`, via
  `runtime/webui/test_mobile.py`). Don't commit them by accident with `git add -A`, and expect
  them to block a `git stash pop` if you ran the suite on a stashed tree.
- `runtime/ha/test_*.py` (run through `tests/test_runtime_subprocesses.py`) need the `amqtt`
  broker module; on a box without it those 3 fail regardless of your change. To run them on
  Termux: `pip install --target ~/claudetmp/amqtt-site --no-deps` amqtt + dacite transitions
  websockets==15.0.1 pwdlib typer argon2-cffi(-bindings) (+ typer's deps), and psutil built from
  its sdist with `LINUX = sys.platform.startswith(("linux", "android"))` patched into
  `psutil/_common.py`; then `PYTHONPATH=~/claudetmp/amqtt-site python3 -m pytest tests/`.
- **X2D facts the HA bridge and planner rely on (live, 2026-11-25):** the report carries full
  `push_status` bodies ~1/s, but acks and `info` replies share the topic — the daemon merges them
  (`state_hub.merge_report`). Per-nozzle / chamber temps are packed `cur | target << 16` in
  `device.extruder.info[].temp` / `device.ctc.info.temp`; `net.info[0].ip` is a little-endian
  uint32; firmware versions only come from `info.get_version` (unsigned, answers on LAN);
  `hw_switch_state` is the external-spool filament sensor, not the door. Bambu Studio requests
  `get_auto_nozzle_mapping` only on hotend-rack printers (`fun` bit 60; 0 on the X2D).
- **Headless bs-bionic resets `has_filament_switcher` and `enable_filament_dynamic_map` to false**
  unless given as CLI options (`BambuStudio.cpp` ~3846), whatever the 3MF says; pass
  `--has-filament-switcher=1` (dashes; `--has_filament_switcher=1` exits 254). A project saved by a
  newer Bambu Studio may need `--load-settings <bundled machine.json>` (start G-code variables this
  build lacks). Both are in `beambam/slice_service.py`.
- **Nozzle grouping in a sliced X2D file:** the truth is `slice_info.config` `<filament … group_id>` (0 = left,
  = `filament_map` − 1); project_settings `filament_map` is only the request and stays all `1` in "Auto For Flush"
  while Bambu splits filaments across both nozzles. The CLI takes the grouping mode from the **plate**
  (model_settings `filament_map_mode` / `filament_maps`) before the project: "Manual" only in project_settings
  slices as auto (same groups and time for every map). Bad settings fail with code -51; the reason is the
  `got error when validate:` line in slicer.log (e.g. "Too small line width": a line no wider than the layer).
- **Slicing for the X2D from other inputs (2026-11-26):** an STL / geometry 3MF slices with
  `--load-settings "<machine>;<user process.json>" --load-filaments "<system filament>.json;…"`. A user process
  profile needs `inherits` + `compatible_printers` (else code -17, "process not compatible"); a user filament
  profile SEGFAULTS this CLI (rc -11), so filaments are system profiles picked by filament id. A MakerWorld project
  made for another printer (P2S) keeps that printer's profile unless re-targeted: one-nozzle G-code, every group 0.
  `--filament-map-mode Manual --filament-map 2,1` works for meshes. All in `beambam/slice_options.py`.
  `--load-filaments` with a project made **for the X2D** crashes the CLI (rc -6, `std::length_error`, right after
  "Initializing StaticPrintConfigs"), so a project takes the spools' presets by rewriting project_settings
  (`beambam/project_sync.py`: inherits, then `include` templates, then own keys; per-variant blocks by name).
- **Official Bambu Studio CLI off the phone (2026-11-26, CI job `slicer`, v02.09.02.62):** the Linux AppImage
  (`--appimage-extract`, run `squashfs-root/AppRun`; needs GTK/WebKit/GStreamer libs on a bare runner) and the
  Windows zip ship NO `*_full` profile folders (macOS's .app does) — `beambam slicer install` flattens them.
  `--load-filaments <file>` does NOT follow `inherits`: pass the `filament_full` copy, or PLA gets density 0 and
  default plate temps. A mesh with no `bed_type` gets Cool Plate, which refuses PETG (code -61; Windows' log names
  no plate). The phone build rejects a binary STL whose facet normals are all zero (code -6); ASCII is fine.
- **Handy's heap over adb (`beambam key`, rooted Saga 192.168.0.82, 2026-11-26):** ~3.5 MB/s over Wi-Fi; `dd` of a
  `[anon:stack_and_tls:*]` mapping hangs (guard page) even with `conv=noerror`: skip stacks. The app cert is in the
  heap too (PEM/DER, issuer `GLOF<n>.bambulab.com`) once Handy's Devices tab has connected; the bottom tab's
  content-desc is "Devices". A cloud print without any app key: only Bambu Connect (`bambu-connect://import-file`,
  Windows/macOS); `/my/task` needs PoP signed with an app key (open-bamboo-networking §10.5).
- **The X2D's plate marker** `device.plate.cur_id` P0301 is the SuperTack plate (owner-confirmed 2026-11-26; `base`
  9, `mat` 1). No public table: only confirmed markers go in `filament_mapping.PLATE_MARKERS` (and pokefit's copy).
- **Test browsers on this box are shared:** another session drives a headless Chromium on CDP port 9333 (it even
  attached to one started here). Use an unusual port and your own `--user-data-dir`, select tabs by URL, and never
  kill "chromium on port N" without checking whose it is. Edge on the phone: CDP via
  `adb forward tcp:9238 localabstract:chrome_devtools_remote` only answers while Edge is in the foreground; the first
  request after allowing its "reach apps on this device" prompt took ~7 s.
- **Headless Chromium on this phone has no WebGL** (not even `--use-angle=swiftshader --enable-unsafe-swiftshader`):
  the /print 3D preview shows its "unavailable" fallback there. Check the preview in Edge on the phone (screen
  unlocked; `NotificationShade` focus means locked, and Edge then renders nothing). Everything else on /print
  tests fine headless.
- **No reverse-DNS on the daemon's start path:** `socket.getfqdn()` takes ~30 s on the macOS CI
  runners, and in `http_validate.allowed_host_names` (run before the bind) it made every HTTP
  test there fail with "connection refused" (2026-11-25).
- **Cross-platform test traps the Termux box hides (bit v1.10.0's new tests, 2026-11-26):** a path
  put in a URL is `/`→`%2F` on POSIX but `\`→`%5C` on Windows (assert either); `os.chmod(…, 0o600)`
  is a no-op on Windows (`st_mode & 0o777` is not `0o600` there — guard the check with
  `sys.platform != "win32"`); and the `daemon` fixture (test_http_print) timed out on macOS only when
  it was the session's FIRST spin-up, because `_serve_http` imports `print_service` / `slice_service` /
  `filament_mapping` lazily *before* it binds and those are cold unless some earlier test imported them
  at module level — the fixture now warms them first and waits 30 s. mypy 2.4 (CI) also needs
  `np.frombuffer(...)` annotated where 2.1 (local) did not.
- CLI help is built by `beambam/cli/manhelp.py` (ManParser, inherited by every subparser) and
  argument text missing at the `add_argument` site is filled from `beambam/cli/arghelp.py`.
  `tests/test_cli_manhelp.py` fails if any argument has no help, or an EXAMPLES entry does not
  parse.
- **UX sweep for the CLI:** run every parser from `manhelp.iter_parsers(build_parser())` with no
  arguments under an empty `HOME` (no creds, no cloud session) and `X2D_*` cleared; flag
  tracebacks, hangs (`ha`, `cam watch` legitimately run forever) and unhelpful one-liners. A
  missing printer must exit with `config.missing_creds_message`, never a template or traceback.
  A user-site package (e.g. `cffi`) vanishes under a fake `HOME`, so `ModuleNotFoundError` there
  is an artefact.
