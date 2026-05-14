# bambu_extract — qemu-x86_64 dlopen wrap of the real `libbambu_networking.so`

Goal: get the real Bambu Lab network plugin to issue a per-installation cert
against our Bambu cloud account so `print.*` MQTT publishes pass the X2D
firmware's signature gate. The plugin's bootstrap signing key is wrapped in
a VMProtect-style VM (we'd need days to crack statically); proxy through the
real plugin under qemu-x86_64 instead.

## Status (2026-05-13)

**Verdict — qemu-dlopen path is blocked by the same VMProtect VM.**

After adding the missing init calls (`set_cert_file`, `connect_server`,
`update_cert`) per BS `GUI_App.cpp:3488-3515` + 5073, plus
`/proc/self/status` and `/proc/<ppid>/cmdline` anti-debug spoofing in
the trace shim, the plugin still fails at `connect_server` with
return code -2 (`BAMBU_NETWORK_ERR_CONNECT_FAILED`). Zero TCP connects
or DNS lookups observed by the LD_PRELOAD trace shim — the plugin
short-circuits before any network call. `connect_server` failing
means `is_user_login` stays false, which means `install_device_cert`
short-circuits — no HTTPS to the cert-issuance endpoint.

Verified separately that `qemu-x86_64` itself CAN reach
`us.mqtt.bambulab.com:8883` and `api.bambulab.com:443` (TCP connect
+ DNS work fine — see `out/qemu_mqtt_probe`). The block is inside
the plugin's VMProtect-wrapped MQTT-auth code, which requires the
embedded bootstrap client cert (AES-GCM-encrypted in `__TEXT,__const`,
only decrypted inside the VM) to authenticate against the cloud
broker. Without unpacking the VM we can't get past this point.

The remaining practical paths to a working per-installation cert+key
pair are:

1. **Run real BambuStudio on a glibc-x86_64 / Linux / Mac / Windows
   machine.** Log in with the user's Bambu account. Snapshot the
   cert + key that the plugin writes to `~/.config/BambuStudio/cert/`
   (Linux) or equivalent. Copy to the Termux installation's
   `bambu_cert.py`. ~30 minutes of work; no RE.
2. **Frida-extract from a logged-in Bambu Handy phone** (pending
   task #24). Layer-3-complete, Layer-4 blocked on Android seccomp.
   Multi-day.
3. **Statically unpack the VMProtect VM** in the plugin. Multi-week
   RE work; recommend skipping.

## Original status (2026-05-09)

| Stage | Status |
|---|---|
| zig cross-compile to x86_64-linux-gnu | ✅ |
| qemu-x86_64 + sysroot can run our binaries | ✅ |
| dlopen the unmodified Bambu plugin under qemu | ✅ |
| Resolve all `bambu_network_*` symbols | ✅ |
| C-side hand-rolled libstdc++ std::string layout | ✅ |
| Itanium C++ ABI: pass std::string by hidden pointer | ✅ |
| `create_agent` + `set_config_dir` + `init_log` + `set_country_code` + `start` | ✅ all return 0 |
| `change_user` accepts our cloud token JSON | ✅ returns 0 |
| **`is_user_login` returns true** | ❌ stays false |
| **`install_device_cert` actually calls api.bambulab.com** | ❌ no network attempt |
| **Per-install cert + key written to `BAMBU_CONFIG_DIR`** | ❌ blocked on above |

Plugin DOES create its own encrypted log file in `BAMBU_LOG_DIR/log/`
(0 bytes — confirms init reaches that point but doesn't progress) and
opens `/proc/self/status` + parent's `cmdline` repeatedly (anti-debug
poll — TracerPid + parent-cmdline check; cosmetic, doesn't block us).

## What's in this dir

- **`dump_driver_c.c`** — pure-C dlopen driver. Hand-rolled
  libstdc++ std::string. Calls create_agent → set_config_dir → init_log
  → set_country_code → start → change_user → install_device_cert.
- **`trace_shim.c`** — LD_PRELOAD shim. Hooks open / openat / creat /
  fopen / write / connect / send / getaddrinfo. Logs to
  `BAMBU_TRACE_LOG`.
- **`out/dump_driver_c`** — built x86_64 ELF.
- **`out/trace_shim.so`** — built x86_64 PIC shared object.
- **`dump_driver.cpp`** — earlier C++ attempt (zig's libc++ uses a
  different std::string ABI than libstdc++; abandoned for the C
  approach). Kept for reference.

## Build + run

```bash
cd runtime/bambu_extract
zig cc  -target x86_64-linux-gnu -O1 -Wno-pointer-bool-conversion \
        -shared -fPIC trace_shim.c -ldl -lpthread -o out/trace_shim.so
zig cc  -target x86_64-linux-gnu -O1 dump_driver_c.c -ldl -o out/dump_driver_c

# Required: a valid cloud session at ~/.x2d/cloud_session.json (run
# `python3.12 x2d_bridge.py cloud-login` if you don't already have one).
TOKEN=$(python3.12 -c "import json,os; print(json.load(open(os.path.expanduser('~/.x2d/cloud_session.json')))['access_token'])")
USER_ID=$(python3.12 -c "import json,os; print(json.load(open(os.path.expanduser('~/.x2d/cloud_session.json')))['user_id'])")

WORK=$TMPDIR/bambu_run
rm -rf "$WORK"; mkdir -p "$WORK/log" "$WORK/certs"
SYSROOT=/data/data/com.termux/files/usr/opt/x86_64-sysroot
SO_DIR=/data/data/com.termux/files/usr/tmp/bambu_plugin/linux
EXTRA_LIBS=/data/data/com.termux/files/home/git/stoatally/tools/x86_64-libs/lib/x86_64-linux-gnu

qemu-x86_64 -L $SYSROOT \
    -E LD_LIBRARY_PATH=$EXTRA_LIBS:$SO_DIR \
    -E LD_PRELOAD=$PWD/out/trace_shim.so \
    -E BAMBU_TRACE_LOG="$WORK/trace.log" \
    -E BAMBU_CONFIG_DIR="$WORK/certs" \
    -E BAMBU_LOG_DIR="$WORK/log" \
    out/dump_driver_c $SO_DIR/libbambu_networking.so "$TOKEN" "$USER_ID"
```

Inspect `$WORK/trace.log` for shim output, `$WORK/certs/` for any cert
file the plugin writes after a successful install_device_cert.

## Next steps to crack `is_user_login`

1. **Register the user-login + on-message callbacks before change_user.**
   The plugin probably runs change_user's token-validate code inside a
   worker thread that calls `set_on_user_login_fn` on success/failure.
   Without that callback registered, the validate path may bail early.
   Symbols to register: `bambu_network_set_on_user_login_fn`,
   `bambu_network_set_on_user_message_fn`,
   `bambu_network_set_on_local_message_fn`,
   `bambu_network_set_on_message_fn`,
   `bambu_network_set_on_server_connected_fn`. These take
   `std::function<...>` which has its own 32-byte-ish ABI — non-trivial
   to construct from C; will need a small C++ wrapper or zig interop.

2. **Try install_device_cert WITH `dev_id=00M09A000000000`** (our
   actual X2D serial) instead of empty string. The cert-issuance flow
   may be different for per-device vs per-account certs.

3. **Try calling `bambu_network_get_my_token` first** — that may
   auto-fetch the token if missing, OR fail-loudly with a useful
   error.

4. **Examine the dylib's internal call graph**:
   `bambu_network_change_user` → `BBL::AccountManager::change_user_internal`
   → reads JSON via `nlohmann::json::parse` → looks for keys.
   Disassemble the change_user implementation (offset `0x462310` per
   nm output for the second copy) to see the exact key names it
   greps. Use objdump under qemu since x86_64 disassembler is on the
   binary itself.

5. **Network reachability under qemu**: confirm qemu-x86_64 lets
   outbound TCP through transparently (tested OK with the earlier
   `zig_test` run, but specifically TLS connections to
   api.bambulab.com may need extra qemu plumbing).

## Why this is the right path

- Bypasses the VMProtect-wrapped bootstrap signing key — we don't need
  to crack it, we let the plugin use it as designed.
- Uses our existing valid Bambu cloud session — no re-login.
- Result is a real Bambu-CA-issued cert + matching private key,
  written to `BAMBU_CONFIG_DIR`. Drop-in replacement for what
  `bambu_cert.py` provides; firmware accepts it for `print.*`
  commands.
- One-shot extraction — afterwards we don't need qemu or the plugin
  again. `bambu_cert.py` and our shim sign with the extracted key
  natively on aarch64.
