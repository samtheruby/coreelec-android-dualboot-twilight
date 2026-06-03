#!/usr/bin/env python3
"""
A/B bootloader_control editing for the CE slot.

The 32-byte AOSP bootloader_control struct lives in the `misc` partition at
offset 0x800. Verified on twilight (probe):
  [0:4]   slot_suffix
  [4:8]   magic 'BCAB'
  [8]     version   [9] nb_slot   ...
  [12]    slot_metadata[0] (_a):  priority:4 | tries:3 | successful:1
  [14]    slot_metadata[1] (_b):  same packing
  [28:32] crc32 (LE) over bytes [0:28]

We set the CE slot's priority byte to 0x00 (unbootable) so Android's A/B
rollback never auto-boots our CoreELEC kernel from that slot. Our env gate uses
raw `imgread boot_X`, which ignores this flag, so CoreELEC still boots.
"""
import struct, zlib

SLOT_BYTE = {"_a": 12, "_b": 14}


def parse(meta32):
    assert len(meta32) == 32, f"need 32 bytes, got {len(meta32)}"
    stored = struct.unpack_from("<I", meta32, 28)[0]
    calc = zlib.crc32(bytes(meta32[0:28])) & 0xffffffff
    return {
        "slot_suffix": bytes(meta32[0:4]),
        "magic": bytes(meta32[4:8]),
        "a_byte": meta32[12], "b_byte": meta32[14],
        "stored_crc": stored, "calc_crc": calc, "crc_ok": stored == calc,
    }


def mark_unbootable(meta32, ce_slot):
    """Return a new 32-byte struct with ce_slot's priority byte zeroed + crc fixed."""
    assert ce_slot in SLOT_BYTE, ce_slot
    info = parse(meta32)
    if not info["crc_ok"]:
        raise ValueError(f"misc crc mismatch (stored {info['stored_crc']:#x} "
                         f"!= calc {info['calc_crc']:#x}) -- refusing to edit")
    if info["magic"] != b"BCAB":
        raise ValueError(f"unexpected magic {info['magic']!r}")
    nd = bytearray(meta32)
    nd[SLOT_BYTE[ce_slot]] = 0x00
    crc = zlib.crc32(bytes(nd[0:28])) & 0xffffffff
    struct.pack_into("<I", nd, 28, crc)
    return bytes(nd)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "mark":
        data = open(sys.argv[2], "rb").read()[:32]
        slot = sys.argv[3]
        info = parse(data)
        print(f"in : a=0x{info['a_byte']:02x} b=0x{info['b_byte']:02x} crc_ok={info['crc_ok']}")
        out = mark_unbootable(data, slot)
        o = parse(out)
        print(f"out: a=0x{o['a_byte']:02x} b=0x{o['b_byte']:02x} crc=0x{o['stored_crc']:08x} (marked {slot} unbootable)")
        open(sys.argv[2] + ".marked", "wb").write(out)
        print("wrote", sys.argv[2] + ".marked")
    else:
        print("usage: ab_misc.py mark <misc32.bin> <_a|_b>")
