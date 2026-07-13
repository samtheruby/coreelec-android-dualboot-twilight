#!/system/bin/sh
# Re-assert the Xiaomi OTA block each boot -- twilight CoreELEC internal dual-boot.
#
# An A/B OTA writes boot+dtbo to the INACTIVE slot (= our CoreELEC slot) and flips active,
# clobbering CoreELEC. install_blockota.py disables the updater from adb, with the framework
# fully up, and VERIFIES it -- that persistent disable is the durable block, and it survives
# both reboots and `pm clear` (measured). This script exists only to put the block back if
# something ever turns the updater on again.
#
# What it must NOT do is what it used to do. The old version fired
# `pm disable-user --user 0 <pkg>` unconditionally, 120 times, 2 s apart. That call FAILS in
# this boot context on this SoC -- every attempt, every boot ("Failure calling service
# package: Failed transaction") -- so a healthy box spent ~4 minutes per boot retrying a
# command that could not work, wrote 40+ error lines to this log, and achieved nothing. The
# sibling blockgms module proves the context is not the problem: its `pm disable <pkg>/<comp>`
# succeeds here on every boot. It is the `--user 0` form specifically that the boot context
# rejects.
#
# So: READS via pm do work here (pm path, pm list) -- use one to check first, and do nothing
# at all when the block is already in place, which is the normal case. Only when the updater
# is actually enabled do we try to fix it, and then we try BOTH command forms.
#
# Remove the module (Magisk app) + `pm enable` to restore normal OTA.

MODDIR=${0%/*}
LOG="$MODDIR/blockota.log"
PKGS="com.xiaomi.mitv.updateservice"

# retry <tries> <cmd...> -- framework binder calls can be rejected for a while after
# boot_completed. Bounded: a call that fails this many times is not going to start working.
retry() {
  _t=$1; shift; _n=0
  while [ "$_n" -lt "$_t" ]; do
    "$@" 2>/dev/null && return 0
    _n=$((_n + 1)); sleep 2
  done
  return 1
}

is_disabled() { pm list packages -d 2>/dev/null | grep -q "^package:$1$"; }

{
  echo "[blockota] $(date 2>/dev/null) boot"
  i=0
  while [ "$(getprop sys.boot_completed 2>/dev/null)" != "1" ]; do
    i=$((i + 1)); [ "$i" -gt 150 ] && { echo "[blockota] boot_completed timeout"; break; }
    sleep 2
  done
  sleep 5

  for p in $PKGS; do
    if ! pm path "$p" >/dev/null 2>&1; then
      echo "[blockota] $p not present (ok)"
      continue
    fi
    if is_disabled "$p"; then
      echo "[blockota] $p already disabled -- nothing to do"
      continue
    fi

    echo "[blockota] $p is ENABLED -- re-asserting the block"
    # Try the --user form first (what install_blockota.py uses from adb), then the form the
    # blockgms module demonstrably CAN run in this context.
    if retry 15 pm disable-user --user 0 "$p" && is_disabled "$p"; then
      echo "[blockota] disabled $p (disable-user)"
    elif retry 15 pm disable "$p" && is_disabled "$p"; then
      echo "[blockota] disabled $p (pm disable)"
    else
      echo "[blockota] could NOT disable $p from the boot context."
      echo "[blockota] Run this from a PC:  python installer/install_blockota.py --serial <serial>"
      continue
    fi
    # Only after the disable holds: wipe anything the app already downloaded/staged.
    retry 5 pm clear --user 0 "$p" && echo "[blockota] cleared $p" \
                                   || echo "[blockota] clear $p failed (the disable still holds)"
  done

  # Automatic-update globals. Check before writing: `settings put` fails in this context on
  # this SoC too, and re-attempting a write that already holds only produced log noise.
  for kv in ota_disable_automatic_update=1 auto_update_system=0; do
    k=${kv%=*}; v=${kv#*=}
    if [ "$(settings get global "$k" 2>/dev/null)" = "$v" ]; then
      echo "[blockota] $k=$v already set"
    elif retry 5 settings put global "$k" "$v"; then
      echo "[blockota] $k=$v"
    else
      echo "[blockota] could not set $k (install_blockota.py sets it from adb)"
    fi
  done
  echo "[blockota] done"
} >> "$LOG" 2>&1
