#!/usr/bin/env python3
"""
Complete a PARTIALLY-applied install: GPT + CE_FLASH + CE_STORAGE + kernel + dtb
are already written, but misc and/or env are not. The normal installer can't be
re-run here because its preflight requires a stock 32-entry GPT (which we have
deliberately replaced). This regenerates the per-unit env/misc blobs from the
device, writes misc (aligned 512-byte sector) + env, then SHA-256 read-back
verifies ALL regions and disables OTA.

Safe to re-run (idempotent: it rebuilds blobs from the current device each time;
env is read fresh, so the gate is re-applied onto whatever env is present).

  python finish_install.py --serial <ip:port> --dry-run
  python finish_install.py --serial <ip:port> --yes
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import flash_to_coreelec as F  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--port", type=int, default=5599)
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)
    dry = not a.yes
    g = F.Ctx(a.serial, dry, a.port)

    print(f"=== finish install (serial={a.serial} mode={'DRY-RUN' if dry else 'REAL WRITE'}) ===")
    F.require_artifacts()
    if g.getprop("ro.product.device") != "twilight":
        sys.exit("device != twilight -- abort")
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
    print(f"  device=twilight root=ok pipefail={g.pipefail} GPT=128 active={active} CE={ce_slot}")

    g.build_target_blobs(ce_slot)   # regenerates env_target.bin + misc_sector.bin from device
    if dry:
        print("\n-- would write (GPT already present -> skip_gpt; NO userdata SB wipe) --")
        print("   kernel+dtb (push+dd) ; misc (b64) ; env/gate (push+dd) ;")
        print("   CE_FLASH/CE_STORAGE (nc) ; then verify_writes (SHA-256 all) + disable OTA")
        print("   userdata is left untouched (already sized) -> the env gate survives.")
        print("\nDRY-RUN only. Re-run with --yes.")
        return

    try:
        # skip_sbwipe: do NOT wipe the userdata SB here. Userdata is already the right
        # size by the time you're "finishing", and a wipe that takes triggers a recovery
        # factory-reset that resets the env -> wipes the gate we just wrote (re-gate loop).
        g.write_all(ce_slot, skip_gpt=True, skip_sbwipe=True)
    finally:
        g.adb("forward", "--remove", f"tcp:{g.port}", capture_output=True)

    g.verify_writes(ce_slot)
    g.disable_ota()
    print("\n=== completion done ===")
    print("  normal reboot -> Android (default); 'Reboot to CoreELEC' app -> CoreELEC")


if __name__ == "__main__":
    main()
