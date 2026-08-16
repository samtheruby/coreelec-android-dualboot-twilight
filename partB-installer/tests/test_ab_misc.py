#!/usr/bin/env python3
"""The A/B bootloader_control edit that keeps Android off the CoreELEC slot.

Dependency-free on purpose: run it directly.

    python tests/test_ab_misc.py

ab_misc.py rewrites the 32-byte AOSP bootloader_control struct in `misc` so the CE slot's
priority is 0 -- unbootable -- and Android's A/B rollback never auto-boots our CoreELEC
kernel from it. Then it fixes the CRC, because the bootloader validates the struct and a
bad CRC is not a no-op.

Everything here is pure struct/crc32 arithmetic over 32 bytes, so it is fully testable
offline -- and worth testing, because the ways it can go wrong are all quiet. Zero the
wrong byte and the ANDROID slot becomes unbootable. Compute the CRC over the wrong span
and the bootloader sees a corrupt control struct on a box that was working a moment ago.
Neither shows up as an error at the time it is written.
"""
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))

import ab_misc  # noqa: E402

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


def make(a_byte=0x7f, b_byte=0x7f, magic=b"BCAB", suffix=b"_a\x00\x00", fix_crc=True):
    """A believable 32-byte bootloader_control, per the layout ab_misc documents."""
    buf = bytearray(32)
    buf[0:4] = suffix
    buf[4:8] = magic
    buf[8] = 1              # version
    buf[9] = 2              # nb_slot
    buf[12] = a_byte        # slot_metadata[0]: priority:4 | tries:3 | successful:1
    buf[14] = b_byte        # slot_metadata[1]
    if fix_crc:
        struct.pack_into("<I", buf, 28, zlib.crc32(bytes(buf[0:28])) & 0xffffffff)
    return bytes(buf)


def parse_reports_a_good_crc():
    info = ab_misc.parse(make())
    assert info["crc_ok"], f"a freshly built struct should verify: {info}"
    assert info["magic"] == b"BCAB", info["magic"]
    assert info["stored_crc"] == info["calc_crc"]


def parse_reports_a_bad_crc_rather_than_raising():
    """A corrupt struct must be reportable -- callers decide what to do, parse does not
    get to refuse to look at it."""
    bad = bytearray(make())
    bad[12] ^= 0xff                       # change a covered byte, leave the stored crc
    info = ab_misc.parse(bytes(bad))
    assert not info["crc_ok"], "a tampered struct should not verify"
    assert info["stored_crc"] != info["calc_crc"]


def crc_covers_bytes_0_to_28():
    """The CRC is over [0:28] only -- it must not include its own 4 bytes."""
    data = make()
    expect = zlib.crc32(data[0:28]) & 0xffffffff
    assert ab_misc.parse(data)["calc_crc"] == expect, "crc is not computed over [0:28]"
    # Changing only the stored crc field must not change the calculated one.
    other = bytearray(data)
    struct.pack_into("<I", other, 28, 0xdeadbeef)
    assert ab_misc.parse(bytes(other))["calc_crc"] == expect, "crc span includes itself"


def parse_rejects_a_wrong_length():
    for n in (0, 31, 33, 64):
        try:
            ab_misc.parse(b"\x00" * n)
        except AssertionError:
            continue
        raise AssertionError(f"parse accepted {n} bytes; it must require exactly 32")


def marking_a_zeroes_only_a():
    out = ab_misc.mark_unbootable(make(a_byte=0x7f, b_byte=0x33), "_a")
    info = ab_misc.parse(out)
    assert info["a_byte"] == 0x00, f"_a priority should be 0, got {info['a_byte']:#04x}"
    assert info["b_byte"] == 0x33, f"_b must be untouched, got {info['b_byte']:#04x}"


def marking_b_zeroes_only_b():
    out = ab_misc.mark_unbootable(make(a_byte=0x33, b_byte=0x7f), "_b")
    info = ab_misc.parse(out)
    assert info["b_byte"] == 0x00, f"_b priority should be 0, got {info['b_byte']:#04x}"
    assert info["a_byte"] == 0x33, f"_a must be untouched, got {info['a_byte']:#04x}"


def result_verifies_against_the_bootloader():
    """The edited struct must carry a CRC the bootloader will accept -- an edit that leaves
    a stale CRC is worse than no edit at all."""
    for slot in ("_a", "_b"):
        out = ab_misc.mark_unbootable(make(), slot)
        assert ab_misc.parse(out)["crc_ok"], f"crc not fixed after marking {slot}"


def nothing_else_in_the_struct_moves():
    """Only the target priority byte and the CRC field may differ."""
    src = make(a_byte=0x7f, b_byte=0x41, suffix=b"_b\x00\x00")
    out = ab_misc.mark_unbootable(src, "_a")
    assert len(out) == 32, f"length changed: {len(out)}"
    differing = {i for i in range(32) if src[i] != out[i]}
    allowed = {12, 28, 29, 30, 31}
    assert differing <= allowed, (
        f"bytes changed outside the priority byte and crc: {sorted(differing - allowed)}")


def refuses_to_edit_a_corrupt_struct():
    bad = bytearray(make())
    bad[12] ^= 0xff                       # crc no longer matches
    try:
        ab_misc.mark_unbootable(bytes(bad), "_a")
    except ValueError as e:
        assert "crc" in str(e).lower(), f"wrong error: {e}"
        return
    raise AssertionError("edited a struct whose crc did not verify -- it must refuse")


def refuses_unexpected_magic():
    try:
        ab_misc.mark_unbootable(make(magic=b"XXXX"), "_a")
    except ValueError as e:
        assert "magic" in str(e).lower(), f"wrong error: {e}"
        return
    raise AssertionError("edited a struct that is not bootloader_control -- it must refuse")


def refuses_an_unknown_slot():
    for slot in ("_c", "a", "", "0"):
        try:
            ab_misc.mark_unbootable(make(), slot)
        except (AssertionError, KeyError, ValueError):
            continue
        raise AssertionError(f"accepted slot {slot!r}; only _a and _b exist")


def marking_is_idempotent():
    """stage1 can be re-run. Marking an already-unbootable slot must be a no-op, not a
    second edit that lands somewhere else."""
    once = ab_misc.mark_unbootable(make(), "_a")
    twice = ab_misc.mark_unbootable(once, "_a")
    assert once == twice, "marking the same slot twice produced a different struct"


if __name__ == "__main__":
    print("ab_misc -- the 32-byte bootloader_control edit")
    check("parse verifies a good crc", parse_reports_a_good_crc)
    check("parse reports a bad crc instead of raising", parse_reports_a_bad_crc_rather_than_raising)
    check("crc covers bytes [0:28] and not itself", crc_covers_bytes_0_to_28)
    check("parse rejects anything that is not 32 bytes", parse_rejects_a_wrong_length)
    check("marking _a zeroes _a only", marking_a_zeroes_only_a)
    check("marking _b zeroes _b only", marking_b_zeroes_only_b)
    check("the edited struct verifies", result_verifies_against_the_bootloader)
    check("no byte outside the priority + crc changes", nothing_else_in_the_struct_moves)
    check("refuses to edit a corrupt struct", refuses_to_edit_a_corrupt_struct)
    check("refuses unexpected magic", refuses_unexpected_magic)
    check("refuses an unknown slot", refuses_an_unknown_slot)
    check("marking twice is idempotent", marking_is_idempotent)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all ab_misc checks passed")
