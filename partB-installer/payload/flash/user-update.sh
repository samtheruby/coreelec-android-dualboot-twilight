#!/bin/sh
# CoreELEC post-update hook -- restores the internal dual-boot after a CE update.
# v2 (slot detection hardened).
#
# A CoreELEC OS update rewrites /flash/kernel.img + /flash/dtb.img (+ may touch the
# u-boot env), but our internal boot loads the kernel from boot_<slot> and the dtb
# from dtbo_<slot> (the named partitions u-boot can read), SYSTEM/storage on
# CE_FLASH/CE_STORAGE. CoreELEC's initramfs calls this (sh /flash/user-update.sh)
# right after applying the update, with /flash mounted rw. We re-sync the named
# partitions and re-assert the env gate.
#
# CRITICAL: this runs in the MINIMAL INITRAMFS -- no fw_printenv, no fw_env.config.
# So the CE slot is resolved, in order: (a) read the gate STRAIGHT FROM the env
# partition bytes (single source of truth, always available); (b) fw_printenv if a
# full userspace ever runs this; (c) /flash/ce_slot.conf the installer drops.
# v1 relied on (b)+(c) only and aborted in the initramfs -> stale boot_<slot> ->
# CoreELEC failed to boot after an update.

log() { echo "[user-update] $*"; }

FWCFG=/etc/fw_env.config

# resolve a block-device node for a by-name partition (quiet; node on stdout, or
# return 1). mknods from /sys major:minor if the node is missing (initramfs).
resolve_node() {
  name="$1"
  for cand in "/dev/block/by-name/$name" "/dev/$name"; do
    [ -b "$cand" ] && { echo "$cand"; return 0; }
  done
  real=""
  [ -e "/dev/block/by-name/$name" ] && real=$(readlink -f "/dev/block/by-name/$name")
  if [ -z "$real" ]; then
    for p in /sys/block/mmcblk0/mmcblk0p*; do
      [ -r "$p/uevent" ] || continue
      if grep -q "PARTNAME=$name\$" "$p/uevent" 2>/dev/null; then
        real="/dev/$(basename "$p")"; break
      fi
    done
  fi
  [ -n "$real" ] || return 1
  if [ ! -b "$real" ]; then
    mm=$(cat "/sys/class/block/$(basename "$real")/dev" 2>/dev/null)
    [ -n "$mm" ] || return 1
    mknod "$real" b "${mm%%:*}" "${mm##*:}" || return 1
  fi
  echo "$real"
}

# --- 1. discover the CE slot -------------------------------------------------
SLOT=""
# (a) read the gate from the env partition directly (works in the bare initramfs)
ENVDEV=$(resolve_node env)
if [ -n "$ENVDEV" ]; then
  G=$(dd if="$ENVDEV" bs=512 count=128 2>/dev/null | tr '\000' '\n' | grep "imgread kernel boot_" | head -n1)
  case "$G" in
    *"imgread kernel boot_a"*) SLOT=a ;;
    *"imgread kernel boot_b"*) SLOT=b ;;
  esac
  [ -n "$SLOT" ] && log "CE slot from env partition = ${SLOT}"
fi
# (b) fw_printenv if a full userspace runs this
if [ -z "$SLOT" ] && command -v fw_printenv >/dev/null 2>&1; then
  case "$(fw_printenv -n bootcefromemmc 2>/dev/null)" in
    *"imgread kernel boot_a"*) SLOT=a ;;
    *"imgread kernel boot_b"*) SLOT=b ;;
  esac
  [ -n "$SLOT" ] && log "CE slot from fw_printenv = ${SLOT}"
fi
# (c) installer-dropped slot file
if [ -z "$SLOT" ] && [ -f /flash/ce_slot.conf ]; then
  . /flash/ce_slot.conf
  SLOT="$CE_SLOT"
  [ -n "$SLOT" ] && log "CE slot from ce_slot.conf = ${SLOT}"
fi
if [ -z "$SLOT" ]; then
  log "ERROR: cannot determine CE slot (env read + fw_printenv + ce_slot.conf all failed) -- aborting"
  exit 1
fi
BOOTP="boot_${SLOT}"; DTBOP="dtbo_${SLOT}"
log "CE slot = ${SLOT}  (boot=${BOOTP} dtbo=${DTBOP})"

# --- 2. resolve the boot/dtbo nodes ------------------------------------------
BOOTDEV=$(resolve_node "$BOOTP") || { log "ERROR: cannot find partition $BOOTP"; exit 1; }
DTBODEV=$(resolve_node "$DTBOP") || { log "ERROR: cannot find partition $DTBOP"; exit 1; }
log "nodes: $BOOTP -> $BOOTDEV   $DTBOP -> $DTBODEV"

# --- 3. re-sync kernel + dtb to the u-boot-readable named partitions ---------
if [ -f /flash/kernel.img ]; then
  log "writing /flash/kernel.img -> $BOOTP"
  dd if=/flash/kernel.img of="$BOOTDEV" conv=fsync 2>/dev/null
fi
if [ -f /flash/dtb.img ]; then
  log "writing /flash/dtb.img -> $DTBOP (zero 128 KiB first)"
  dd if=/dev/zero of="$DTBODEV" bs=1024 count=128 2>/dev/null
  dd if=/flash/dtb.img of="$DTBODEV" conv=fsync 2>/dev/null
fi
sync

# --- 3b. re-assert the gated env from the precomputed image (the real fix) ----
# A CoreELEC update REWRITES bootcmd to a stock version that DROPS our boot_ce
# gate (bootcefromemmc survives, but bootcmd no longer runs it) -> "Reboot to
# CoreELEC" stops working. fw_setenv is NOT in the initramfs, so we restore the
# installer's precomputed gated env IMAGE (valid CRC; per-device identity is
# repopulated by keyman at boot, so a snapshot is safe). boot_ce=1 in the image
# also auto-enters the freshly-updated CoreELEC on the post-update reboot.
[ -z "$ENVDEV" ] && ENVDEV=$(resolve_node env)
if [ -f /flash/env_dualboot.bin ] && [ -n "$ENVDEV" ]; then
  log "restoring gated env -> $ENVDEV (re-asserts boot_ce gate)"
  dd if=/flash/env_dualboot.bin of="$ENVDEV" conv=fsync 2>/dev/null
  sync
fi

# --- 4. re-assert the u-boot env gate via fw_setenv (only if it exists) -------
#     (belt-and-suspenders for a full-userspace run; the initramfs uses 3b above)
if command -v fw_setenv >/dev/null 2>&1; then
  if [ ! -b /dev/env ]; then
    mm=$(cat /sys/class/block/mmcblk0p2/dev 2>/dev/null)
    [ -n "$mm" ] && mknod /dev/env b "${mm%%:*}" "${mm##*:}"
  fi
  log "re-applying boot_ce env gate (slot ${SLOT})"
  fw_setenv -c "$FWCFG" bootcefromemmc "setenv bootargs \"\${bootargs} BOOT_IMAGE=kernel.img boot=LABEL=CE_FLASH disk=LABEL=CE_STORAGE console=tty0 no_console_suspend quiet\"; setenv loadaddr \${loadaddr_kernel}; store read \${dtb_mem_addr} ${DTBOP} 0 0x20000; if imgread kernel ${BOOTP} \${loadaddr}; then bootm \${loadaddr}; fi"
  fw_setenv -c "$FWCFG" bootcmd 'if test ${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; else run bootfromsd; run bootfromusb; if test ${boot_ce} = 1; then setenv boot_ce 0; saveenv; run bootcefromemmc; fi; fi; run storeboot'
fi
sync
log "done"
