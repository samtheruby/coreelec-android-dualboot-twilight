#!/bin/sh
# CoreELEC post-update hook -- restores the internal dual-boot after a CE update.
# v3 (validates before it writes; no longer hardcodes the boot default).
#
# A CoreELEC OS update rewrites /flash/kernel.img + /flash/dtb.img and resets the u-boot
# env's bootcmd to a stock version that DROPS our boot_ce gate. Our internal boot loads the
# kernel from boot_<slot> and the dtb from dtbo_<slot> (the named partitions u-boot can
# read), with SYSTEM/storage on CE_FLASH/CE_STORAGE. CoreELEC's initramfs calls this
# (sh /flash/user-update.sh) right after applying the update, with /flash mounted rw. We
# re-sync the named partitions and re-assert the env gate.
#
# CRITICAL: this runs in the MINIMAL INITRAMFS -- no fw_printenv, no fw_env.config. So the
# CE slot is resolved, in order: (a) read the gate STRAIGHT FROM the env partition bytes
# (single source of truth, always available); (b) fw_printenv if a full userspace ever runs
# this; (c) /flash/ce_slot.conf the installer drops. v1 relied on (b)+(c) only and aborted
# in the initramfs -> stale boot_<slot> -> CoreELEC failed to boot after an update.
#
# THIS SCRIPT WRITES THE ENV PARTITION, UNATTENDED, WITH NO PC AND NO HUMAN WATCHING.
# It is the most dangerous thing in the project, so every write below is validated before
# it happens and verified after. Two rules earned the hard way (see v3 notes):
#   * NEVER dd a blob onto the env partition without checking it first. /flash is FAT32 on
#     eMMC with no journal; a truncated or half-written env_dualboot.bin is a real state,
#     and writing it produces an env whose CRC does not verify.
#   * NEVER hardcode the boot default. v2's fw_setenv fallback wrote the ANDROID-default
#     bootcmd unconditionally, AFTER restoring the env image -- so on a box the user had set
#     to boot CoreELEC by default, an OS update silently flipped it back to Android.

log() { echo "[user-update] $*"; }

FWCFG=/etc/fw_env.config
ENV_SIZE=65536            # 0x10000 -- the u-boot env area; env_dualboot.bin must be exactly this
ENV_BACKUP=/flash/env_preupdate.bin

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

# Does the env area at $1 carry the boot gate for slot partition $2 (e.g. boot_a)?
# The env is NUL-separated key=val, so turn NULs into newlines and grep. This is the only
# env validation available here: there is no fw_printenv and no crc32 tool in the initramfs.
env_has_gate() {
  dd if="$1" bs=512 count=128 2>/dev/null | tr '\000' '\n' | grep -q "imgread kernel $2"
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
# (c) installer-dropped slot file. PARSED, not sourced: `. /flash/ce_slot.conf` executed
# whatever a FAT partition happened to contain, as root, in the initramfs.
if [ -z "$SLOT" ] && [ -f /flash/ce_slot.conf ]; then
  SLOT=$(sed -n 's/^CE_SLOT=\([ab]\)[[:space:]]*$/\1/p' /flash/ce_slot.conf | head -n1)
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
# rc is checked now: a dd that fails here leaves CoreELEC with a stale or half-written
# kernel, and the old code discarded the status and still logged "done".
RC=0
if [ -f /flash/kernel.img ]; then
  log "writing /flash/kernel.img -> $BOOTP"
  if dd if=/flash/kernel.img of="$BOOTDEV" conv=fsync 2>/dev/null; then
    log "  kernel written"
  else
    log "  ERROR: writing kernel.img to $BOOTDEV FAILED -- CoreELEC may not boot"
    RC=1
  fi
fi
if [ -f /flash/dtb.img ]; then
  log "writing /flash/dtb.img -> $DTBOP (zero 128 KiB first)"
  dd if=/dev/zero of="$DTBODEV" bs=1024 count=128 2>/dev/null
  if dd if=/flash/dtb.img of="$DTBODEV" conv=fsync 2>/dev/null; then
    log "  dtb written"
  else
    log "  ERROR: writing dtb.img to $DTBODEV FAILED -- CoreELEC may not boot"
    RC=1
  fi
fi
sync

# --- 3b. re-assert the gated env from the precomputed image (the real fix) ----
# A CoreELEC update rewrites bootcmd to stock, dropping our boot_ce gate, and fw_setenv is
# not in the initramfs. So restore the installer's precomputed gated env IMAGE: it has a
# valid CRC, it already carries THIS box's chosen boot default, and per-device identity is
# repopulated by keyman at boot, so a snapshot is safe.
#
# Validated first, because this is a boot-critical partition and the source lives on a FAT32
# filesystem with no journal.
ENV_RESTORED=0
[ -z "$ENVDEV" ] && ENVDEV=$(resolve_node env)
if [ -f /flash/env_dualboot.bin ] && [ -n "$ENVDEV" ]; then
  SZ=$(wc -c < /flash/env_dualboot.bin 2>/dev/null || echo 0)
  if [ "$SZ" -ne "$ENV_SIZE" ]; then
    log "ERROR: env_dualboot.bin is ${SZ} B, expected ${ENV_SIZE} -- truncated or corrupt."
    log "ERROR: REFUSING to write it to $ENVDEV (a bad env is how a box stops booting)."
    RC=1
  elif ! tr '\000' '\n' < /flash/env_dualboot.bin | grep -q "imgread kernel ${BOOTP}"; then
    log "ERROR: env_dualboot.bin carries no 'imgread kernel ${BOOTP}' gate -- it is corrupt,"
    log "ERROR: or was built for the other slot. REFUSING to write it to $ENVDEV."
    RC=1
  else
    # Keep the pre-update env: if the restore is somehow wrong, this is the way back.
    dd if="$ENVDEV" bs=512 count=128 of="$ENV_BACKUP" conv=fsync 2>/dev/null \
      && log "saved the pre-update env -> $ENV_BACKUP"
    log "restoring gated env -> $ENVDEV (re-asserts the boot_ce gate)"
    if dd if=/flash/env_dualboot.bin of="$ENVDEV" conv=fsync 2>/dev/null; then
      sync
      if env_has_gate "$ENVDEV" "$BOOTP"; then
        log "  env gate verified on $ENVDEV (imgread kernel ${BOOTP})"
        ENV_RESTORED=1
      else
        log "  ERROR: env read-back does NOT show the gate -- the write did not take."
        log "  ERROR: the previous env is at $ENV_BACKUP"
        RC=1
      fi
    else
      log "  ERROR: dd of env_dualboot.bin FAILED; previous env is at $ENV_BACKUP"
      RC=1
    fi
  fi
else
  log "no /flash/env_dualboot.bin (or no env node) -- cannot restore the gated env image"
fi

# --- 4. fw_setenv fallback: ONLY when 3b could not do the job -----------------
# 3b writes a COMPLETE env image that already carries the boot default the user chose at
# install time. This fallback cannot know that choice: an OS update has just reset bootcmd,
# so there is nothing left on the box to read it back from -- which means it can only write
# the ANDROID-default bootcmd.
#
# v2 ran this unconditionally, AFTER 3b. On a box set to boot CoreELEC by default, that
# silently rewrote the default back to Android on every OS update -- the restored image was
# correct, and then this clobbered it. fw_setenv IS present in CoreELEC's full userspace
# (/usr/sbin/fw_setenv), so this was not hypothetical; it was simply never reached in the
# initramfs. Now it only runs when there is no good env image to restore, and it says so.
if [ "$ENV_RESTORED" = 1 ]; then
  log "env restored from the gated image -- skipping the fw_setenv fallback (it would"
  log "overwrite the boot default this box was installed with)"
elif command -v fw_setenv >/dev/null 2>&1; then
  log "WARNING: no usable env_dualboot.bin, falling back to fw_setenv."
  log "WARNING: this fallback can only write the ANDROID-default bootcmd. If this box was"
  log "WARNING: set to boot CoreELEC by default, re-select it: CoreELEC Toolbox addon ->"
  log "WARNING: 'Set default boot OS'."
  # fw_env.config points fw_setenv at /dev/env, so that node has to exist. Derive its
  # major:minor from the env partition we already resolved BY NAME, rather than assuming
  # mmcblk0p2: the stick and box happen to number env identically today, but a partition
  # index is exactly the kind of thing that differs between units, and a wrong /dev/env
  # would send fw_setenv at the wrong partition.
  if [ ! -b /dev/env ] && [ -n "$ENVDEV" ]; then
    mm=$(cat "/sys/class/block/$(basename "$ENVDEV")/dev" 2>/dev/null)
    [ -n "$mm" ] && mknod /dev/env b "${mm%%:*}" "${mm##*:}"
  fi
  log "re-applying the boot_ce gate (slot ${SLOT})"
  fw_setenv -c "$FWCFG" bootcefromemmc "setenv bootargs \"\${bootargs} BOOT_IMAGE=kernel.img boot=LABEL=CE_FLASH disk=LABEL=CE_STORAGE console=tty0 no_console_suspend quiet hdmitx=\"; setenv loadaddr \${loadaddr_kernel}; store read \${dtb_mem_addr} ${DTBOP} 0 0x20000; if imgread kernel ${BOOTP} \${loadaddr}; then bootm \${loadaddr}; fi"
  fw_setenv -c "$FWCFG" bootcmd 'if test ${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; else run bootfromsd; run bootfromusb; if test ${boot_ce} = 1; then setenv boot_ce 0; saveenv; run bootcefromemmc; fi; fi; run storeboot'
else
  log "WARNING: the boot_ce gate could NOT be re-asserted (no env image, no fw_setenv)."
  log "WARNING: 'Reboot to CoreELEC' may not work until the installer re-applies the gate."
  RC=1
fi
sync

if [ "$RC" = 0 ]; then
  log "done"
else
  log "done WITH ERRORS -- see the messages above"
fi
exit "$RC"
