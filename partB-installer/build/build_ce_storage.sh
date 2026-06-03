#!/usr/bin/env bash
# Build ce_storage.img: empty ext4 (label CE_STORAGE), CoreELEC /storage.
# Run under WSL (needs mke2fs). Size from build/layout.py. Journal ON for
# power-loss resilience on a TV box. Reserved blocks 0 (-m 0): this is a pure
# data partition and CoreELEC runs as root (the 5% root-reserve would just be
# wasted in df without helping anything), so give all non-metadata space to use.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../artifacts"
IMG="$OUT/ce_storage.img"
SIZE_MIB=$(python3 -c "import sys;sys.path.insert(0,'$HERE');import layout;print(layout.SIZES_MIB['CE_STORAGE'])")   # from build/layout.py

mkdir -p "$OUT"; rm -f "$IMG"
truncate -s "${SIZE_MIB}M" "$IMG"
mke2fs -q -F -t ext4 -m 0 -L CE_STORAGE "$IMG"
echo "ce_storage.img: $(stat -c %s "$IMG") B ($SIZE_MIB MiB)"
dumpe2fs -h "$IMG" 2>/dev/null | grep -E "Volume name|Block count|Filesystem features" | sed 's/^/  /'
echo "  sha256=$(sha256sum "$IMG" | cut -c1-16)"
