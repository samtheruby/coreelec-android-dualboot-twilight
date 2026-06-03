#!/usr/bin/env python3
"""
Single source of truth for the twilight (Amlogic s7d / S905X5M) internal
dual-boot partition layout. Every PC-side builder AND the on-device installer
read their geometry from here (the installer imports the JSON dump).

Device facts (verified, see CoreELEC-internal-dualboot-twilight.md):
  eMMC mmcblk0 = 15,269,888 sectors x 512 B = 7.28 GiB
  GPT, 32 entries originally -> expanded non-destructively to 128.
  userdata (orig p32) span = sectors 6,713,344 .. 15,265,791
                            = 3278 MiB .. 7454 MiB  (4176 MiB carve region)

We carve that one span into 3 partitions. Partition NUMBER is irrelevant for
boot (CoreELEC finds CE_FLASH / CE_STORAGE by LABEL via Linux); only userdata's
name matters so Android reformats it by-name on first boot.
"""
import json

SECTOR = 512
TOTAL_SECTORS = 15_269_888              # whole eMMC
MIB = 1024 * 1024
SEC_PER_MIB = MIB // SECTOR             # 2048

# --- carve region (the original userdata span) -------------------------------
CARVE_START_MIB = 3278                  # = sector 6_713_344
CARVE_END_MIB   = 7454                  # = sector 15_265_792 (== last_usable+1)
CARVE_TOTAL_MIB = CARVE_END_MIB - CARVE_START_MIB   # 4176

# --- the rebalanced layout (Android bigger; see doc B2) ----------------------
# Sizes MUST sum to CARVE_TOTAL_MIB (4176). userdata up ~550M vs first build.
SIZES_MIB = {
    "userdata":   2376,   # Android user storage
    "CE_FLASH":    600,   # FAT32: kernel.img + SYSTEM(347M) + recovery/dtb/dovi (~381M used);
                          #        update rm's old SYSTEM then dd's new in place (1x, not 2x),
                          #        so 600 leaves ~200M growth margin (verified in CE initramfs).
    "CE_STORAGE": 1200,   # ext4: CoreELEC /storage. Update extracts the WHOLE tar to
                          #       /storage/.update/.tmp while the tar still exists -> ~770-790M
                          #       transient peak; +<200M user data fits in 1200 (~180M margin).
}
# Order in which they are laid out across the carve region (low -> high MiB).
ORDER = ["userdata", "CE_FLASH", "CE_STORAGE"]

# --- GPT partition type GUIDs (mixed-endian, on-disk byte form) --------------
# userdata keeps its ORIGINAL type+unique GUID (copied from the live GPT at
# build time) so Android keeps recognising it. CE_* use Linux filesystem data.
GUID_LINUX_FS = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"   # CE_STORAGE (ext4)
GUID_MS_BASIC = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"   # CE_FLASH (FAT32 / basic data)


def partitions():
    """Yield (name, start_mib, end_mib, size_mib) in layout order."""
    cur = CARVE_START_MIB
    out = []
    for name in ORDER:
        size = SIZES_MIB[name]
        out.append((name, cur, cur + size, size))
        cur += size
    assert cur == CARVE_END_MIB, f"layout sums to {cur} MiB, expected {CARVE_END_MIB}"
    return out


def as_sectors():
    """Same, but (name, start_lba, end_lba_inclusive, count_sectors)."""
    out = []
    for name, s_mib, e_mib, _ in partitions():
        start = s_mib * SEC_PER_MIB
        end   = e_mib * SEC_PER_MIB - 1          # GPT last_lba is inclusive
        out.append((name, start, end, end - start + 1))
    return out


def dump_json():
    return json.dumps({
        "sector": SECTOR,
        "total_sectors": TOTAL_SECTORS,
        "carve_start_mib": CARVE_START_MIB,
        "carve_end_mib": CARVE_END_MIB,
        "sizes_mib": SIZES_MIB,
        "order": ORDER,
        "sectors": [
            {"name": n, "start_lba": s, "end_lba": e, "count": c}
            for n, s, e, c in as_sectors()
        ],
    }, indent=2)


if __name__ == "__main__":
    assert sum(SIZES_MIB.values()) == CARVE_TOTAL_MIB, \
        f"sizes sum {sum(SIZES_MIB.values())} != carve {CARVE_TOTAL_MIB}"
    print(f"carve region: {CARVE_START_MIB}..{CARVE_END_MIB} MiB ({CARVE_TOTAL_MIB} MiB)")
    print(f"{'name':<12} {'start_mib':>9} {'end_mib':>8} {'size_mib':>8}  "
          f"{'start_lba':>10} {'end_lba':>10} {'sectors':>9}")
    secs = dict((n, (s, e, c)) for n, s, e, c in as_sectors())
    for name, s_mib, e_mib, size in partitions():
        s, e, c = secs[name]
        print(f"{name:<12} {s_mib:>9} {e_mib:>8} {size:>8}  {s:>10} {e:>10} {c:>9}")
    print("\nlayout valid (sums to carve region).")
