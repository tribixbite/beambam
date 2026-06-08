#!/system/bin/sh
# x2dcap module service.sh — runs as a Magisk late_start service every boot.
# Pins adb-over-wifi to a FIXED TCP port (5555) so the wireless-debugging serial
# stops rotating across reboots. Connect with: adb connect <device-ip>:5555
# (The device IP can still change via DHCP — set a DHCP reservation on the
# router for a fully-static serial.)
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 5
# persist.* survives even if this service is ever skipped; service.* is what
# adbd actually reads to choose its listen port.
resetprop persist.adb.tcp.port 5555
resetprop service.adb.tcp.port 5555
setprop service.adb.tcp.port 5555
stop adbd
start adbd
# Re-assert if adbd didn't come up on the port (race with late boot).
sleep 5
if ! getprop init.svc.adbd | grep -q running; then
  setprop service.adb.tcp.port 5555
  start adbd
fi
