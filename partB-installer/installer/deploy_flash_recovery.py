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

  python deploy_flash_recovery.py --serial <ip:port>
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool  # noqa: E402

HOOK = next((p for p in (os.path.join(HERE, "..", "payload", "flash", "user-update.sh"),
                         os.path.join(HERE, "..", "flash", "user-update.sh"))
             if os.path.exists(p)), None)


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out", "su -c '" + cmd.replace("'", "'\\''") + "'"],
                       capture_output=True)
    return r.stdout, r.returncode


def push(serial, local, remote):
    return subprocess.run(["adb", "-s", serial, "push", local, remote], capture_output=True).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
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

    tmp = os.path.join(HERE, "_envdb.bin")
    open(tmp, "wb").write(img)
    if push(s, tmp, "/data/local/tmp/_envdb.bin") != 0:
        os.remove(tmp); sys.exit("push env failed")
    os.remove(tmp)
    if HOOK and push(s, HOOK, "/data/local/tmp/_uu.sh") != 0:
        sys.exit("push hook failed")

    script = (
        "mkdir -p /mnt/ceflash; "
        "mount -t vfat -o rw /dev/block/by-name/CE_FLASH /mnt/ceflash 2>/dev/null; "
        "mount -o rw,remount /mnt/ceflash 2>/dev/null; "
        "cp /data/local/tmp/_envdb.bin /mnt/ceflash/env_dualboot.bin; "
        f"printf 'CE_SLOT={slot}\\n' > /mnt/ceflash/ce_slot.conf; "
        "[ -f /data/local/tmp/_uu.sh ] && { cp /data/local/tmp/_uu.sh /mnt/ceflash/user-update.sh; chmod 0755 /mnt/ceflash/user-update.sh; }; "
        "sync; ls -la /mnt/ceflash/env_dualboot.bin /mnt/ceflash/ce_slot.conf /mnt/ceflash/user-update.sh; "
        "umount /mnt/ceflash 2>/dev/null; rm -f /data/local/tmp/_envdb.bin /data/local/tmp/_uu.sh; echo DONE")
    out, rc = su(s, script)
    print(out.decode("utf-8", "replace"))
    if "DONE" not in out.decode("utf-8", "replace"):
        sys.exit("deploy failed")
    print(f"OK -- /flash recovery files written (CE_SLOT={slot}, env_dualboot.bin gated+boot_ce=1).")


if __name__ == "__main__":
    main()
