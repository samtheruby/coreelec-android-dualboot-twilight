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
`identify()` requires TWO independent facts to agree before it will name a device:

  1. ro.product.model            -- MiTV-AFMU1 (stick) vs MiTV-AFMU0 (box)
  2. eMMC total sector count     -- /sys/class/block/mmcblk0/size

A model string alone is not trusted (it could be spoofed or, in theory, reused);
the physical eMMC size is a second, hardware-rooted check. `ro.product.device`
(the shared codename) is cross-checked too, purely as a sanity guard. Any mismatch,
or an unrecognised model, aborts with NO device selected -- fail closed, never guess.

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


class Device:
    def __init__(self, slug, model, codename, total_sectors,
                 magisk_img, layout_ready, name, notes=""):
        self.slug = slug                    # short internal id: "stick" | "box"
        self.model = model                  # ro.product.model, the primary key
        self.codename = codename            # ro.product.device (shared: "twilight")
        self.total_sectors = total_sectors  # mmcblk0 size in 512 B sectors (2nd check)
        self.magisk_img = magisk_img        # filename under magisk/ for stage_magisk
        self.layout_ready = layout_ready    # False -> refuse geometry-dependent steps
        self.name = name                    # human label for logs
        self.notes = notes

    def __repr__(self):
        return f"<Device {self.slug} model={self.model} sectors={self.total_sectors}>"


# ----------------------------------------------------------------------------
# The registry. Add a new physical device here and every script picks it up.
# ----------------------------------------------------------------------------
STICK = Device(
    slug="stick",
    model="MiTV-AFMU1",
    codename="twilight",
    total_sectors=15_269_888,               # == build/layout.py TOTAL_SECTORS
    magisk_img="twilight-init_boot-patched.img",
    layout_ready=True,
    name="Xiaomi TV Stick 4K 2nd Gen",
)

BOX = Device(
    slug="box",
    model="MiTV-AFMU0",
    codename="twilight",
    total_sectors=61_071_360,               # 29.12 GiB (from recon)
    magisk_img="xiaomi_tv_box_s_3rd_gen_init_boot.img",
    layout_ready=False,                      # carve split undecided -> no geometry steps yet
    name="Xiaomi TV Box S 3rd Gen",
    notes="Geometry/carve layout pending the carve-size decision. See PORT-TVBOX-S-3RDGEN.md.",
)

DEVICES = [STICK, BOX]
BY_MODEL = {d.model: d for d in DEVICES}
BY_SLUG = {d.slug: d for d in DEVICES}


def known_models():
    return sorted(BY_MODEL)


def identify(getprop, read_sectors, require_layout=False, log=None):
    """Return the Device attached, or raise SystemExit (fail closed).

    Parameters
    ----------
    getprop : callable(name) -> str
        Reads an Android property (e.g. a wrapper around `adb ... getprop`).
        Must work WITHOUT root (stage_magisk runs before root exists).
    read_sectors : callable() -> int
        Returns mmcblk0 size in 512 B sectors
        (e.g. `int(cat /sys/class/block/mmcblk0/size)`). No root required.
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

    # Second, independent, hardware-rooted check: the physical eMMC size.
    try:
        sectors = int(read_sectors())
    except (ValueError, TypeError) as e:
        raise SystemExit(f"could not read mmcblk0 sector count ({e}) -- refusing to "
                         "proceed without the size cross-check. Abort.")
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
        log(f"  device: {dev.name} [{dev.slug}] model={model} "
            f"eMMC={sectors:,} sectors -- identity + geometry match")
    return dev


# Convenience: a standard mmcblk0 sector reader given a shell-exec callable that
# returns text. `su_text(cmd) -> str`. Kept here so callers don't re-implement it.
def sectors_reader(su_text):
    def _read():
        out = su_text("cat /sys/class/block/mmcblk0/size").strip()
        return int(out.split()[0])
    return _read


if __name__ == "__main__":
    # Print the registry (sanity/inspection).
    for d in DEVICES:
        ready = "layout READY" if d.layout_ready else "layout pending"
        print(f"{d.slug:<6} {d.model:<12} {d.codename:<10} "
              f"{d.total_sectors:>12,} sec  {ready}  magisk={d.magisk_img}")
    # Guard against duplicate keys.
    assert len(BY_MODEL) == len(DEVICES), "duplicate model in registry"
    assert len(BY_SLUG) == len(DEVICES), "duplicate slug in registry"
    print("registry OK")
    sys.exit(0)
