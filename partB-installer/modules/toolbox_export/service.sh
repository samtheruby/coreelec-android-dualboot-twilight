#!/system/bin/sh
# Export Android Bluetooth pairings + WiFi/BT MAC to /flash (CE_FLASH) each boot,
# for the CoreELEC Toolbox addon's "Sync Bluetooth remotes" + "Fix WiFi MAC".
#
# Android userdata is FBE-encrypted, so ONLY Android (booted + unlocked) can read
# /data/misc/bluedroid/bt_config.conf. We copy it -- decrypted -- to /flash, which
# is plain FAT32 shared with CoreELEC. Runs after full boot. Reboot Android after
# pairing a new remote to refresh.

MODDIR=${0%/*}
LOG="$MODDIR/toolbox_export.log"
BTCFG=/data/misc/bluedroid/bt_config.conf
CEFLASH=/dev/block/by-name/CE_FLASH
MNT=/mnt/toolbox_ceflash

{
  echo "[toolbox_export] $(date 2>/dev/null) boot"
  i=0
  while [ "$(getprop sys.boot_completed 2>/dev/null)" != "1" ]; do
    i=$((i + 1)); [ "$i" -gt 150 ] && { echo "[toolbox_export] boot_completed timeout"; break; }
    sleep 2
  done
  sleep 5

  [ -b "$CEFLASH" ] || { echo "[toolbox_export] CE_FLASH not found ($CEFLASH) -- not a dual-boot unit?"; exit 0; }
  mkdir -p "$MNT"
  mount -t vfat -o rw "$CEFLASH" "$MNT" 2>/dev/null || mount -o rw,remount "$MNT" 2>/dev/null

  if [ -f "$BTCFG" ]; then
    cp "$BTCFG" "$MNT/android_bt_config.conf" && echo "[toolbox_export] bt_config.conf -> /flash"
  else
    echo "[toolbox_export] no bt_config.conf (no BT pairings yet)"
  fi

  WIFI=$(cat /sys/class/net/wlan0/address 2>/dev/null)
  BT=$(sed -n 's/^Address = //p' "$BTCFG" 2>/dev/null | head -1)
  printf "WIFI_MAC=%s\nBT_MAC=%s\n" "$WIFI" "$BT" > "$MNT/android_macs.conf"
  echo "[toolbox_export] macs: wifi=$WIFI bt=$BT"

  sync
  umount "$MNT" 2>/dev/null
  echo "[toolbox_export] done"
} >> "$LOG" 2>&1
