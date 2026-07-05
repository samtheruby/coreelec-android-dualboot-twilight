#!/usr/bin/env python3
"""
Partition-layout geometry for the CoreELEC internal dual-boot.

The per-device geometry now lives in build/devices.py (the single registry that
also drives stick/box discrimination). This module is the thin computation layer:
given a Device, it yields the carved partition table, and it re-exports the
device-generic constants (sector size, type GUIDs).

Back-compat: every function defaults to the STICK device, and the old module-level
constants (TOTAL_SECTORS, CARVE_*, SIZES_MIB, ORDER) still resolve to the stick's
values, so existing stick callers keep working unchanged. New/box-aware callers
pass an explicit device: `layout.as_sectors(dev)`.

We carve the original userdata span into 3 partitions. Partition NUMBER is
irrelevant for boot (CoreELEC finds CE_FLASH / CE_STORAGE by LABEL via Linux);
only userdata's name matters so Android reformats it by-name on first boot.
"""
import json
import devices
from devices import SECTOR, MIB, SEC_PER_MIB, STICK, BOX  # noqa: F401 (re-export)

# --- GPT partition type GUIDs (mixed-endian, on-disk byte form) --------------
# userdata keeps its ORIGINAL type+unique GUID (copied from the live GPT at
# build time) so Android keeps recognising it. CE_* use Linux filesystem data.
GUID_LINUX_FS = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"   # CE_STORAGE (ext4)
GUID_MS_BASIC = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"   # CE_FLASH (FAT32 / basic data)

# --- back-compat aliases: default (stick) geometry as module-level names ------
TOTAL_SECTORS = STICK.total_sectors
CARVE_START_MIB = STICK.carve_start_mib
CARVE_END_MIB = STICK.carve_end_mib
CARVE_TOTAL_MIB = STICK.carve_total_mib
SIZES_MIB = STICK.sizes_mib
ORDER = STICK.order


def _dev(dev):
    return dev if dev is not None else STICK


def partitions(dev=None):
    """(name, start_mib, end_mib, size_mib) in layout order, for `dev` (default stick)."""
    return _dev(dev).partitions()


def as_sectors(dev=None):
    """(name, start_lba, end_lba_inclusive, count_sectors) for `dev` (default stick)."""
    return _dev(dev).as_sectors()


def dump_json(dev=None):
    d = _dev(dev)
    return json.dumps({
        "device": d.slug,
        "model": d.model,
        "sector": SECTOR,
        "total_sectors": d.total_sectors,
        "carve_start_mib": d.carve_start_mib,
        "carve_end_mib": d.carve_end_mib,
        "sizes_mib": d.sizes_mib,
        "order": d.order,
        "gpt_backup_lba": d.gpt_backup_lba,
        "stock_ud_last_lba": d.stock_ud_last_lba,
        "sectors": [
            {"name": n, "start_lba": s, "end_lba": e, "count": c}
            for n, s, e, c in d.as_sectors()
        ],
    }, indent=2)


def _print(dev):
    d = _dev(dev)
    assert sum(d.sizes_mib.values()) == d.carve_total_mib, \
        f"sizes sum {sum(d.sizes_mib.values())} != carve {d.carve_total_mib}"
    print(f"[{d.slug}] {d.name}  eMMC={d.total_sectors:,} sectors")
    print(f"carve region: {d.carve_start_mib}..{d.carve_end_mib} MiB ({d.carve_total_mib} MiB)")
    print(f"{'name':<12} {'start_mib':>9} {'end_mib':>8} {'size_mib':>8}  "
          f"{'start_lba':>10} {'end_lba':>10} {'sectors':>9}")
    secs = {n: (s, e, c) for n, s, e, c in d.as_sectors()}
    for name, s_mib, e_mib, size in d.partitions():
        s, e, c = secs[name]
        print(f"{name:<12} {s_mib:>9} {e_mib:>8} {size:>8}  {s:>10} {e:>10} {c:>9}")
    print("layout valid (sums to carve region).\n")


if __name__ == "__main__":
    import sys
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if slug:
        _print(devices.BY_SLUG[slug])
    else:
        for d in devices.DEVICES:
            _print(d)
