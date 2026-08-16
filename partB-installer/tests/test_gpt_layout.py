#!/usr/bin/env python3
"""The GPT surgery that carves the CoreELEC partitions.

Dependency-free on purpose: run it directly.

    python tests/test_gpt_layout.py

build_gpt_layout.py edits the stock partition table: it writes two new entries and then
recomputes the two CRCs a GPT carries -- one over the entry array, one over the header. A
GPT whose CRCs do not match is not a subtly wrong partition table, it is a partition table
the bootloader rejects outright, on a box whose stock table has already been overwritten.

All of it is struct and crc32 arithmetic over bytes, so it is fully testable offline. The
committed refdata blobs are checked too: they are the INPUT this surgery starts from, and
if one were truncated or re-saved wrongly in the repo, every build after that would carve
from a broken table.
"""
import os
import struct
import sys
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "build"))

import build_gpt_layout as G  # noqa: E402
import devices  # noqa: E402

PRIMARY_HDR_OFF = 512          # LBA 1
PRIMARY_ARR_OFF = 1024         # LBA 2

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


def synthetic_gpt():
    """A minimal but structurally real GPT: header at LBA 1, entry array at LBA 2."""
    buf = bytearray(PRIMARY_ARR_OFF + G.ARR_BYTES)
    h = buf
    h[PRIMARY_HDR_OFF:PRIMARY_HDR_OFF + 8] = b"EFI PART"
    struct.pack_into("<Q", h, PRIMARY_HDR_OFF + 24, 1)          # my_lba
    struct.pack_into("<Q", h, PRIMARY_HDR_OFF + 32, 0x1000)     # alt_lba
    struct.pack_into("<Q", h, PRIMARY_HDR_OFF + 40, 34)         # first_usable
    struct.pack_into("<Q", h, PRIMARY_HDR_OFF + 48, 0xfff)      # last_usable
    struct.pack_into("<Q", h, PRIMARY_HDR_OFF + 72, 2)          # pe_lba
    struct.pack_into("<I", h, PRIMARY_HDR_OFF + 84, 128)        # esize
    G.rebuild_crcs(buf, PRIMARY_HDR_OFF, PRIMARY_ARR_OFF)
    return buf


# --- entries -------------------------------------------------------------------------------
def make_entry_is_exactly_one_slot():
    e = G.make_entry(uuid.UUID(G.L.GUID_LINUX_FS).bytes_le, G.UNIQUE_CE_STORAGE,
                     100, 199, 0, "CE_STORAGE")
    assert len(e) == 128, f"a GPT entry is 128 bytes, got {len(e)}"


def make_entry_round_trips_its_name():
    for name in ("CE_FLASH", "CE_STORAGE", "a"):
        e = G.make_entry(b"\x00" * 16, G.UNIQUE_CE_FLASH, 1, 2, 0, name)
        assert G.entry_name(e) == name, f"name {name!r} came back as {G.entry_name(e)!r}"


def make_entry_records_the_lbas_it_was_given():
    e = G.make_entry(b"\x00" * 16, G.UNIQUE_CE_FLASH, 2048, 4095, 0, "CE_FLASH")
    first = struct.unpack_from("<Q", e, 32)[0]
    last = struct.unpack_from("<Q", e, 40)[0]
    assert (first, last) == (2048, 4095), f"got first={first} last={last}"


def make_entry_refuses_an_oversized_name():
    """The name field is 72 bytes of UTF-16. Silently truncating it would produce a
    partition the installer then fails to find by name."""
    try:
        G.make_entry(b"\x00" * 16, G.UNIQUE_CE_FLASH, 1, 2, 0, "X" * 40)   # 80 bytes UTF-16
    except AssertionError:
        return
    raise AssertionError("accepted a name longer than the 72-byte field")


def the_two_new_partitions_have_distinct_guids():
    """Identical unique GUIDs on both new partitions would make them ambiguous to anything
    that addresses partitions by GUID."""
    assert G.UNIQUE_CE_FLASH != G.UNIQUE_CE_STORAGE


# --- CRCs ----------------------------------------------------------------------------------
def rebuild_crcs_produces_a_header_that_verifies():
    buf = synthetic_gpt()
    assert G.hdr_crc_ok(buf, PRIMARY_HDR_OFF), "freshly rebuilt header does not verify"


def rebuild_crcs_covers_the_entry_array():
    """Changing an entry must change the array CRC -- otherwise the carve is written and
    the table still claims the old contents."""
    buf = synthetic_gpt()
    before = G.parse_hdr(buf, PRIMARY_HDR_OFF)["arr_crc"]
    buf[PRIMARY_ARR_OFF:PRIMARY_ARR_OFF + 128] = G.make_entry(
        b"\x11" * 16, G.UNIQUE_CE_FLASH, 2048, 4095, 0, "CE_FLASH")
    G.rebuild_crcs(buf, PRIMARY_HDR_OFF, PRIMARY_ARR_OFF)
    after = G.parse_hdr(buf, PRIMARY_HDR_OFF)["arr_crc"]
    assert before != after, "array CRC unchanged after rewriting an entry"
    expect = zlib.crc32(bytes(buf[PRIMARY_ARR_OFF:PRIMARY_ARR_OFF + G.ARR_BYTES])) & 0xffffffff
    assert after == expect, "array CRC is not computed over the whole entry array"


def rebuild_crcs_sets_the_entry_count():
    """The installer verifies this same number; a stale count and the table disagree about
    how many entries there are."""
    buf = synthetic_gpt()
    assert G.parse_hdr(buf, PRIMARY_HDR_OFF)["num"] == G.NEW_NUM == devices.CARVED_NUM_ENTRIES


def a_tampered_header_fails_its_crc():
    buf = synthetic_gpt()
    buf[PRIMARY_HDR_OFF + 40] ^= 0xff          # first_usable, a covered field
    assert not G.hdr_crc_ok(buf, PRIMARY_HDR_OFF), "a tampered header still verified"


def the_header_crc_excludes_its_own_field():
    """The GPT spec zeroes the CRC field before computing it. Including it would make the
    header unverifiable by anything else -- including the bootloader."""
    buf = synthetic_gpt()
    h = bytearray(buf[PRIMARY_HDR_OFF:PRIMARY_HDR_OFF + 92])
    stored = struct.unpack_from("<I", h, 16)[0]
    struct.pack_into("<I", h, 16, 0)
    assert zlib.crc32(h) & 0xffffffff == stored, "header CRC is not computed with the field zeroed"


def parse_hdr_refuses_a_non_gpt_buffer():
    try:
        G.parse_hdr(bytearray(200), 0)
    except AssertionError:
        return
    raise AssertionError("parsed a buffer with no 'EFI PART' signature as a GPT header")


# --- the committed inputs -------------------------------------------------------------------
def the_committed_refdata_is_a_valid_gpt():
    """refdata/<slug>/stock_gpt_first2m.bin is what every build carves from. If one were
    truncated or re-saved wrongly in the repo, every build after that starts from a broken
    table -- and the failure would appear on a device, not here."""
    for dev in devices.DEVICES:
        p = os.path.join(ROOT, "refdata", dev.slug, "stock_gpt_first2m.bin")
        assert os.path.exists(p), f"missing {p}"
        buf = open(p, "rb").read()
        assert len(buf) >= PRIMARY_ARR_OFF + 128, f"{dev.slug}: only {len(buf)} bytes"
        assert buf[PRIMARY_HDR_OFF:PRIMARY_HDR_OFF + 8] == b"EFI PART", (
            f"{dev.slug}: no EFI PART signature at LBA 1")
        assert G.hdr_crc_ok(buf, PRIMARY_HDR_OFF), f"{dev.slug}: stock GPT header CRC is bad"
        h = G.parse_hdr(buf, PRIMARY_HDR_OFF)
        assert h["my_lba"] == 1, f"{dev.slug}: primary header says my_lba={h['my_lba']}"
        assert h["esize"] == 128, f"{dev.slug}: entry size {h['esize']}, expected 128"


if __name__ == "__main__":
    print("build_gpt_layout -- the partition table surgery")
    check("an entry is exactly 128 bytes", make_entry_is_exactly_one_slot)
    check("an entry round-trips its name", make_entry_round_trips_its_name)
    check("an entry records the LBAs it was given", make_entry_records_the_lbas_it_was_given)
    check("an oversized name is refused, not truncated", make_entry_refuses_an_oversized_name)
    check("the two new partitions have distinct GUIDs", the_two_new_partitions_have_distinct_guids)
    check("rebuild_crcs produces a header that verifies", rebuild_crcs_produces_a_header_that_verifies)
    check("rebuild_crcs covers the whole entry array", rebuild_crcs_covers_the_entry_array)
    check("rebuild_crcs sets the entry count", rebuild_crcs_sets_the_entry_count)
    check("a tampered header fails its CRC", a_tampered_header_fails_its_crc)
    check("the header CRC excludes its own field", the_header_crc_excludes_its_own_field)
    check("parse_hdr refuses a non-GPT buffer", parse_hdr_refuses_a_non_gpt_buffer)
    check("the committed refdata is a valid GPT (both devices)", the_committed_refdata_is_a_valid_gpt)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all gpt layout checks passed")
