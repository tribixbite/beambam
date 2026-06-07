#!/system/bin/sh
# x2dcap module service.sh — runs as a Magisk late_start service every boot.
# Pins adb-over-wifi to a FIXED TCP port (5555) so the wireless-debugging serial
# stops rotating across reboots. Connect with: adb connect <device-ip>:5555
# (The device IP can still change via DHCP — set a DHCP reservation on the
# router for a fully-static serial.)
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 5
resetprop service.adb.tcp.port 5555
setprop service.adb.tcp.port 5555
stop adbd
start adbd
