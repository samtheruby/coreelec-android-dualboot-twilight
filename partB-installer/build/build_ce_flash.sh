#!/usr/bin/env bash
# Build ce_flash.img: 850 MiB FAT32 (label CE_FLASH) preloaded with the verified
# CoreELEC payload. Run under WSL (needs mkfs.vfat + mtools).
#
# Size MUST equal the GPT CE_FLASH partition (build/layout.py: 850 MiB) so the
# filesystem spans the whole partition when dd'd on.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLASH="$HERE/../payload/flash"
OUT="$HERE/../artifacts"
IMG="$OUT/ce_flash.img"
SIZE_MIB=$(python3 -c "import sys;sys.path.insert(0,'$HERE');import layout;print(layout.SIZES_MIB['CE_FLASH'])")   # from build/layout.py

mkdir -p "$OUT"
rm -f "$IMG"

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
