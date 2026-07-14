#!/usr/bin/env bash
# Build ce_flash.img: FAT32 (label CE_FLASH) preloaded with the verified CoreELEC
# payload. Run under WSL (needs mkfs.vfat + mtools).
#
# Usage: build_ce_flash.sh [device_slug]   (default: stick)
#
# The payload (payload/flash/*) is shared across devices (same SoC + CoreELEC
# build); only the FAT SIZE differs per device. Size MUST equal that device's GPT
# CE_FLASH partition (build/devices.py) so the filesystem spans the whole partition
# when dd'd on. Output -> artifacts/<slug>/ce_flash.img.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEV="${1:-stick}"
FLASH="$HERE/../payload/flash"
OUT="$HERE/../artifacts/$DEV"
IMG="$OUT/ce_flash.img"
SIZE_MIB=$(python3 -c "import sys;sys.path.insert(0,'$HERE');import devices;print(devices.BY_SLUG['$DEV'].sizes_mib['CE_FLASH'])")   # from build/devices.py

echo "== [$DEV] ce_flash: ${SIZE_MIB} MiB FAT32 -> artifacts/$DEV/ =="
mkdir -p "$OUT"
# $IMG.gz is DERIVED from $IMG (see the gzip step at the end). Drop it BEFORE building
# the new raw so a crashed build can never leave a gz from the previous CoreELEC payload
# sitting next to a fresh raw -- the installer streams the gz, so that pair flashes the
# old build while verifying the new one.
rm -f "$IMG" "$IMG.gz"

echo "== create ${SIZE_MIB} MiB image + mkfs.vfat (FAT32, label CE_FLASH) =="
dd if=/dev/zero of="$IMG" bs=1M count="$SIZE_MIB" status=none
# -i pins the FAT volume ID, which mkfs.vfat otherwise derives from the clock.
# NOTE: this does NOT make ce_flash.img byte-reproducible -- mcopy still stamps each
# directory entry's creation time from the clock, so two builds of the same payload differ
# by a couple of bytes (measured: 4 bytes, or 2 with SOURCE_DATE_EPOCH set; mtools 4.0.43
# has no way to pin the rest). Do not use this image's sha256 to decide whether the payload
# changed -- hash payload/flash/ for that. Unlike ce_storage.img (see build_ce_storage.sh,
# which IS reproducible) this image is gitignored, so the leftover nondeterminism costs
# nothing; the fixed volume ID is just one less thing that moves for no reason.
mkfs.vfat -F 32 -n CE_FLASH -i 43450001 "$IMG" >/dev/null   # 'CE' 00 01

# files that CoreELEC needs to boot internally + our hooks (exclude build cruft:
# cfgload.orig, fs-resize.log, dtb.xml, and Android media dirs).
FILES=(SYSTEM SYSTEM.md5 kernel.img kernel.img.md5 dtb.img recovery.img dovi.ko
       cfgload cfgload_env aml_autoscript config.ini resolution.ini user-update.sh)

# dovi.ko does not come from the CoreELEC image -- it is copied into payload/flash by hand
# and carried across every payload swap, and payload/flash is gitignored, so a stale module
# leaves no trace in the diff and ships. v1.2.8 went out with 5.15_2.6_am9_dovi_felfix.ko
# that way, when the current module is 5.15_2.6_dovi_patched_fix_fel.ko. Pin it: bump this
# only when the module is deliberately updated.
DOVI_SHA256=f6c26659a255447685ceac9441e399c999b1fae9c6435c48d70e14a14dd7f8f7

export MTOOLS_SKIP_CHECK=1
echo "== mcopy payload =="
for f in "${FILES[@]}"; do
  if [ -e "$FLASH/$f" ]; then
    mcopy -i "$IMG" "$FLASH/$f" "::$f"
    echo "  + $f"
  else
    echo "  ! MISSING $f (skipped)"
  fi
done
# device_trees/ directory
mcopy -i "$IMG" -s "$FLASH/device_trees" "::device_trees"
echo "  + device_trees/"

echo "== verify: directory listing =="
mdir -i "$IMG" :: | sed 's/^/   /'

echo "== verify: SYSTEM md5 survives the FS roundtrip =="
mcopy -i "$IMG" "::SYSTEM" /tmp/_sys_check
calc=$(md5sum /tmp/_sys_check | cut -d' ' -f1)
stored=$(cut -d' ' -f1 "$FLASH/SYSTEM.md5")
rm -f /tmp/_sys_check
echo "   stored=$stored calc=$calc  $( [ "$calc" = "$stored" ] && echo MATCH || echo MISMATCH )"
[ "$calc" = "$stored" ] || { echo "SYSTEM md5 mismatch -- abort"; exit 1; }

# This used to grep cfgload for `disk=LABEL=CE_STORAGE`. That check was wrong twice over:
# the internal boot path never executes cfgload (the bootcefromemmc env gate sets bootargs
# inline and goes straight to `imgread kernel boot_X`), and upstream has since moved
# cfgload's eMMC branch to `disk=FOLDER=/dev/CE_STORAGE` -- so it warned on a string we
# don't need, in a file we don't run. Check the files the boot path DOES depend on instead.
#
# What actually has to be in this FAT:
#   SYSTEM     the squashfs the CoreELEC initramfs mounts (boot=LABEL=CE_FLASH). md5 above.
#   kernel.img + dtb.img
#              NOT read from here at boot (u-boot pulls those from boot_X/dtbo_X), but
#              user-update.sh reflashes those partitions FROM THESE FILES after a CoreELEC
#              OS update. If they are missing or corrupt here, the next OTA silently leaves
#              the slots stale and the box boots the old kernel against the new SYSTEM.
#   dovi.ko / resolution.ini / user-update.sh
#              our additions -- absent from the generic CoreELEC image, so they only exist
#              here if they were carried over from the previous payload. Easy to lose in a
#              payload swap; a missing one is silent until Dolby Vision or a reboot breaks.
echo "== verify: kernel.img md5 survives the FS roundtrip =="
mcopy -i "$IMG" "::kernel.img" /tmp/_kern_check
kcalc=$(md5sum /tmp/_kern_check | cut -d' ' -f1)
kstored=$(cut -d' ' -f1 "$FLASH/kernel.img.md5")
rm -f /tmp/_kern_check
echo "   stored=$kstored calc=$kcalc  $( [ "$kcalc" = "$kstored" ] && echo MATCH || echo MISMATCH )"
[ "$kcalc" = "$kstored" ] || { echo "kernel.img md5 mismatch -- abort"; exit 1; }

echo "== verify: dtb.img is an FDT and fits the 128 KiB dtbo span =="
mcopy -i "$IMG" "::dtb.img" /tmp/_dtb_check
dmagic=$(od -A n -t x1 -N 4 /tmp/_dtb_check | tr -d ' \n')
dsize=$(stat -c %s /tmp/_dtb_check)
rm -f /tmp/_dtb_check
[ "$dmagic" = "d00dfeed" ] || { echo "   !! dtb.img magic=$dmagic (not an FDT) -- abort"; exit 1; }
[ "$dsize" -le 131072 ] || { echo "   !! dtb.img $dsize B > 128 KiB dtbo span -- abort"; exit 1; }
echo "   OK FDT magic d00dfeed, $dsize B (fits 131072 B span)"

echo "== verify: our additions survived the payload swap =="
missing=0
for f in dovi.ko resolution.ini user-update.sh; do
  if mdir -i "$IMG" "::$f" >/dev/null 2>&1; then
    echo "   OK $f"
  else
    echo "   !! MISSING $f -- not in the generic CoreELEC image; carry it over from the previous payload"
    missing=1
  fi
done
[ "$missing" -eq 0 ] || { echo "required additions missing from payload -- abort"; exit 1; }

# Present is not enough for dovi.ko -- the wrong module is just as silent as no module, and
# it only shows up as Dolby Vision misbehaving on the box. Hash the one that came back OUT
# of the FAT, so this covers a stale payload file AND a bad copy in.
echo "== verify: dovi.ko is the pinned module =="
mcopy -i "$IMG" "::dovi.ko" /tmp/_dovi_check
dcalc=$(sha256sum /tmp/_dovi_check | cut -d' ' -f1)
rm -f /tmp/_dovi_check
echo "   pinned=$DOVI_SHA256"
echo "   in img=$dcalc  $( [ "$dcalc" = "$DOVI_SHA256" ] && echo MATCH || echo MISMATCH )"
[ "$dcalc" = "$DOVI_SHA256" ] || {
  echo "   !! dovi.ko is not the pinned module -- payload/flash/dovi.ko is stale, or the"
  echo "      module was updated on purpose (then bump DOVI_SHA256 at the top of this script)"
  exit 1; }

sz=$(stat -c %s "$IMG")
echo
echo "ce_flash.img built: $sz B ($((sz/1024/1024)) MiB)  sha256=$(sha256sum "$IMG" | cut -c1-16)"

# ce_flash.img.gz -- the form the installer actually streams (gunzip | dd on-device).
# Written here, from THIS raw, so raw and gz are always the same generation. Atomic mv:
# an interrupted gzip leaves the .tmp, never a truncated .gz that still looks flashable.
echo "== gzip -> ce_flash.img.gz =="
# -n: no source filename, no timestamp in the gzip header -- see build_ce_storage.sh. This
# image is not byte-reproducible anyway (mtools stamps each directory entry from the clock),
# so -n does not make the .gz stable here; it is set for consistency, and so that the only
# thing that ever moves is the payload.
gzip -n -6 -c "$IMG" > "$IMG.gz.tmp" && mv -f "$IMG.gz.tmp" "$IMG.gz"
echo "ce_flash.img.gz:    $(stat -c %s "$IMG.gz") B"
