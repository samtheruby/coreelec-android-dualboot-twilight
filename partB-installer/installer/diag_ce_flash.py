#!/usr/bin/env python3
"""
READ-ONLY diagnostic for a CE_FLASH (or CE_STORAGE) SHA read-back FAIL.

Writes NOTHING. Answers the three questions the pass/fail line can't:

  1. WHERE does the on-eMMC region diverge from the image we streamed?
     Chunk-by-chunk SHA-256 compare -> a map of good/bad chunks.
       - all chunks bad            -> the write landed at the wrong offset, or the
                                      stream never reached the disk
       - a contiguous BAD TAIL     -> the decompress/dd stopped early (truncation)
       - scattered bad chunks      -> something else wrote into the region
  2. Is the region STABLE, or is something still writing into it?
     The bad chunks are re-hashed a second time (caches dropped in between). A hash
     that CHANGES between two reads of an idle disk means a live writer -- the
     mounted f2fs /data still spans these LBAs under the old geometry.
  3. WHAT is actually in the first bad chunk? (zeros / old userdata / f2fs blocks)

  python installer/diag_ce_flash.py                 # auto-picks the only adb device
  python installer/diag_ce_flash.py --region CE_STORAGE
"""
import argparse, gzip, hashlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
sys.path.insert(0, HERE)
import flash_to_coreelec as F  # noqa: E402
import devices  # noqa: E402

CHUNK_SECTORS = 65536          # 32 MiB per chunk
SEC = 512


def pc_chunk_hashes(path, is_gz, chunk_bytes):
    """SHA-256 of each chunk of the image AS FLASHED (gunzipped if we streamed the gz)."""
    op = (lambda: gzip.open(path, "rb")) if is_gz else (lambda: open(path, "rb"))
    hashes, total = [], 0
    with op() as f:
        while True:
            h, got = hashlib.sha256(), 0
            while got < chunk_bytes:
                b = f.read(min(1 << 22, chunk_bytes - got))
                if not b:
                    break
                h.update(b); got += len(b)
            if got == 0:
                break
            hashes.append(h.hexdigest()); total += got
            if got < chunk_bytes:
                break
    return hashes, total


def dev_chunk_hashes(g, start_lba, idxs, n_sectors):
    """SHA-256 of the given chunk indexes read straight off the eMMC (caches dropped)."""
    g._drop_caches()
    lines = []
    for i in idxs:
        skip = start_lba + i * CHUNK_SECTORS
        count = min(CHUNK_SECTORS, n_sectors - i * CHUNK_SECTORS)
        lines.append(f"dd if={F.DISK} bs=512 skip={skip} count={count} 2>/dev/null | sha256sum")
    out, _ = g.su("; ".join(lines))
    return [ln.split()[0] for ln in out.strip().splitlines() if ln.strip()]


def hexdump(b, base=0, limit=128):
    for off in range(0, min(len(b), limit), 16):
        row = b[off:off + 16]
        hx = " ".join(f"{c:02x}" for c in row).ljust(47)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in row)
        print(f"    {base + off:#010x}  {hx}  |{asc}|")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial")
    ap.add_argument("--region", default="CE_FLASH", choices=["CE_FLASH", "CE_STORAGE"])
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    g = F.Ctx(a.serial, dry=True, port=5599)
    g.device = devices.identify(g.getprop,
                                devices.sectors_reader(lambda c: g.su(c)[0]),
                                require_layout=True, log=print)
    g.artdir = F.artdir_for(g.device)
    if "uid=0" not in g.su("id")[0]:
        sys.exit("su root not available")

    basename = {"CE_FLASH": "ce_flash.img", "CE_STORAGE": "ce_storage.img"}[a.region]
    path, is_gz = g._img_payload(basename)
    s = F.secs(g.device)
    start_lba, _, part_sectors = s[a.region]

    print(f"\n-- {a.region} --")
    print(f"  payload      : {os.path.basename(path)} ({os.path.getsize(path):,} B on disk, gz={is_gz})")
    print(f"  partition    : LBA {start_lba:,} .. +{part_sectors:,} sectors "
          f"({part_sectors * SEC / 1048576:.0f} MiB)")

    # ---- 1. what the PC thinks should be there, chunk by chunk ----
    pc, img_bytes = pc_chunk_hashes(path, is_gz, CHUNK_SECTORS * SEC)
    img_sectors = img_bytes // SEC
    print(f"  image        : {img_bytes:,} B ({img_bytes / 1048576:.0f} MiB) -> {len(pc)} chunks of "
          f"{CHUNK_SECTORS * SEC // 1048576} MiB")
    if img_bytes % SEC:
        print(f"  NOTE: image is not sector-aligned ({img_bytes % SEC} B tail)")
    if img_sectors > part_sectors:
        print(f"  !! image is LARGER than the partition by "
              f"{(img_sectors - part_sectors) * SEC:,} B -- it overflows the next partition")

    # ---- 2. what is actually on the eMMC, chunk by chunk ----
    print(f"\n-- reading {img_bytes / 1048576:.0f} MiB off eMMC (this takes a minute) --")
    dev = dev_chunk_hashes(g, start_lba, range(len(pc)), img_sectors)
    if len(dev) != len(pc):
        sys.exit(f"device returned {len(dev)} chunk hashes, expected {len(pc)} -- read failed")

    bad = [i for i, (p, d) in enumerate(zip(pc, dev)) if p != d]
    print(f"\n-- chunk map ({len(pc)} chunks, '.' = match, 'X' = differs) --")
    print("  " + "".join("." if i not in bad else "X" for i in range(len(pc))))
    if not bad:
        print("  every chunk matches -- the region is byte-identical to the source NOW.")
        print("  (a verify that failed earlier but passes now = something raced the verify)")
        return
    print(f"  {len(bad)}/{len(pc)} chunks differ: {bad[:24]}{' ...' if len(bad) > 24 else ''}")
    contig_tail = bad == list(range(bad[0], len(pc)))
    if bad == list(range(len(pc))):
        print("  SHAPE: ALL chunks differ -> the stream never landed here (wrong offset?)")
    elif contig_tail:
        off = bad[0] * CHUNK_SECTORS * SEC
        print(f"  SHAPE: contiguous BAD TAIL from chunk {bad[0]} (image offset {off:,} B) ->")
        print(f"         the write/decompress stopped early; first {off:,} B are correct")
    else:
        print("  SHAPE: scattered bad chunks -> something OVERWROTE parts of a good write")

    # ---- 3. stable, or is a live writer still touching it? ----
    probe = bad[:6]
    print(f"\n-- re-reading bad chunks {probe} (caches dropped) to test stability --")
    dev2 = dev_chunk_hashes(g, start_lba, probe, img_sectors)
    moved = [i for i, h in zip(probe, dev2) if h != dev[i]]
    if moved:
        print(f"  !! chunks {moved} CHANGED between two reads of an idle disk")
        print("     -> a LIVE WRITER is still writing into this region (mounted f2fs /data")
        print("        still spans these LBAs under the pre-carve geometry)")
    else:
        print("  stable across both reads -> no live writer; the bad bytes were written once")

    # ---- 4. what is in the first bad chunk? ----
    off = bad[0] * CHUNK_SECTORS * SEC
    lba = start_lba + bad[0] * CHUNK_SECTORS
    print(f"\n-- first bad chunk {bad[0]}: image offset {off:,} B, LBA {lba:,} --")
    got = g.su_bytes(f"dd if={F.DISK} bs=512 skip={lba} count=1 2>/dev/null")[:512]
    with (gzip.open(path, "rb") if is_gz else open(path, "rb")) as f:
        skipped = 0
        while skipped < off:
            b = f.read(min(1 << 22, off - skipped))
            if not b:
                break
            skipped += len(b)
        want = f.read(512)
    print("  ON DEVICE:")
    hexdump(got, off)
    print(f"    ({'ALL ZERO' if got.strip(b'0') == b'' or set(got) == {0} else 'non-zero'})")
    print("  EXPECTED:")
    hexdump(want, off)

    # ---- 5. is /data still mounted over these LBAs? ----
    print("\n-- mounted /data (the pre-carve f2fs spans the whole old userdata span) --")
    mounts, _ = g.su("cat /proc/mounts")
    for ln in mounts.splitlines():
        if " /data " in ln or "userdata" in ln:
            print("  " + ln.strip())
    print(f"\n  stock userdata span: LBA {g.device.stock_ud_first_lba:,} .. "
          f"{g.device.stock_ud_last_lba:,}")
    print(f"  {a.region} sits INSIDE that span: "
          f"{g.device.stock_ud_first_lba <= start_lba <= g.device.stock_ud_last_lba}")


if __name__ == "__main__":
    main()
