#!/usr/bin/env python3
"""
Build the FINAL 128-entry GPT for a stock twilight unit, with the dual-boot
layout already applied. Output: two raw blobs to dd straight onto mmcblk0.

  gpt_primary.bin  -> dd seek=0            (LBA 0..33, 34 sectors)
  gpt_backup.bin   -> dd seek=15265792     (the last-2M region: entry array + alt header)

Starts from the STOCK GPT reference (refdata/stock_gpt_first2m.bin +
stock_gpt_last2m.bin -- identity-free: only random partition GUIDs + generic
partition names, no serial/MAC/cpu_id), then:
  1. expands the entry array 32 -> 128 (zeroing stale backup slots) -- proven by
     the original build_gpt128.py; u-boot uses the PRIMARY GPT.
  2. resizes the userdata entry (keeps its type GUID, unique GUID, attrs, name;
     only last_lba shrinks) per build.layout.
  3. adds CE_FLASH (MS basic data / FAT32) and CE_STORAGE (Linux fs / ext4)
     entries with fresh unique GUIDs.
  4. recomputes entry-array CRC32 + header CRC32 for BOTH primary and backup.

Self-verifies: re-parses output, checks every CRC, confirms the 31 untouched
partitions are byte-identical to stock, confirms first_usable_lba unchanged,
and confirms the 3 carve partitions match build.layout exactly.

This is the single most dangerous artifact (a bad CRC = unbootable GPT), so it
refuses to write unless every check passes.
"""
import struct, zlib, os, uuid, sys
import layout as L

BK = os.path.join(os.path.dirname(__file__), "..", "refdata")
OUT = os.path.join(os.path.dirname(__file__), "..", "artifacts")
SECTOR = L.SECTOR
BACK_START_LBA = 15_265_792             # where stock_gpt_last2m.bin begins
NEW_NUM = 128
ARR_BYTES = NEW_NUM * 128               # 16384

# fixed unique GUIDs for the two new partitions (baked into the shipped image;
# identical across flashed units, like any factory image -- acceptable).
UNIQUE_CE_FLASH   = uuid.UUID("11111111-2222-3333-4444-c0de0ceaf1a5")
UNIQUE_CE_STORAGE = uuid.UUID("11111111-2222-3333-4444-c0de0ce570a9")


def parse_hdr(buf, off):
    h = buf[off:off+92]
    assert h[:8] == b"EFI PART", f"no EFI PART at {off:#x}"
    return {
        "my_lba":       struct.unpack_from("<Q", h, 24)[0],
        "alt_lba":      struct.unpack_from("<Q", h, 32)[0],
        "first_usable": struct.unpack_from("<Q", h, 40)[0],
        "last_usable":  struct.unpack_from("<Q", h, 48)[0],
        "pe_lba":       struct.unpack_from("<Q", h, 72)[0],
        "num":          struct.unpack_from("<I", h, 80)[0],
        "esize":        struct.unpack_from("<I", h, 84)[0],
        "arr_crc":      struct.unpack_from("<I", h, 88)[0],
        "hdr_crc":      struct.unpack_from("<I", h, 16)[0],
    }


def hdr_crc_ok(buf, off):
    h = bytearray(buf[off:off+92]); stored = struct.unpack_from("<I", h, 16)[0]
    struct.pack_into("<I", h, 16, 0)
    return zlib.crc32(h) & 0xffffffff == stored


def entry_name(e):
    return e[56:128].decode("utf-16-le", "replace").split("\x00")[0]


def make_entry(type_guid_bytes, unique_guid, first_lba, last_lba, attrs, name):
    e = bytearray(128)
    e[0:16]  = type_guid_bytes
    e[16:32] = unique_guid.bytes_le
    struct.pack_into("<Q", e, 32, first_lba)
    struct.pack_into("<Q", e, 40, last_lba)
    struct.pack_into("<Q", e, 48, attrs)
    nm = name.encode("utf-16-le")
    assert len(nm) <= 72, f"name too long: {name}"
    e[56:56+len(nm)] = nm
    return bytes(e)


def rebuild_crcs(buf, hdr_off, arr_off):
    arr_crc = zlib.crc32(buf[arr_off:arr_off+ARR_BYTES]) & 0xffffffff
    struct.pack_into("<I", buf, hdr_off+80, NEW_NUM)
    struct.pack_into("<I", buf, hdr_off+88, arr_crc)
    struct.pack_into("<I", buf, hdr_off+16, 0)
    hdr_crc = zlib.crc32(buf[hdr_off:hdr_off+92]) & 0xffffffff
    struct.pack_into("<I", buf, hdr_off+16, hdr_crc)
    return arr_crc, hdr_crc


def main():
    prim = bytearray(open(os.path.join(BK, "stock_gpt_first2m.bin"), "rb").read())
    back = bytearray(open(os.path.join(BK, "stock_gpt_last2m.bin"), "rb").read())

    prim_hdr_off = 512
    back_hdr_off = back.rfind(b"EFI PART")
    ph = parse_hdr(prim, prim_hdr_off)
    bh = parse_hdr(back, back_hdr_off)
    print("stock primary:", {k: ph[k] for k in ("num", "esize", "first_usable", "last_usable")})
    assert ph["num"] == 32 and ph["esize"] == 128, "unexpected stock GPT"
    assert hdr_crc_ok(prim, prim_hdr_off) and hdr_crc_ok(back, back_hdr_off), "stock hdr CRC bad"

    p_arr = ph["pe_lba"] * SECTOR
    b_arr = (bh["pe_lba"] - BACK_START_LBA) * SECTOR

    # --- locate stock userdata entry (to copy its type+unique GUID, attrs) ----
    ud_idx = None
    for i in range(32):
        e = prim[p_arr+i*128: p_arr+(i+1)*128]
        if entry_name(e) == "userdata":
            ud_idx = i; break
    assert ud_idx is not None, "userdata entry not found in stock GPT"
    ud = prim[p_arr+ud_idx*128: p_arr+(ud_idx+1)*128]
    ud_first = struct.unpack_from("<Q", ud, 32)[0]
    ud_last  = struct.unpack_from("<Q", ud, 40)[0]
    print(f"stock userdata = slot {ud_idx} (p{ud_idx+1}), LBA {ud_first}..{ud_last}")

    secs = {n: (s, e, c) for n, s, e, c in L.as_sectors()}
    assert ud_first == secs["userdata"][0], \
        f"layout userdata start {secs['userdata'][0]} != stock {ud_first}"

    # snapshot all 32 stock entries for the untouched-check
    stock_entries = [bytes(prim[p_arr+i*128: p_arr+(i+1)*128]) for i in range(32)]

    # --- build the new entry array (128 slots) -------------------------------
    new_arr = bytearray(ARR_BYTES)
    # copy stock slots 0..31 verbatim
    new_arr[0:32*128] = prim[p_arr: p_arr+32*128]

    # resize userdata in place: keep everything, change last_lba only
    s, e, _ = secs["userdata"]
    struct.pack_into("<Q", new_arr, ud_idx*128 + 40, e)   # new last_lba

    # add CE_FLASH (slot 32 / p33) and CE_STORAGE (slot 33 / p34)
    cf_type = uuid.UUID(L.GUID_MS_BASIC).bytes_le
    cs_type = uuid.UUID(L.GUID_LINUX_FS).bytes_le
    s, e, _ = secs["CE_FLASH"]
    new_arr[32*128:33*128] = make_entry(cf_type, UNIQUE_CE_FLASH, s, e, 0, "CE_FLASH")
    s, e, _ = secs["CE_STORAGE"]
    new_arr[33*128:34*128] = make_entry(cs_type, UNIQUE_CE_STORAGE, s, e, 0, "CE_STORAGE")

    # --- write new array into primary + backup, fix CRCs ---------------------
    prim[p_arr: p_arr+ARR_BYTES] = new_arr
    back[b_arr: b_arr+ARR_BYTES] = new_arr
    pac, phc = rebuild_crcs(prim, prim_hdr_off, p_arr)
    bac, bhc = rebuild_crcs(back, back_hdr_off, b_arr)
    print(f"primary arr_crc={pac:#010x} hdr_crc={phc:#010x}")
    print(f"backup  arr_crc={bac:#010x} hdr_crc={bhc:#010x}")

    # ======================= VERIFY (refuse to write on any failure) =========
    ok = True
    ph2 = parse_hdr(prim, prim_hdr_off); bh2 = parse_hdr(back, back_hdr_off)

    def chk(cond, msg):
        nonlocal ok
        print(("  OK  " if cond else " FAIL ") + msg); ok = ok and cond

    chk(ph2["num"] == 128 and bh2["num"] == 128, "num_entries == 128 (primary+backup)")
    chk(hdr_crc_ok(prim, prim_hdr_off), "primary header CRC valid")
    chk(hdr_crc_ok(back, back_hdr_off), "backup header CRC valid")
    chk((zlib.crc32(prim[p_arr:p_arr+ARR_BYTES]) & 0xffffffff) == ph2["arr_crc"], "primary array CRC valid")
    chk((zlib.crc32(back[b_arr:b_arr+ARR_BYTES]) & 0xffffffff) == bh2["arr_crc"], "backup array CRC valid")
    chk(ph2["first_usable"] == ph["first_usable"], f"first_usable unchanged ({ph['first_usable']})")
    chk(bytes(prim[p_arr:p_arr+ARR_BYTES]) == bytes(back[b_arr:b_arr+ARR_BYTES]), "primary array == backup array")

    # untouched partitions byte-identical (all 32 stock slots except userdata)
    for i in range(32):
        cur = bytes(prim[p_arr+i*128: p_arr+(i+1)*128])
        if i == ud_idx:
            # only last_lba (off 40..47) may differ
            chk(cur[:40] == stock_entries[i][:40] and cur[48:] == stock_entries[i][48:],
                f"userdata slot {i}: only last_lba changed")
        else:
            chk(cur == stock_entries[i], f"slot {i} ({entry_name(cur) or 'empty'}) byte-identical")

    # carve partitions match layout
    for name, idx in (("userdata", ud_idx), ("CE_FLASH", 32), ("CE_STORAGE", 33)):
        e = prim[p_arr+idx*128: p_arr+(idx+1)*128]
        fl = struct.unpack_from("<Q", e, 32)[0]; ll = struct.unpack_from("<Q", e, 40)[0]
        es, ee, _ = secs[name]
        chk(fl == es and ll == ee and entry_name(e) == name,
            f"{name}: LBA {fl}..{ll} name='{entry_name(e)}' (want {es}..{ee})")

    if not ok:
        print("\nVERIFY FAILED -- not writing.")
        sys.exit(1)

    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "gpt_primary.bin"), "wb").write(prim[0:34*SECTOR])
    open(os.path.join(OUT, "gpt_backup.bin"), "wb").write(back)
    print(f"\nwrote gpt_primary.bin (34 sectors) + gpt_backup.bin ({len(back)} B) to artifacts/")
    print(f"APPLY: dd gpt_primary.bin seek=0 ; dd gpt_backup.bin seek={BACK_START_LBA}")


if __name__ == "__main__":
    main()
