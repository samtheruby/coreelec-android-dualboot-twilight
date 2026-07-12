#!/usr/bin/env python3
"""
Complete a PARTIALLY-applied install: the GPT is already the 128-entry carve but
one or more regions (misc, env, CE_FLASH, CE_STORAGE, kernel, dtb) did not land --
e.g. a CE_STORAGE stream that timed out mid-write. The normal installer can't be
re-run here because its preflight requires a stock 32-entry GPT (which we have
deliberately replaced). This regenerates the per-unit env/misc blobs from the
device and writes with idempotent=True: EVERY region is hash-gated, so a region
that already matches the source on eMMC is SKIPPED and only the ones that actually
failed are re-written. Then a SHA-256 read-back verifies ALL regions and disables OTA.

Safe to re-run any number of times: it rebuilds blobs from the current device each
run (env is read fresh, so the gate re-applies onto whatever env is present) and
the hash-gate makes re-writes a no-op once a region is correct. Note the gate reads
each region off the eMMC to hash it, so a run that skips the big CE images still
spends a few minutes hashing 10 GiB+ -- expected, not a hang.

  python finish_install.py --serial <serial> --dry-run
  python finish_install.py --serial <serial> --yes
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import flash_to_coreelec as F  # noqa: E402
import devices  # noqa: E402  -- stick/box discrimination registry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick the only device")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--port", type=int, default=5599)
    # Must match the stage1 run being finished: build_target_blobs rebuilds the env
    # from the device, so omitting this silently flipped a --default coreelec install
    # back to android (caught in the field; masked there only because the pre-reboot
    # repair path ends in a factory reset that clears env for stage2 to re-gate).
    ap.add_argument("--default", choices=["android", "coreelec"], default="android",
                    help="which OS a normal reboot boots -- pass the SAME value the "
                         "original stage1 used (default: android)")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)
    dry = not a.yes
    g = F.Ctx(a.serial, dry, a.port, a.default)

    print(f"=== finish install (serial={a.serial} mode={'DRY-RUN' if dry else 'REAL WRITE'}) ===")
    print(f"    build={F.BUILD}")
    # Identify by model + eMMC size; geometry-dependent completion -> require_layout=True.
    dev = devices.identify(g.getprop,
                           devices.sectors_reader(lambda cmd: g.su(cmd)[0]),
                           require_layout=True, log=print)
    g.device = dev
    g.artdir = F.artdir_for(dev)
    F.require_artifacts(dev)
    if "uid=0" not in g.su("id")[0]:
        sys.exit("su root not available")
    g.pipefail = g.su("set -o pipefail 2>/dev/null && echo Y")[0].strip() == "Y"

    # sanity: GPT should ALREADY be the 128-entry layout (i.e. partly installed)
    g._drop_caches()
    import struct
    gpt = g.su_bytes(f"dd if={F.DISK} bs=512 count=2 2>/dev/null")
    num = struct.unpack_from("<I", gpt, 512 + 80)[0] if gpt[512:520] == b"EFI PART" else -1
    # The on-disk GPT being 128-entry is the indicator of a partly-applied install.
    # by-name still shows the cached stock table until a reboot (no CE_* nodes yet),
    # but env/misc live in unchanged partitions (p2/p11) so writing them works now.
    if num != 128:
        sys.exit(f"GPT entries={num} (expected 128) -- this does NOT look "
                 "partly-installed; use flash_to_coreelec.py instead.")

    active = g.getprop("ro.boot.slot_suffix")
    ce_slot = {"_a": "_b", "_b": "_a"}.get(active)
    if not ce_slot:
        sys.exit(f"bad slot_suffix '{active}'")
    print(f"  device={dev.slug} root=ok pipefail={g.pipefail} GPT=128 active={active} CE={ce_slot}")

    g.build_target_blobs(ce_slot)   # regenerates env_target.bin + misc_sector.bin from device
    if dry:
        print("\n-- would write (GPT already present -> skip_gpt; NO userdata SB wipe) --")
        print("   each region hash-gated: SKIP if eMMC already matches source, else (re)write")
        print("   kernel+dtb (push+dd) ; misc (b64) ; env/gate (push+dd) ;")
        print("   CE_FLASH/CE_STORAGE (nc) ; then verify_writes (SHA-256 all) + disable OTA")
        print("   userdata is left untouched (already sized) -> the env gate survives.")
        print("\nDRY-RUN only. Re-run with --yes.")
        return

    # OTA disable BEFORE write_all: when the pre-reboot f2fs is still live,
    # write_all quiesces it by stopping the framework, and `pm` dies with the
    # framework. Transient either way; stage2's Magisk module is the durable block.
    g.disable_ota()
    try:
        # skip_sbwipe: do NOT wipe the userdata SB here. Userdata is already the right
        # size by the time you're "finishing", and a wipe that takes triggers a recovery
        # factory-reset that resets the env -> wipes the gate we just wrote (re-gate loop).
        # idempotent: hash-gate every region so a re-run skips what already matches on
        # eMMC (e.g. a completed CE_FLASH) and only re-streams what actually failed
        # (e.g. a timed-out CE_STORAGE). Safe to run repeatedly until all regions verify.
        quiesced = g.write_all(ce_slot, skip_gpt=True, skip_sbwipe=True, idempotent=True)
    finally:
        g.adb("forward", "--remove", f"tcp:{g.port}", capture_output=True)

    g.verify_writes(ce_slot)
    if quiesced:
        # write_all had to quiesce a LIVE old-geometry /data, i.e. this repaired a
        # stage1 that died BEFORE its reboot -- so the userdata reformat was never
        # armed (stage1 arms it AFTER verify, which is where it aborted). Without
        # the BCB, a reboot writes the cached full-size f2fs superblock back over
        # the stage1 SB wipe and Android comes up on an oversized fs that will
        # I/O-error once usage crosses the partition end. Arm it now; recovery
        # reformats userdata on the next boot, exactly like the normal stage1 path.
        g.arm_factory_reset()
        print("\n=== completion done (pre-reboot repair) ===")
        print("  REBOOT NOW (adb reboot). Recovery reformats userdata, then Android.")
        print("  The reformat wipes Magisk's data + the env gate -- continue the normal")
        print("  post-stage1 path: install.py stage1b, then stage2.")
    else:
        print("\n=== completion done ===")
        print("  normal reboot -> Android (default); 'Reboot to CoreELEC' app -> CoreELEC")


if __name__ == "__main__":
    main()
