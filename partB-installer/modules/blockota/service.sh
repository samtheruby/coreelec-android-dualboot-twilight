#!/system/bin/sh
# Block OTA each boot -- twilight CoreELEC internal dual-boot.
#
# Why a boot-time module (not a one-time `pm disable`): the dual-boot installer
# shrinks + reformats userdata, which erases /data/system (so a pre-install
# `pm disable` is lost). And a single disable can be re-enabled. This runs in
# Magisk's late_start service context on EVERY boot, re-asserting the block, so:
#   - the Xiaomi updater can't download/stage an OTA, and
#   - automatic system updates stay off.
# An A/B OTA would write boot+dtbo to the INACTIVE slot (= our CoreELEC slot) and
# flip the active slot, clobbering CoreELEC -- this prevents that trigger.
# Remove the module (Magisk app) to restore normal OTA.

MODDIR=${0%/*}
LOG="$MODDIR/blockota.log"

PKGS="com.xiaomi.mitv.updateservice"

# retry <tries> <cmd...> : framework binder calls can fail ("Failed transaction")
# until the system is fully settled even after boot_completed; retry a few times.
retry() {
  _t=$1; shift; _n=0
  while [ "$_n" -lt "$_t" ]; do
    "$@" 2>/dev/null && return 0
    _n=$((_n + 1)); sleep 2
  done
  return 1
}

{
  echo "[blockota] $(date 2>/dev/null) boot"
  # Wait until Android is FULLY booted. `pm path android` returns early, but
  # disable-user/clear/settings then fail with "Failed transaction (2147483646)"
  # until the framework is fully up -- so gate on sys.boot_completed + a grace.
  i=0
  while [ "$(getprop sys.boot_completed 2>/dev/null)" != "1" ]; do
    i=$((i + 1)); [ "$i" -gt 150 ] && { echo "[blockota] boot_completed timeout"; break; }
    sleep 2
  done
  sleep 5

  for p in $PKGS; do
    if pm path "$p" >/dev/null 2>&1; then
      # disable first so it can't run/re-download, THEN clear to wipe any already
      # downloaded/staged OTA + its scheduling state.
      # generous retry: on some boxes `cmd package` rejects transactions for a
      # while after boot_completed. Not load-bearing -- the install-time
      # `pm disable-user` is persistent and survives reboots on its own -- but
      # this re-asserts when it can (and is the only place `pm clear` runs).
      retry 120 pm disable-user --user 0 "$p" && echo "[blockota] disabled $p" \
                                              || echo "[blockota] disable $p FAILED (persistent disable still holds)"
      retry 20 pm clear --user 0 "$p" && echo "[blockota] cleared $p" \
                                     || echo "[blockota] clear $p failed"
    else
      echo "[blockota] $p not present (ok)"
    fi
  done
  # belt-and-suspenders: turn off automatic system updates
  retry 20 settings put global ota_disable_automatic_update 1 \
    && echo "[blockota] ota_disable_automatic_update=1"
  retry 20 settings put global auto_update_system 0 \
    && echo "[blockota] auto_update_system=0"
  echo "[blockota] done"
} >> "$LOG" 2>&1
