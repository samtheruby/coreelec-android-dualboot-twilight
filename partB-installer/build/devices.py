#!/usr/bin/env python3
"""
Device registry + identification for the CoreELEC internal dual-boot.

WHY THIS EXISTS
---------------
Two supported units share the SAME Android codename `twilight`:

    Xiaomi TV Stick 4K 2nd Gen  -> model MiTV-AFMU1,  ~7.28 GiB eMMC
    Xiaomi TV Box S   3rd Gen   -> model MiTV-AFMU0,  ~29.12 GiB eMMC

Because `ro.product.device` is `twilight` on BOTH, the old codename lock
(`if device != "twilight"`) can no longer tell them apart. If a script picked the
wrong device's artifacts (GPT geometry, CoreELEC images, Magisk-patched init_boot),
it could brick the unit. This module is the single source of truth that every
PC-side script uses to decide *which* physical device is attached and therefore
*which* files are safe to use.

DISCRIMINATION (defence in depth)
---------------------------------
`identify()` cross-checks these facts before it will name a device:

  1. ro.product.model            -- MiTV-AFMU1 (stick) vs MiTV-AFMU0 (box)
  2. ro.product.name             -- adastra   (stick) vs twilight   (box)
  3. eMMC total sector count     -- /sys/class/block/mmcblk0/size   (ROOT ONLY, see below)

A model string alone is not trusted (it could be spoofed or, in theory, reused);
the physical eMMC size is a second, hardware-rooted check. `ro.product.device`
(the shared codename) is cross-checked too, purely as a sanity guard. Any mismatch,
or an unrecognised model, aborts with NO device selected -- fail closed, never guess.

Note the two `twilight`s are different fields: Android's fingerprint is
BRAND/PRODUCT/DEVICE, and Xiaomi built both units from ONE board (device=twilight)
as TWO products (name=adastra for the stick, name=twilight for the box). So the
shared codename is the *board*; the variant lives in name+model.

WHY THE SIZE CHECK IS ROOT-ONLY
-------------------------------
Reading /sys/class/block/mmcblk0/size requires root. AOSP's SELinux policy grants
the `domain` attribute only `sysfs:dir search` and `sysfs:lnk_file read` -- there is
no generic `sysfs:file` read -- and `shell.te` adds just a short allowlist
(sysfs_batteryinfo, sysfs_net, ...) that does not include block devices. So the
non-root `shell` domain gets `Permission denied`, on BOTH units. (/proc/partitions
and /proc/device-tree/amlogic-dt-id are denied for the same reason.)

Every caller that runs AFTER root exists passes an su-backed `read_sectors` and gets
the full hardware-rooted check. `stage_magisk` runs BEFORE root exists -- rooting is
what it is for -- so it passes `read_sectors=None` and the check is skipped, not
faked. It compensates by re-verifying the unit from the BOOTLOADER
(`fastboot getvar partition-size:userdata`, see userdata_size_ok / parse_fastboot_size)
in the moment before it writes anything. That is a harder fact than sysfs: the
bootloader reads the real GPT off the eMMC, with no Android and no properties in the
path.

layout_ready
------------
A device may be positively identifiable long before its partition geometry / carve
layout has been implemented. `layout_ready=False` means "we can recognise this unit,
but we must NOT run any geometry-dependent (partitioning) step against it yet."
Geometry-independent steps (rooting, env-gate re-assert, env/misc restore) still run.

NOTE: the stick's `total_sectors` here MUST equal build/layout.py TOTAL_SECTORS.
They are kept as two literals (this module is intentionally dependency-free so it
can be imported anywhere), so keep them in sync.
"""

import sys

SECTOR = 512
MIB = 1024 * 1024
SEC_PER_MIB = MIB // SECTOR              # 2048


class Device:
    def __init__(self, slug, model, codename, total_sectors,
                 magisk_img, layout_ready, name, notes="",
                 product=None,
                 boot_fingerprint=None,
                 carve_start_mib=None, carve_end_mib=None,
                 sizes_mib=None, order=None,
                 gpt_backup_lba=None, stock_ud_last_lba=None,
                 stock_num_entries=32):
        # ---- identity (discrimination) ----
        self.slug = slug                    # short internal id: "stick" | "box"
        self.model = model                  # ro.product.model, the primary key
        self.codename = codename            # ro.product.device (shared: "twilight")
        self.product = product              # ro.product.name -- NOT shared: adastra|twilight
        self.total_sectors = total_sectors  # mmcblk0 size in 512 B sectors (root-only check)
        self.magisk_img = magisk_img        # filename under magisk/ for stage_magisk
        self.layout_ready = layout_ready    # False -> refuse geometry-dependent steps
        self.name = name                    # human label for logs
        self.notes = notes
        # OPTIONAL manual override of the firmware build the shipped magisk_img was
        # patched from. Normally left None: stage_magisk reads the expected fingerprint
        # straight OUT of the image itself (ro.bootimage.build.fingerprint in the ramdisk
        # build.prop -- see boot_fingerprint_from_img), so there is no pin to drift. Set
        # this only if an image's ramdisk uses a compression we can't unpack.
        self.boot_fingerprint = boot_fingerprint
        # ---- partition geometry (single source; consumed by layout.py, the GPT
        #      builder, and the installer) ----
        self.carve_start_mib = carve_start_mib   # start of the carve region (== stock userdata start)
        self.carve_end_mib = carve_end_mib       # end of the carve region
        self.sizes_mib = sizes_mib or {}         # {userdata, CE_FLASH, CE_STORAGE} -> MiB
        self.order = order or []                 # layout order low->high MiB
        # GPT backup location: BOTH the dd-seek target for gpt_backup.bin AND the
        # LBA where the stock backup blob begins (build_gpt_layout BACK_START_LBA).
        # On the stick these coincide with total-4096; on the box they do NOT
        # (userdata ends ~2 MiB below last_usable), so store it explicitly.
        self.gpt_backup_lba = gpt_backup_lba
        self.stock_ud_last_lba = stock_ud_last_lba   # preflight: stock userdata last_lba
        self.stock_num_entries = stock_num_entries   # stock GPT entry count (32)

    # ---- derived geometry ---------------------------------------------------
    @property
    def carve_total_mib(self):
        return self.carve_end_mib - self.carve_start_mib

    @property
    def stock_ud_first_lba(self):
        return self.carve_start_mib * SEC_PER_MIB

    @property
    def gpt_backup_span(self):
        """Sectors from gpt_backup_lba to end of disk == size of the backup-GPT blob
        (dd count for the backup grab). Stick: 4096 (2 MiB). Box: 33 (array+alt hdr)."""
        return self.total_sectors - self.gpt_backup_lba

    # ---- userdata size: the bootloader-side identity check (stage_magisk) ------
    # `fastboot getvar partition-size:userdata` makes the BOOTLOADER read the real
    # GPT, which is the only hardware-rooted fact available before root exists.
    # A unit is in one of exactly two states, so both are legal:
    #     stock   -- userdata still spans the whole carve region (pre-install)
    #     carved  -- userdata shrunk to sizes_mib["userdata"] (post-install re-run)
    # Stick {4176, 2376} MiB vs box {26540, 14800} MiB: ~6x apart, no overlap.
    @property
    def stock_userdata_bytes(self):
        return (self.stock_ud_last_lba - self.stock_ud_first_lba + 1) * SECTOR

    @property
    def carved_userdata_bytes(self):
        return self.sizes_mib["userdata"] * MIB

    def expected_userdata_bytes(self):
        return (self.stock_userdata_bytes, self.carved_userdata_bytes)

    def userdata_size_ok(self, nbytes):
        return nbytes in self.expected_userdata_bytes()

    def partitions(self):
        """Yield (name, start_mib, end_mib, size_mib) in layout order."""
        cur = self.carve_start_mib
        out = []
        for name in self.order:
            size = self.sizes_mib[name]
            out.append((name, cur, cur + size, size))
            cur += size
        assert cur == self.carve_end_mib, \
            f"{self.slug} layout sums to {cur} MiB, expected {self.carve_end_mib}"
        return out

    def as_sectors(self):
        """Same as partitions() but (name, start_lba, end_lba_inclusive, count)."""
        out = []
        for name, s_mib, e_mib, _ in self.partitions():
            start = s_mib * SEC_PER_MIB
            end = e_mib * SEC_PER_MIB - 1        # GPT last_lba is inclusive
            out.append((name, start, end, end - start + 1))
        return out

    def __repr__(self):
        return f"<Device {self.slug} model={self.model} sectors={self.total_sectors}>"


# ----------------------------------------------------------------------------
# The registry. Add a new physical device here and every script picks it up.
# ----------------------------------------------------------------------------
STICK = Device(
    slug="stick",
    model="MiTV-AFMU1",
    codename="twilight",
    product="adastra",                      # ro.product.name (confirmed on-device)
    total_sectors=15_269_888,               # 7.28 GiB
    magisk_img="twilight-init_boot-patched.img",
    layout_ready=True,
    name="Xiaomi TV Stick 4K 2nd Gen",
    carve_start_mib=3278,                    # == stock userdata start (sector 6,713,344)
    carve_end_mib=7454,                      # sector 15,265,792 (== last_usable+1)
    sizes_mib={"userdata": 2376, "CE_FLASH": 600, "CE_STORAGE": 1200},
    order=["userdata", "CE_FLASH", "CE_STORAGE"],
    gpt_backup_lba=15_265_792,               # total-4096: start of the stick's 2 MiB backup-GPT grab
    stock_ud_last_lba=15_265_791,
)

BOX = Device(
    slug="box",
    model="MiTV-AFMU0",
    codename="twilight",
    product="twilight",                     # ro.product.name == the board name here
    total_sectors=61_071_360,               # 29.12 GiB (from recon)
    magisk_img="xiaomi_tv_box_s_3rd_gen_init_boot.img",
    layout_ready=True,                       # geometry implemented + artifacts/box/ built
    name="Xiaomi TV Box S 3rd Gen",
    notes="",
    carve_start_mib=3278,                    # same stock userdata start (sector 6,713,344)
    carve_end_mib=29818,                     # 3278 + 26540 (carve = stock userdata span)
    sizes_mib={"userdata": 14800, "CE_FLASH": 1500, "CE_STORAGE": 10240},
    order=["userdata", "CE_FLASH", "CE_STORAGE"],
    # NOTE: unlike the stick, backup GPT is NOT at total-4096. Box stock userdata ends
    # ~2 MiB below last_usable, so the backup array+alt-header sit at last_usable+1.
    gpt_backup_lba=61_071_327,               # last_usable(61,071,326)+1; 33-sector backup blob
    stock_ud_last_lba=61_067_263,            # ud start 6,713,344 + size 54,353,920 - 1
)

DEVICES = [STICK, BOX]
BY_MODEL = {d.model: d for d in DEVICES}
BY_SLUG = {d.slug: d for d in DEVICES}


def known_models():
    return sorted(BY_MODEL)


def identify(getprop, read_sectors=None, require_layout=False, log=None):
    """Return the Device attached, or raise SystemExit (fail closed).

    Parameters
    ----------
    getprop : callable(name) -> str
        Reads an Android property (e.g. a wrapper around `adb ... getprop`).
        Works without root.
    read_sectors : callable() -> int, or None
        Returns mmcblk0 size in 512 B sectors
        (e.g. `int(cat /sys/class/block/mmcblk0/size)`). REQUIRES ROOT: SELinux
        denies the non-root `shell` domain that read (see the module docstring).
        Pass None from a pre-root caller (stage_magisk) to skip the size check --
        skipping it is honest; faking it is not. Every root-side caller passes an
        su-backed reader and gets the full hardware-rooted check.
    require_layout : bool
        If True, also refuse a device whose partition layout is not implemented
        yet (layout_ready=False). Use for any geometry/partitioning step.
    log : callable(str) or None
        Optional line logger for the confirmation message.

    On success prints (via `log`) a one-line identity confirmation and returns
    the Device. On ANY mismatch it raises SystemExit and selects nothing.
    """
    model = (getprop("ro.product.model") or "").strip()
    codename = (getprop("ro.product.device") or "").strip()
    product = (getprop("ro.product.name") or "").strip()

    dev = BY_MODEL.get(model)
    if dev is None:
        raise SystemExit(
            f"unrecognised model '{model}' (device='{codename}') -- not a supported "
            f"unit. Known: {', '.join(known_models())}. Abort (no device selected).")

    # Cross-check the shared codename as a sanity guard.
    if codename != dev.codename:
        raise SystemExit(
            f"model {model} ({dev.slug}) expects codename '{dev.codename}' but the "
            f"device reports '{codename}' -- identity mismatch. Abort.")

    # ro.product.name is the one variant signal besides the model that reads without
    # root: adastra (stick) vs twilight (box). Enforce it when the unit reports one.
    if product and product != dev.product:
        raise SystemExit(
            f"model {model} ({dev.slug}) expects product '{dev.product}' but the "
            f"device reports '{product}' -- identity mismatch. Abort.")
    if not product and log:
        log(f"  WARNING: this unit reports no ro.product.name (expected "
            f"'{dev.product}') -- that cross-check is unavailable.")

    # Independent, hardware-rooted check: the physical eMMC size. Root-only.
    sectors = None
    if read_sectors is not None:
        try:
            sectors = int(read_sectors())
        except (ValueError, TypeError, IndexError) as e:
            raise SystemExit(
                f"could not read mmcblk0 sector count ({type(e).__name__}: {e}) -- the "
                f"sysfs read returned nothing. This needs root: SELinux denies the "
                f"non-root shell domain that read. Refusing to proceed without the size "
                f"cross-check. Abort.")
        if sectors != dev.total_sectors:
            raise SystemExit(
                f"{dev.slug} ({model}) expects {dev.total_sectors:,} eMMC sectors but "
                f"mmcblk0 reports {sectors:,} -- WRONG DEVICE/GEOMETRY. Abort.")

    if require_layout and not dev.layout_ready:
        raise SystemExit(
            f"{dev.name} ({model}) recognised, but its partition layout/carve is not "
            f"implemented yet (pending the carve-size decision). Refusing to run a "
            f"geometry-dependent step. {dev.notes}")

    if log:
        size = (f"eMMC={sectors:,} sectors -- identity + geometry match"
                if sectors is not None else
                "eMMC size not checked (needs root; the bootloader re-verifies "
                "this unit before any write)")
        log(f"  device: {dev.name} [{dev.slug}] model={model} product={product} {size}")
    return dev


# Convenience: a standard mmcblk0 sector reader given a shell-exec callable that
# returns text. `su_text(cmd) -> str`. Kept here so callers don't re-implement it.
# MUST be given an su-backed exec: a non-root shell gets 'Permission denied' on stderr
# and nothing on stdout. Returns None (not a crash) when the read yields no number, so
# identify() can fail closed with a message instead of an IndexError traceback.
def sectors_reader(su_text):
    def _read():
        out = (su_text("cat /sys/class/block/mmcblk0/size") or "").split()
        return int(out[0]) if out and out[0].isdigit() else None
    return _read


# `fastboot getvar partition-size:<part>` prints e.g.
#     partition-size:userdata: 0x0000000094800000
# on stdout or stderr depending on the fastboot build. Returns bytes, or None if the
# variable is absent/unsupported (older bootloader) -- absence is not evidence of a
# wrong device, so callers must treat None as "unknown", not as "mismatch".
def parse_fastboot_size(raw):
    import re as _re
    m = _re.search(r"0x([0-9a-fA-F]+)", raw or "")
    return int(m.group(1), 16) if m else None


# ---------------------------------------------------------------------------
# init_boot firmware-match guard (for stage_magisk)
# ---------------------------------------------------------------------------
# A pre-patched init_boot is bytewise tied to the exact stock build it was patched
# from; flashing it onto a unit on a DIFFERENT build can bootloop. That build is
# recorded INSIDE the image -- the ramdisk carries system/etc/ramdisk/build.prop
# with ro.bootimage.build.fingerprint -- so we read it back and let stage_magisk
# refuse a mismatch. No hardcoded pin, no drift.
_BOOT_FP_KEY = b"ro.bootimage.build.fingerprint="


def _lz4_legacy_decompress(data):
    """Decode an lz4 'legacy' frame (magic 02 21 4c 18) -- Magisk's usual ramdisk
    compression -- in pure Python (no external tools / deps)."""
    import struct as _s
    out = bytearray(); i = 4                        # skip the 4-byte magic
    while i + 4 <= len(data):
        bs = _s.unpack_from("<I", data, i)[0]; i += 4
        if bs == 0 or bs > len(data) - i:
            break
        block = data[i:i + bs]; i += bs
        j = 0; n = len(block)
        while j < n:                                # one lz4 block = a run of sequences
            tok = block[j]; j += 1
            lit = tok >> 4
            if lit == 15:
                while True:
                    b = block[j]; j += 1; lit += b
                    if b != 255: break
            out += block[j:j + lit]; j += lit
            if j >= n:
                break
            off = block[j] | (block[j + 1] << 8); j += 2
            m = tok & 15
            if m == 15:
                while True:
                    b = block[j]; j += 1; m += b
                    if b != 255: break
            m += 4; st = len(out) - off
            for k in range(m):                      # match copy (may overlap)
                out.append(out[st + k])
    return bytes(out)


def _ramdisk_bytes(img):
    """Decompressed ramdisk of an Android boot/init_boot image (header v3/v4).
    Handles lz4-legacy / gzip / xz; returns the raw ramdisk if uncompressed."""
    import struct as _s
    if img[:8] != b"ANDROID!":
        return b""
    ks, rs = _s.unpack_from("<II", img, 8)
    off = 4096 + ((ks + 4095) // 4096) * 4096       # ramdisk follows the page-aligned kernel
    rd = img[off:off + rs]
    if rd[:4] == b"\x02\x21\x4c\x18":
        return _lz4_legacy_decompress(rd)
    if rd[:2] == b"\x1f\x8b":
        import gzip; return gzip.decompress(rd)
    if rd[:5] == b"\xfd7zXZ":
        import lzma; return lzma.decompress(rd)
    return rd                                        # uncompressed cpio -> substring search still works


def boot_fingerprint_from_img(path):
    """ro.bootimage.build.fingerprint baked into a boot/init_boot image, or None if it
    can't be read (unknown ramdisk format / prop absent)."""
    try:
        raw = _ramdisk_bytes(open(path, "rb").read())
    except Exception:
        return None
    idx = raw.find(_BOOT_FP_KEY)
    if idx < 0:
        return None
    end = raw.find(b"\n", idx)
    end = len(raw) if end < 0 else end
    return raw[idx + len(_BOOT_FP_KEY):end].decode("latin1").strip() or None


def expected_boot_fingerprint(dev, img_path):
    """The build fingerprint the shipped init_boot must match: read from the image,
    or the device's manual override if set."""
    return dev.boot_fingerprint or boot_fingerprint_from_img(img_path)


if __name__ == "__main__":
    # Print the registry + validate every device's geometry (sanity/inspection).
    for d in DEVICES:
        ready = "layout READY" if d.layout_ready else "layout pending"
        print(f"\n{d.slug:<6} {d.model:<12} {d.codename:<10} "
              f"{d.total_sectors:>12,} sec  {ready}  magisk={d.magisk_img}")
        got = sum(d.sizes_mib.values())
        assert got == d.carve_total_mib, \
            f"{d.slug}: sizes sum {got} != carve {d.carve_total_mib}"
        print(f"       carve {d.carve_start_mib}..{d.carve_end_mib} MiB "
              f"({d.carve_total_mib} MiB)  gpt_backup_lba={d.gpt_backup_lba:,}")
        for name, s, e, c in d.as_sectors():
            print(f"       {name:<12} LBA {s:>10}..{e:<10} ({c // SEC_PER_MIB} MiB)")
    # Guard against duplicate keys.
    assert len(BY_MODEL) == len(DEVICES), "duplicate model in registry"
    assert len(BY_SLUG) == len(DEVICES), "duplicate slug in registry"
    print("\nregistry OK (geometry validated)")
    sys.exit(0)
