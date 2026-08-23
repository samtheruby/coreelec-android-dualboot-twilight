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
#   * NEVER let a MISSING TOOL read as a BAD FILE. The only binary here is busybox, its applet
#     set is small AND VARIES BY BOX (no wc/expr/find/sort; some boxes also lack stat and awk).
#     v3 sized the env with `wc -c ... || echo 0` and declared every healthy image "0 B --
#     truncated"; v4's stat/awk fell over the same way on a box with neither. So is_size() now
#     measures with only dd + `[ -s ]`, which are always present. See is_size().

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

# Is file $1 EXACTLY $2 bytes? Built from only `dd` and the shell's own `[ -s ]` -- the two
# things this minimal initramfs is PROVEN to have (dd wrote the kernel above; [ -s ] is a shell
# builtin). It gates a boot-critical write, so it must NOT depend on an OPTIONAL applet:
#   v3  sized with `wc -c ... || echo 0`; busybox here has no wc, so every healthy 65536 B image
#       read as 0 B and the gate was never restored -- a manual re-assert after EVERY CE update.
#   v4  switched to `stat`/`ls+awk`; a real box's initramfs had NEITHER, so it still refused a
#       good image ("cannot measure ... no stat, no ls+awk"). Same bug, different absent applet.
# A byte at 0-indexed offset K reads back iff the file has at least K+1 bytes; the file is
# exactly N bytes when offset N-1 yields a byte and offset N yields nothing. `dd ... of=$ENV_PROBE`
# then `[ -s ]` tests emptiness by SIZE not content, so a probed NUL (the env is mostly NULs)
# still counts as present -- a `$(...)` capture would drop it and mis-measure the file.
ENV_PROBE=/flash/.env_probe.$$
is_size() {
  _f=$1; _n=$2; _ok=1
  dd if="$_f" bs=1 skip=$(( _n - 1 )) count=1 of="$ENV_PROBE" 2>/dev/null
  [ -s "$ENV_PROBE" ] || _ok=0                       # nothing at N-1 -> file is shorter than N
  dd if="$_f" bs=1 skip="$_n"         count=1 of="$ENV_PROBE" 2>/dev/null
  [ -s "$ENV_PROBE" ] && _ok=0                        # a byte at N    -> file is longer than N
  rm -f "$ENV_PROBE"
  [ "$_ok" = 1 ]
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
#
# v5: a stick was stranded by a dd that exited 0 having written all but the last 659,456
# bytes of kernel.img. The kernel region of boot_b matched perfectly and only the tail of
# the zstd initramfs was left over from the previous kernel, so u-boot loaded the image, the
# kernel could not unpack the ramdisk, freed it, and panicked:
#     rootfs image is not initramfs (ZSTD-compressed data is corrupt); looks like an initrd
#     Kernel panic - not syncing: Requested init /init failed (error -2).
# boot_ce is consumed BEFORE bootcefromemmc runs, so the next boot went to Android and the
# whole thing looked like the switcher app silently doing nothing.
#
# We cannot verify content here: there is no cmp and no md5sum in this initramfs (see
# INITRAMFS_MISSING in tests/test_boot_gate.py). Two things we CAN do:
#   * make the source trustworthy before reading it -- sync (commit the updater's writes),
#     then drop the caches (force the read to come off eMMC). sync MUST come first: dropping
#     first can discard the updater's pending writes and hand us the PREVIOUS kernel.img.
#   * stop discarding dd's stderr. dd reports "<N>+0 records out"; that count is the only
#     evidence of a short write available in this environment, and `2>/dev/null` threw it
#     away. We know the expected N because is_size_of() measures the source with dd alone.
# This attests to the byte count dd reported -- not to what reached the platter. The real
# content check lives where the tools exist: KernelGate in the switcher app, and the
# Toolbox repair suite (repair_core.check_kernel_image).
RC=0

# Largest byte count the file has, by doubling then bisecting with is_size()'s probe. dd and
# `[ -s ]` only, for the same reason is_size() is: an optional applet that is missing must
# never read as a bad file.
file_size() {
  _f=$1; _lo=0; _hi=1
  while has_byte_at "$_f" "$_hi"; do
    _lo=$_hi; _hi=$(( _hi * 2 ))
    [ "$_hi" -gt 1073741824 ] && return 1        # 1 GiB: nothing here is remotely this big
  done
  while [ $(( _hi - _lo )) -gt 1 ]; do
    _mid=$(( (_lo + _hi) / 2 ))
    if has_byte_at "$_f" "$_mid"; then _lo=$_mid; else _hi=$_mid; fi
  done
  # _lo is the highest offset that yields a byte, so the file is _lo + 1 bytes long.
  has_byte_at "$_f" 0 || { echo 0; return 0; }
  echo $(( _lo + 1 ))
}

# Does file $1 have a byte at 0-indexed offset $2? Same primitive is_size() is built from.
has_byte_at() {
  dd if="$1" bs=1 skip="$2" count=1 of="$ENV_PROBE" 2>/dev/null
  if [ -s "$ENV_PROBE" ]; then rm -f "$ENV_PROBE"; return 0; fi
  rm -f "$ENV_PROBE"; return 1
}

# Write $1 -> $2 with dd, then hold dd to the record count its own source size implies.
# Retries once: the failure this guards against was transient on the box we saw it on.
DD_LOG=/flash/.dd_log.$$
write_verified() {
  _src=$1; _dst=$2; _what=$3
  _sz=$(file_size "$_src") || { log "  ERROR: cannot measure $_src -- refusing to write $_what"; return 1; }
  if [ "$_sz" = 0 ]; then log "  ERROR: $_src measured 0 bytes -- refusing to write $_what"; return 1; fi
  # dd's default block size is 512. A size that is not a multiple of 512 makes the last read
  # a partial one, which dd reports as "N+1 records out" rather than "N+0". Work out both the
  # expected record line and the expected byte line: busybox builds vary in which they print,
  # and demanding a line this dd never emits would fail every healthy update.
  _blocks=$(( _sz / 512 ))
  _rem=$(( _sz - _blocks * 512 ))
  if [ "$_rem" = 0 ]; then _want_rec="$_blocks+0 records out"; else _want_rec="$_blocks+1 records out"; fi
  _try=1
  while [ "$_try" -le 2 ]; do
    rm -f "$DD_LOG"
    dd if="$_src" of="$_dst" conv=fsync 2>"$DD_LOG"
    _rc=$?
    sync
    if [ "$_rc" = 0 ] && { grep -q "^$_want_rec" "$DD_LOG" || grep -q "^$_sz bytes" "$DD_LOG"; }; then
      log "  $_what: dd accounted for all $_sz bytes (attempt $_try)"
      rm -f "$DD_LOG"; return 0
    fi
    log "  WARNING: $_what write did not account for all $_sz bytes (attempt $_try, rc=$_rc):"
    while read -r _ln; do log "    dd: $_ln"; done < "$DD_LOG"
    _try=$(( _try + 1 ))
  done
  rm -f "$DD_LOG"
  return 1
}

if [ -f /flash/kernel.img ]; then
  # The updater has just rewritten this file. Commit it, then read it off the eMMC.
  sync
  echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
  log "writing /flash/kernel.img -> $BOOTP"
  if write_verified /flash/kernel.img "$BOOTDEV" "kernel"; then
    log "  kernel write accounted for (byte count only -- content is checked by the switcher app)"
  else
    log "  ERROR: writing kernel.img to $BOOTDEV FAILED or came up short -- CoreELEC may not boot."
    log "  ERROR: press 'Reboot to CoreELEC' on Android; the app re-checks and repairs this image."
    RC=1
  fi
fi
if [ -f /flash/dtb.img ]; then
  log "writing /flash/dtb.img -> $DTBOP (zero 128 KiB first)"
  dd if=/dev/zero of="$DTBODEV" bs=1024 count=128 2>/dev/null
  if write_verified /flash/dtb.img "$DTBODEV" "dtb"; then
    log "  dtb write accounted for"
  else
    log "  ERROR: writing dtb.img to $DTBODEV FAILED or came up short -- CoreELEC may not boot"
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
  if ! is_size /flash/env_dualboot.bin "$ENV_SIZE"; then
    log "ERROR: env_dualboot.bin is not exactly ${ENV_SIZE} B (truncated, corrupt or unreadable) --"
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
  fw_setenv -c "$FWCFG" bootcefromemmc "setenv bootargs \"\${bootargs} BOOT_IMAGE=kernel.img boot=LABEL=CE_FLASH disk=LABEL=CE_STORAGE console=tty0 no_console_suspend quiet vout=1080p60hz,dis frac_rate_policy=0 hdmitx= hdr_policy=1\"; setenv loadaddr \${loadaddr_kernel}; store read \${dtb_mem_addr} ${DTBOP} 0 0x20000; if imgread kernel ${BOOTP} \${loadaddr}; then bootm \${loadaddr}; fi"
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
