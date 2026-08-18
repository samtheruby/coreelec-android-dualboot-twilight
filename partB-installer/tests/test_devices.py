#!/usr/bin/env python3
"""Identity/discrimination tests for build/devices.py.

Dependency-free on purpose (devices.py is too): run it directly.

    python tests/test_devices.py

The bug these guard against: `stage_magisk` runs BEFORE root exists, and on the
stock Xiaomi firmware SELinux denies the non-root `shell` domain read access to
/sys/class/block/mmcblk0/size (AOSP grants `domain` only sysfs:dir search and
sysfs:lnk_file read -- no generic sysfs:file read). The reader therefore got an
empty string and died with IndexError instead of failing closed.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))

import devices                                                   # noqa: E402

STICK, BOX = devices.STICK, devices.BOX

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


def props_for(dev, **override):
    """A getprop callable that answers like the given device."""
    table = {
        "ro.product.model": dev.model,
        "ro.product.device": dev.codename,
        "ro.product.name": dev.product,
    }
    table.update(override)
    return lambda p: table.get(p, "")


def sectors_for(dev):
    return lambda: dev.total_sectors


def denied():
    """What sectors_reader() yields when SELinux denies the sysfs read: the
    'Permission denied' text goes to stderr, so stdout is empty."""
    return devices.sectors_reader(lambda cmd: "")


def expect_exit(fn, must_contain=None):
    try:
        fn()
    except SystemExit as e:
        msg = str(e)
        if must_contain and must_contain not in msg:
            raise AssertionError(f"SystemExit did not mention {must_contain!r}: {msg}")
        return
    raise AssertionError("expected SystemExit, none raised")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_products_differ():
    # ro.product.device is SHARED ('twilight'); ro.product.name is not. It is the
    # only variant signal besides the model that reads without root.
    assert STICK.product == "adastra", STICK.product
    assert BOX.product == "twilight", BOX.product
    assert STICK.product != BOX.product


def test_userdata_sizes_do_not_overlap():
    # The fastboot cross-check compares `partition-size:userdata` against these.
    # A unit is stock (pre-carve) or carved (post-install); both must be accepted,
    # and no stick value may collide with a box value.
    s, b = set(STICK.expected_userdata_bytes()), set(BOX.expected_userdata_bytes())
    assert not (s & b), f"stick/box userdata sizes overlap: {s & b}"
    MIB = devices.MIB
    assert STICK.carved_userdata_bytes == 2376 * MIB      # confirmed live on the stick
    assert STICK.stock_userdata_bytes == 4176 * MIB
    assert BOX.carved_userdata_bytes == 14800 * MIB
    assert BOX.stock_userdata_bytes == 26540 * MIB


# ---------------------------------------------------------------------------
# sectors_reader: must not explode on a denied read
# ---------------------------------------------------------------------------
def test_sectors_reader_returns_none_when_denied():
    # THE REGRESSION. Was: IndexError: list index out of range.
    assert denied()() is None


def test_sectors_reader_parses_a_normal_read():
    r = devices.sectors_reader(lambda cmd: "15269888\n")
    assert r() == 15269888


# ---------------------------------------------------------------------------
# identify: happy paths
# ---------------------------------------------------------------------------
def test_identify_stick():
    assert devices.identify(props_for(STICK), sectors_for(STICK)) is STICK


def test_identify_box():
    assert devices.identify(props_for(BOX), sectors_for(BOX)) is BOX


# ---------------------------------------------------------------------------
# identify: fail closed
# ---------------------------------------------------------------------------
def test_unknown_model_aborts():
    gp = props_for(STICK, **{"ro.product.model": "MiTV-NOPE"})
    expect_exit(lambda: devices.identify(gp, sectors_for(STICK)), "unrecognised model")


def test_codename_mismatch_aborts():
    gp = props_for(STICK, **{"ro.product.device": "notwilight"})
    expect_exit(lambda: devices.identify(gp, sectors_for(STICK)), "identity mismatch")


def test_product_mismatch_aborts():
    # A box's ro.product.name on a unit claiming to be the stick.
    gp = props_for(STICK, **{"ro.product.name": "twilight"})
    expect_exit(lambda: devices.identify(gp, sectors_for(STICK)), "identity mismatch")


def test_wrong_geometry_aborts():
    # Box-sized eMMC but the stick's model: the exact brick this guard exists for.
    expect_exit(lambda: devices.identify(props_for(STICK), sectors_for(BOX)),
                "WRONG DEVICE/GEOMETRY")


def test_denied_sectors_aborts_when_not_allowed():
    # Root-side callers keep the hard fail -- but as a clean SystemExit, not a traceback.
    expect_exit(lambda: devices.identify(props_for(STICK), denied()),
                "could not read mmcblk0 sector count")


# ---------------------------------------------------------------------------
# identify: read_sectors=None (stage_magisk, pre-root)
# ---------------------------------------------------------------------------
# The size read is not merely unreliable before root -- it is IMPOSSIBLE (SELinux),
# so stage_magisk does not attempt it. It passes no reader, and identity rests on
# model + codename + product. The bootloader re-verifies the unit before any write.
def test_no_reader_skips_the_size_check():
    assert devices.identify(props_for(STICK), None) is STICK
    assert devices.identify(props_for(BOX), None) is BOX


def test_no_reader_still_rejects_a_bad_model():
    gp = props_for(BOX, **{"ro.product.model": "MiTV-NOPE"})
    expect_exit(lambda: devices.identify(gp, None), "unrecognised model")


def test_no_reader_still_rejects_a_bad_product():
    gp = props_for(BOX, **{"ro.product.name": "adastra"})
    expect_exit(lambda: devices.identify(gp, None), "identity mismatch")


def test_no_reader_says_so_in_the_log():
    lines = []
    devices.identify(props_for(BOX), None, log=lines.append)
    assert any("eMMC size not checked" in l for l in lines), lines


# ---------------------------------------------------------------------------
# fastboot cross-check helper
# ---------------------------------------------------------------------------
def test_userdata_size_accepts_stock_and_carved():
    assert STICK.userdata_size_ok(STICK.stock_userdata_bytes)
    assert STICK.userdata_size_ok(STICK.carved_userdata_bytes)
    assert BOX.userdata_size_ok(BOX.stock_userdata_bytes)
    assert BOX.userdata_size_ok(BOX.carved_userdata_bytes)


def test_userdata_size_rejects_the_other_device():
    # The whole point: a box in fastboot must not pass as a stick.
    assert not STICK.userdata_size_ok(BOX.stock_userdata_bytes)
    assert not STICK.userdata_size_ok(BOX.carved_userdata_bytes)
    assert not BOX.userdata_size_ok(STICK.stock_userdata_bytes)
    assert not BOX.userdata_size_ok(STICK.carved_userdata_bytes)


def test_envgate_kt_mirrors_the_registry():
    """The Android app's EnvGate.kt re-asserts the boot gate on-device, and guards that
    write with its own copy of model -> eMMC sectors (it cannot import this module). That
    copy is the fail-closed check standing between a wrong unit and a bootloader env
    write, so it must not drift from the registry it mirrors."""
    kt = os.path.join(HERE, "..", "app", "RebootToCoreELEC", "app", "src", "main", "java",
                      "com", "jamal2367", "coreelec", "EnvGate.kt")
    src = open(kt, encoding="utf-8").read()
    m = re.search(r"KNOWN_DEVICES\s*=\s*mapOf\((.*?)\n\s*\)", src, re.S)
    assert m, "EnvGate.kt no longer declares KNOWN_DEVICES"
    got = {k: int(v.replace("_", ""))
           for k, v in re.findall(r'"([^"]+)"\s+to\s+([0-9_]+)L', m.group(1))}
    want = {d.model: d.total_sectors for d in devices.DEVICES}
    assert got == want, (
        f"EnvGate.kt KNOWN_DEVICES has drifted from the registry:\n"
        f"    EnvGate.kt: {got}\n    devices.py: {want}")


def test_parse_fastboot_getvar_size():
    # Real output, captured from the stick's bootloader.
    raw = "partition-size:userdata: 0x0000000094800000\nFinished. Total time: 0.001s\n"
    assert devices.parse_fastboot_size(raw) == 2376 * devices.MIB
    assert devices.parse_fastboot_size("FAILED (remote: 'invalid partition')") is None
    assert devices.parse_fastboot_size("") is None


if __name__ == "__main__":
    print("== devices.py identity tests ==")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            check(name, fn)
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)})")
        sys.exit(1)
    print("all passed")
