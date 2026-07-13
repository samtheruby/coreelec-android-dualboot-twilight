#!/usr/bin/env python3
"""
Write the /flash recovery files that let the internal dual-boot survive CoreELEC
OS updates. Run from Android AFTER first boot (CE_FLASH is mountable then), or via
install.py stage2.

Files written to /flash (= CE_FLASH, which the CE update does NOT erase):
  ce_slot.conf      CE_SLOT=a|b   -- slot hint for user-update.sh (fallback)
  env_dualboot.bin  precomputed GATED u-boot env (boot_ce=1). A CE update resets
                    bootcmd to stock (drops our boot_ce gate); the update-hook
                    (user-update.sh, runs in the initramfs without fw_setenv)
                    dd's this image back -> gate restored + auto-enters the new CE.
  user-update.sh    latest hook (also baked into ce_flash.img; refreshed here)

  python deploy_flash_recovery.py --serial <serial>
"""
import argparse, hashlib, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool  # noqa: E402
import bundle   # noqa: E402  -- SHA256SUMS.txt check on the hook we install

HOOK = next((p for p in (os.path.join(HERE, "..", "payload", "flash", "user-update.sh"),
                         os.path.join(HERE, "..", "flash", "user-update.sh"))
             if os.path.exists(p)), None)

MNT = "/mnt/ceflash"


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out", "su -c '" + cmd.replace("'", "'\\''") + "'"],
                       capture_output=True)
    return r.stdout, r.returncode


def push(serial, local, remote):
    return subprocess.run(["adb", "-s", serial, "push", local, remote], capture_output=True).returncode


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify_on_flash(serial, want):
    """Re-mount CE_FLASH READ-ONLY and hash what is actually on it. `want` maps filename ->
    expected sha256.

    This exists because the write cannot be trusted on its own. If the rw mount fails, `cp`
    does not fail -- it writes into /mnt/ceflash as an ordinary directory on the root
    filesystem, `ls` then lists the files perfectly happily, and the whole thing reports
    success while CE_FLASH stays empty. Nobody finds out until a CoreELEC update rewrites
    bootcmd and the hook that was supposed to restore the gate turns out to have never been
    there. So: unmount, mount again read-only, and hash the bytes off the partition."""
    out, _ = su(serial, (
        f"mkdir -p {MNT}; umount {MNT} 2>/dev/null; "
        f"mount -t vfat -o ro /dev/block/by-name/CE_FLASH {MNT} || {{ echo NOMOUNT; exit 1; }}; "
        + "".join(f"sha256sum {MNT}/{n}; " for n in want) +
        f"umount {MNT} 2>/dev/null; echo VERIFIED"))
    text = out.decode("utf-8", "replace")
    if "VERIFIED" not in text or "NOMOUNT" in text:
        sys.exit(f"could not re-mount CE_FLASH to verify the files:\n{text.strip()}")
    got = {}
    for ln in text.splitlines():
        p = ln.split()
        if len(p) == 2 and p[1].startswith(MNT + "/"):
            got[os.path.basename(p[1])] = p[0].lower()
    bad = [n for n, h in want.items() if got.get(n) != h]
    for n, h in want.items():
        mark = "OK  " if got.get(n) == h else "FAIL"
        print(f"  {mark} /flash/{n}  {got.get(n, '(missing)')[:16]}")
    if bad:
        sys.exit(f"/flash verify FAILED for {bad} -- the file(s) are NOT on CE_FLASH "
                 f"(a failed mount would write them to the root filesystem instead, where "
                 f"they vanish). The dual-boot would not survive a CoreELEC OS update.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick the only device")
    ap.add_argument("--default", choices=["coreelec", "android"], default=None,
                    help="boot default to bake into env_dualboot.bin (default: keep current)")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)
    s = a.serial

    # build env_dualboot.bin from the device's current (gated) env
    raw, _ = su(s, "dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null")
    raw = raw[:envtool.ENV_SIZE]
    if len(raw) < envtool.ENV_SIZE or not envtool.crc_ok(raw)[0]:
        sys.exit("env read/CRC invalid -- run the installer first")
    d = envtool.parse(raw)
    g = d.get("bootcefromemmc", "")
    ce_slot = "_a" if "imgread kernel boot_a" in g else ("_b" if "imgread kernel boot_b" in g else None)
    if not ce_slot:
        sys.exit("no boot_ce gate in env -- not a dual-boot unit")
    bc = d.get("bootcmd", "")
    cur = "coreelec" if ("run bootcefromemmc; fi; run storeboot" in bc and "boot_ce} = 1" not in bc) else "android"
    default = a.default or cur
    img = envtool.apply_gate(raw, ce_slot, default)
    if default == "android":
        img = envtool.set_boot_ce(img, 1)   # android-default: auto-enter CE after an update
    # (coreelec-default already auto-enters the new CE since CE is the default)
    assert envtool.crc_ok(img)[0]
    slot = ce_slot[-1]
    print(f"  env_dualboot.bin default = {default}")

    # The hook IS the update-survival mechanism -- a CoreELEC update resets bootcmd to
    # stock, and user-update.sh is what dd's the gated env back. Missing it used to be
    # silent (HOOK=None, and the copy simply skipped).
    if HOOK is None:
        sys.exit("user-update.sh not found (payload/flash/ or flash/) -- that hook is what "
                 "restores the boot gate after a CoreELEC OS update. Refusing to deploy a "
                 "recovery set without it.")
    bundle.verify(HOOK)                      # it runs as root out of the initramfs
    conf = f"CE_SLOT={slot}\n".encode()
    hook_bytes = open(HOOK, "rb").read()

    tmp = os.path.join(HERE, "_envdb.bin")
    open(tmp, "wb").write(img)
    ok = push(s, tmp, "/data/local/tmp/_envdb.bin") == 0
    os.remove(tmp)
    if not ok:
        sys.exit("push env failed")
    if push(s, HOOK, "/data/local/tmp/_uu.sh") != 0:
        sys.exit("push hook failed")

    # Fail-closed: `set -e` plus an explicit mount check. Without both, a failed mount left
    # cp writing into /mnt/ceflash as a plain directory on the root filesystem and the old
    # script still printed DONE (`;`-joined, unconditional echo).
    script = (
        "set -e; "
        f"mkdir -p {MNT}; umount {MNT} 2>/dev/null || true; "
        f"mount -t vfat -o rw /dev/block/by-name/CE_FLASH {MNT}; "
        f"grep -qs ' {MNT} ' /proc/mounts || {{ echo NOT_A_MOUNT; exit 1; }}; "
        f"cp /data/local/tmp/_envdb.bin {MNT}/env_dualboot.bin; "
        f"printf 'CE_SLOT={slot}\\n' > {MNT}/ce_slot.conf; "
        f"cp /data/local/tmp/_uu.sh {MNT}/user-update.sh; chmod 0755 {MNT}/user-update.sh; "
        f"sync; umount {MNT}; "
        "rm -f /data/local/tmp/_envdb.bin /data/local/tmp/_uu.sh; echo WROTE")
    out, rc = su(s, script)
    text = out.decode("utf-8", "replace")
    if rc != 0 or "WROTE" not in text:
        sys.exit(f"/flash write failed (rc={rc}): {text.strip() or '(no output)'}")

    # Read the bytes back off the partition -- see verify_on_flash().
    print("-- verify (re-mounted read-only, hashed off CE_FLASH) --")
    verify_on_flash(s, {"env_dualboot.bin": sha256(img),
                        "ce_slot.conf": sha256(conf),
                        "user-update.sh": sha256(hook_bytes)})
    print(f"OK -- /flash recovery files verified on CE_FLASH "
          f"(CE_SLOT={slot}, env_dualboot.bin gated+boot_ce=1).")


if __name__ == "__main__":
    main()
