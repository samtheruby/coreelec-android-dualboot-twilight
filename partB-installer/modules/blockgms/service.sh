#!/system/bin/sh
# Block the Google Play Services SYSTEM-UPDATE components on every boot.
#
# Why: on a Google/Android TV box the Settings "Check for updates" flow is owned
# by GMS (com.google.android.gms .update.* -> update_engine). An applied A/B OTA
# writes the INACTIVE slot (= our internal CoreELEC slot) and flips active, which
# clobbers CoreELEC. GMS itself CANNOT be disabled (it breaks Play/accounts/cast),
# but its .update.* COMPONENTS can be disabled individually -- that kills the
# system-update feature while leaving GMS working.
#
# Best-effort each boot. The DURABLE block is the persistent `pm disable` applied
# at install time (component-disabled state survives reboots); this re-asserts it
# where the boot context allows. Remove the module (+ `pm enable`) to restore OTA.

MODDIR=${0%/*}
LOG="$MODDIR/blockgms.log"
GMS=com.google.android.gms

# Both phone + TV variants; non-present ones simply fail and are skipped.
COMPONENTS="
.update.SystemUpdateService
.update.SystemUpdateGcmTaskService
.update.SystemUpdatePersistentListenerService
.update.SystemUpdateActivity
.update.SystemUpdatePanoActivity
.update.OtaSuggestionActivity
.update.OtaPanoSetupActivity
.update.phone.PopupDialog
"

disable_one() { pm disable "$GMS/$GMS$1" >/dev/null 2>&1; }

{
  echo "[blockgms] $(date 2>/dev/null) boot"
  i=0
  while [ "$(getprop sys.boot_completed 2>/dev/null)" != "1" ]; do
    i=$((i + 1)); [ "$i" -gt 150 ] && { echo "[blockgms] boot_completed timeout"; break; }
    sleep 2
  done
  sleep 5

  # Probe readiness on the primary component (retry ~60s); cmd/pm can reject
  # transactions for a while after boot_completed on some boxes.
  ready=0
  n=0
  while [ "$n" -lt 30 ]; do
    if disable_one ".update.SystemUpdateService"; then ready=1; break; fi
    n=$((n + 1)); sleep 2
  done

  if [ "$ready" = 1 ]; then
    echo "[blockgms] disabled .update.SystemUpdateService"
    for c in $COMPONENTS; do
      [ "$c" = ".update.SystemUpdateService" ] && continue
      disable_one "$c" && echo "[blockgms] disabled $c"
    done
  else
    echo "[blockgms] pm unusable at boot on this box -- persistent install-time disable still holds"
  fi
  echo "[blockgms] done"
} >> "$LOG" 2>&1
