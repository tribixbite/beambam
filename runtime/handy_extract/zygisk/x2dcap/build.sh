#!/data/data/com.termux/files/usr/bin/bash
# build.sh — compile the X2D Capture Zygisk module on Termux (native aarch64).
#
# This is the zero-Frida-footprint capture path: a Zygisk module mapped by
# ReZygisk at zygote-specialize, so Promon SHIELD sees a normal process (no
# ptrace, no frida-agent memfd, no gum threads) and never fork-escapes — the
# app stays FUNCTIONAL with our SSL_write hook live. (Frida fails: SHIELD
# 0xdead-kills it or, once stealthed, forks the app into a non-functional husk
# — see ../../README.md Paths #5-#6.)
#
# Termux's clang 21 targets aarch64-linux-android24 natively, so no separate
# NDK toolchain is needed — only the NDK *headers*:
#   apt-get install ndk-sysroot
#
# Output: libx2dcap.so (~6 KB, stripped, zero termux/frida/gum strings) copied
# to module/zygisk/arm64-v8a.so for packaging.
#
# Flag rationale:
#   -nostdlib++ -fno-exceptions -fno-rtti -fno-threadsafe-statics
#       no libc++ runtime dep (links only libc/libdl/libm — present in every
#       app process); -fno-threadsafe-statics avoids __cxa_guard_* from the
#       REGISTER_ZYGISK_MODULE function-local static.
#   -Wl,--no-undefined : fail at link, not at app-load, on a missing symbol.
# Post-build: patchelf drops the Termux RUNPATH dynamic entry, then we zero the
# leftover "/data/data/com.termux/..." string bytes in .dynstr (else SHIELD
# could scan loaded libs for them); llvm-strip removes symbols.
set -euo pipefail
cd "$(dirname "$0")"

# The module uses __android_log_print (logcat diagnostics). Termux has no
# linkable liblog stub (ndk-multilib-native-stubs mismatches clang 21), so
# generate a minimal stub whose SONAME is liblog.so — the .so gets
# DT_NEEDED=liblog.so and the REAL liblog.so resolves the symbol at runtime in
# the app process. No device binary committed to the repo.
printf 'int __android_log_print(int p,const char*t,const char*f,...){return 0;}\n' \
  | clang --target=aarch64-linux-android24 -shared -fPIC -x c - \
    -nostdlib -Wl,-soname,liblog.so -o liblog.so

clang++ --target=aarch64-linux-android24 -std=c++17 -O2 -fPIC -shared \
  -fvisibility=hidden -ffunction-sections -fdata-sections -Wl,--gc-sections \
  -fno-exceptions -fno-rtti -nostdlib++ -fno-threadsafe-statics \
  -Wl,--no-undefined \
  -I jni jni/x2dcap.cpp jni/And64InlineHook.cpp ./liblog.so -o libx2dcap.so
rm -f liblog.so

patchelf --remove-rpath libx2dcap.so
llvm-strip --strip-all libx2dcap.so

# Zero any leftover termux path string bytes (unreferenced after rpath removal).
python3 - <<'PY'
data = bytearray(open('libx2dcap.so', 'rb').read())
needle = b'/data/data/com.termux'
i = 0
while True:
    i = data.find(needle, i)
    if i < 0:
        break
    j = i
    while j < len(data) and data[j] != 0:
        data[j] = 0
        j += 1
    i = j
open('libx2dcap.so', 'wb').write(bytes(data))
PY

mkdir -p module/zygisk
cp libx2dcap.so module/zygisk/arm64-v8a.so
echo "built libx2dcap.so ($(stat -c %s libx2dcap.so) bytes) -> module/zygisk/arm64-v8a.so"

# Install onto a connected rooted device (ReZygisk picks it up after a zygote
# restart): adb push via /data/local/tmp, then cp into /data/adb/modules.
if [ "${1:-}" = "--install" ]; then
  : "${ANDROID_SERIAL:?set ANDROID_SERIAL to the device}"
  adb push module/module.prop /data/local/tmp/x2d_module.prop
  adb push module/zygisk/arm64-v8a.so /data/local/tmp/x2d_arm64.so
  adb shell 'su -c "
    mkdir -p /data/adb/modules/x2dcap/zygisk
    cp /data/local/tmp/x2d_module.prop /data/adb/modules/x2dcap/module.prop
    cp /data/local/tmp/x2d_arm64.so /data/adb/modules/x2dcap/zygisk/arm64-v8a.so
    chmod 644 /data/adb/modules/x2dcap/module.prop /data/adb/modules/x2dcap/zygisk/arm64-v8a.so
    chcon u:object_r:system_file:s0 /data/adb/modules/x2dcap/zygisk/arm64-v8a.so 2>/dev/null
    rm -f /data/local/tmp/x2d_module.prop /data/local/tmp/x2d_arm64.so
    echo installed - REBOOT or restart zygote to load"'
fi
