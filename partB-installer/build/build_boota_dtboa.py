#!/usr/bin/env python3
"""
Build boota.img and dtboa.img from the extracted CoreELEC payload.

  boota.img  = CoreELEC kernel.img, raw (dd'd into the inactive boot_X; u-boot
               loads it via `imgread kernel boot_X`). Verified against kernel.img.md5.
  dtboa.img  = CoreELEC dtb.img (xiaomi_3rd_gen FDT) zero-padded to exactly
               0x20000 (128 KiB). u-boot reads it via `store read ... dtbo_X 0 0x20000`,
               so the image must be that exact span with the FDT at offset 0.
               (Mirrors user-update.sh: zero 128 KiB, then write the FDT.)

These are slot-agnostic blobs; the installer dd's them into whichever slot is
the INACTIVE one on the target (boot_X / dtbo_X).
"""
import os, hashlib, struct

BASE = os.path.dirname(__file__)
FLASH = os.path.join(BASE, "..", "payload", "flash")
OUT = os.path.join(BASE, "..", "artifacts")
DTBO_SPAN = 0x20000      # 128 KiB, matches the env gate's `store read ... 0 0x20000`

os.makedirs(OUT, exist_ok=True)


def md5(b):
    return hashlib.md5(b).hexdigest()


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# ---- boota.img = kernel.img (verify md5) ------------------------------------
kernel = open(os.path.join(FLASH, "kernel.img"), "rb").read()
stored = open(os.path.join(FLASH, "kernel.img.md5")).read().split()[0]
calc = md5(kernel)
assert calc == stored, f"kernel.img md5 mismatch: {calc} != {stored}"
open(os.path.join(OUT, "boota.img"), "wb").write(kernel)
print(f"boota.img   {len(kernel):>9} B  md5={calc} (matches kernel.img.md5)  sha256={sha256(kernel)[:16]}")

# ---- dtboa.img = dtb.img padded to 128 KiB ----------------------------------
dtb = open(os.path.join(FLASH, "dtb.img"), "rb").read()
# sanity: FDT magic 0xd00dfeed at offset 0
magic = struct.unpack(">I", dtb[:4])[0]
assert magic == 0xd00dfeed, f"dtb.img not an FDT (magic={magic:#x})"
fdt_total = struct.unpack(">I", dtb[4:8])[0]
assert fdt_total == len(dtb), f"FDT totalsize {fdt_total} != file {len(dtb)}"
assert len(dtb) <= DTBO_SPAN, f"dtb {len(dtb)} > 128 KiB span"
dtboa = dtb + b"\x00" * (DTBO_SPAN - len(dtb))
open(os.path.join(OUT, "dtboa.img"), "wb").write(dtboa)
print(f"dtboa.img   {len(dtboa):>9} B  (FDT {len(dtb)} B + {DTBO_SPAN-len(dtb)} B zero pad)  "
      f"fdt_sha256={sha256(dtb)[:16]}")

print("\nboota.img + dtboa.img written to artifacts/")
