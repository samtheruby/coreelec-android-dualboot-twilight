#!/usr/bin/env python3
"""The partition geometry that gets written to the eMMC.

Dependency-free on purpose: run it directly.

    python tests/test_layout.py

layout.py turns a device's carve description into the LBA ranges stage1 writes into the
GPT. Everything here is arithmetic, and everything it can get wrong is destructive: a
partition that starts one sector early overlaps the one before it, and a carve that runs
past its end lands in whatever the vendor put after it.

The specific gap this closes: layout.py DOES assert that the partition sizes sum to the
carve total -- but only inside _print(), the human-facing dump. Nothing checks it on the
path stage1 actually takes. The invariant is real either way, so it is asserted here for
every registered device rather than only when someone runs the module to look at it.

Device-generic on purpose: a geometry rule that held for the stick and not the box would
be a bug in itself, so every check runs against both.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))

import devices  # noqa: E402
import layout  # noqa: E402

SEC_PER_MIB = devices.SEC_PER_MIB

_FAILURES = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        _FAILURES.append(f"{name}: {e}")
        print(f"  FAIL  {name}: {e}")
    except Exception as e:                                       # noqa: BLE001
        _FAILURES.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
    else:
        print(f"  ok    {name}")


def sizes_sum_to_the_carve():
    """The invariant layout.py only asserts inside _print()."""
    for dev in devices.DEVICES:
        total = sum(dev.sizes_mib.values())
        assert total == dev.carve_total_mib, (
            f"{dev.slug}: partition sizes sum to {total} MiB but the carve is "
            f"{dev.carve_total_mib} MiB -- the difference is either unallocated or overrun")


def partitions_are_contiguous_and_ordered():
    for dev in devices.DEVICES:
        parts = layout.partitions(dev)
        assert parts, f"{dev.slug}: no partitions"
        assert [p[0] for p in parts] == list(dev.order), (
            f"{dev.slug}: partition order {[p[0] for p in parts]} != declared {list(dev.order)}")
        assert parts[0][1] == dev.carve_start_mib, (
            f"{dev.slug}: first partition starts at {parts[0][1]} MiB, carve starts at "
            f"{dev.carve_start_mib}")
        for prev, cur in zip(parts, parts[1:]):
            assert cur[1] == prev[2], (
                f"{dev.slug}: gap or overlap between {prev[0]} (ends {prev[2]}) and "
                f"{cur[0]} (starts {cur[1]})")
        assert parts[-1][2] == dev.carve_end_mib, (
            f"{dev.slug}: last partition ends at {parts[-1][2]} MiB, carve ends at "
            f"{dev.carve_end_mib}")


def declared_sizes_match_the_computed_spans():
    for dev in devices.DEVICES:
        for name, start, end, size in layout.partitions(dev):
            assert end - start == size, (
                f"{dev.slug}/{name}: span {start}..{end} is {end - start} MiB but size "
                f"says {size}")
            assert dev.sizes_mib[name] == size, (
                f"{dev.slug}/{name}: sizes_mib says {dev.sizes_mib[name]}, layout says {size}")


def sectors_do_not_overlap():
    """GPT last_lba is INCLUSIVE. An off-by-one here overlaps the next partition by a
    sector, which is not visibly wrong until something writes there."""
    for dev in devices.DEVICES:
        secs = layout.as_sectors(dev)
        for name, start, end, count in secs:
            assert end >= start, f"{dev.slug}/{name}: end {end} before start {start}"
            assert count == end - start + 1, (
                f"{dev.slug}/{name}: count {count} != inclusive span {end - start + 1} "
                "(last_lba is inclusive)")
        for prev, cur in zip(secs, secs[1:]):
            assert cur[1] == prev[2] + 1, (
                f"{dev.slug}: {prev[0]} ends at LBA {prev[2]} and {cur[0]} starts at "
                f"{cur[1]} -- must be exactly one sector later")


def sectors_agree_with_the_mib_view():
    """Same regions, two units. A rounding difference between them would put the GPT and
    the human-readable plan out of step."""
    for dev in devices.DEVICES:
        for (n1, s_mib, e_mib, _), (n2, s_lba, e_lba, _) in zip(layout.partitions(dev),
                                                                layout.as_sectors(dev)):
            assert n1 == n2, f"{dev.slug}: partition order differs between the two views"
            assert s_lba == s_mib * SEC_PER_MIB, (
                f"{dev.slug}/{n1}: start {s_mib} MiB is LBA {s_mib * SEC_PER_MIB}, got {s_lba}")
            assert e_lba == e_mib * SEC_PER_MIB - 1, (
                f"{dev.slug}/{n1}: end {e_mib} MiB is inclusive LBA "
                f"{e_mib * SEC_PER_MIB - 1}, got {e_lba}")


def the_carve_fits_on_the_device():
    """Nothing may be written past the end of the eMMC, or into the GPT's own backup."""
    for dev in devices.DEVICES:
        last = layout.as_sectors(dev)[-1][2]
        assert last < dev.total_sectors, (
            f"{dev.slug}: carve ends at LBA {last} but the device has only "
            f"{dev.total_sectors} sectors")
        assert last < dev.gpt_backup_lba, (
            f"{dev.slug}: carve ends at LBA {last}, on or past the backup GPT at "
            f"{dev.gpt_backup_lba}")


def devices_do_not_share_a_layout():
    """stick and box are different sizes. Identical geometry would mean one of them is
    being described with the other's numbers."""
    a, b = devices.STICK, devices.BOX
    assert layout.as_sectors(a) != layout.as_sectors(b), (
        "stick and box produce identical sector layouts -- one is using the other's "
        "geometry")


def the_default_device_is_the_stick():
    """layout's module-level constants are the stick's, and callers pass dev=None for it."""
    assert layout.partitions() == layout.partitions(devices.STICK)
    assert layout.as_sectors() == layout.as_sectors(devices.STICK)
    assert layout.TOTAL_SECTORS == devices.STICK.total_sectors


if __name__ == "__main__":
    print("layout -- the partition geometry stage1 writes")
    check("partition sizes sum to the carve total", sizes_sum_to_the_carve)
    check("partitions are contiguous, ordered, and fill the carve", partitions_are_contiguous_and_ordered)
    check("declared sizes match the computed spans", declared_sizes_match_the_computed_spans)
    check("sector ranges do not overlap (last_lba inclusive)", sectors_do_not_overlap)
    check("the sector view agrees with the MiB view", sectors_agree_with_the_mib_view)
    check("the carve fits below total_sectors and the backup GPT", the_carve_fits_on_the_device)
    check("stick and box do not share a layout", devices_do_not_share_a_layout)
    check("the default device is the stick", the_default_device_is_the_stick)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all layout checks passed")
