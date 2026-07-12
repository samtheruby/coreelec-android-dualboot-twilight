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
mkfs.vfat -F 32 -n CE_FLASH "$IMG" >/dev/null

# files that CoreELEC needs to boot internally + our hooks (exclude build cruft:
# cfgload.orig, fs-resize.log, dtb.xml, and Android media dirs).
FILES=(SYSTEM SYSTEM.md5 kernel.img kernel.img.md5 dtb.img recovery.img dovi.ko
       cfgload cfgload_env aml_autoscript config.ini resolution.ini user-update.sh)

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

echo "== verify: cfgload uses LABEL=CE_STORAGE =="
mcopy -i "$IMG" "::cfgload" /tmp/_cfg
if grep -q 'disk=LABEL=CE_STORAGE' /tmp/_cfg; then echo "   OK LABEL=CE_STORAGE"; else echo "   !! cfgload missing LABEL=CE_STORAGE"; fi
rm -f /tmp/_cfg

sz=$(stat -c %s "$IMG")
echo
echo "ce_flash.img built: $sz B ($((sz/1024/1024)) MiB)  sha256=$(sha256sum "$IMG" | cut -c1-16)"

# ce_flash.img.gz -- the form the installer actually streams (gunzip | dd on-device).
# Written here, from THIS raw, so raw and gz are always the same generation. Atomic mv:
# an interrupted gzip leaves the .tmp, never a truncated .gz that still looks flashable.
echo "== gzip -> ce_flash.img.gz =="
gzip -6 -c "$IMG" > "$IMG.gz.tmp" && mv -f "$IMG.gz.tmp" "$IMG.gz"
echo "ce_flash.img.gz:    $(stat -c %s "$IMG.gz") B"
