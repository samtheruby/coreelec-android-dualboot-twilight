#!/usr/bin/env python3
"""
Install the toolbox_export Magisk module on the Android side, over adb.

It copies, on each Android boot, the decrypted Bluetooth pairings
(/data/misc/bluedroid/bt_config.conf) + the WiFi/BT MAC to /flash (CE_FLASH),
so the CoreELEC-side "CoreELEC Toolbox" addon can sync BT remotes into CoreELEC
(whose userdata it can't read -- Android FBE encryption). Generic to any
Android+CoreELEC internal dual-boot; no Xiaomi specifics.

Run AFTER the dual-boot install + first Android boot (userdata reformats during
the install, which would erase the module otherwise). Reboot to activate.

  python install_toolbox_export.py --serial <ip:port>            # install
  python install_toolbox_export.py --serial <ip:port> --verify    # check (after reboot)
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
# module source lives at ../app/toolbox_export (repo) or ../toolbox_export (bundle)
MOD = next((p for p in (os.path.join(HERE, "..", "app", "toolbox_export"),
                        os.path.join(HERE, "..", "toolbox_export"))
            if os.path.exists(os.path.join(p, "module.prop"))), None)
MODID = "toolbox_export"
MDIR = f"/data/adb/modules/{MODID}"


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out",
                        "su -c '" + cmd.replace("'", "'\\''") + "'"], capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    if a.verify:
        verify(a.serial)
        return
    if MOD is None:
        sys.exit("toolbox_export module source not found (app/toolbox_export)")
    if "uid=0" not in su(a.serial, "id")[0]:
        sys.exit("no root")
    if not su(a.serial, "[ -d /data/adb/magisk ] && echo y")[0].strip() == "y":
        sys.exit("/data/adb/magisk not found -- Magisk-rooted + booted into Android required")

    for f in ("module.prop", "service.sh"):
        r = subprocess.run(["adb", "-s", a.serial, "push", os.path.join(MOD, f),
                            f"/data/local/tmp/{f}"], capture_output=True)
        if r.returncode != 0:
            sys.exit("push failed: " + r.stderr.decode("utf-8", "replace"))
        print(f"  pushed {f}")
    script = (f"set -e; mkdir -p {MDIR}; "
              f"cp /data/local/tmp/module.prop {MDIR}/module.prop; "
              f"cp /data/local/tmp/service.sh {MDIR}/service.sh; "
              f"chmod 0755 {MDIR}/service.sh; chmod 0644 {MDIR}/module.prop; "
              f"rm -f /data/local/tmp/module.prop /data/local/tmp/service.sh; echo placed")
    o, rc = su(a.serial, script)
    if rc != 0 or "placed" not in o:
        sys.exit("module placement failed: " + o)
    print(f"  module placed in {MDIR}")
    print(f"\nInstalled '{MODID}'. Reboot to run its first export:")
    print(f"  adb -s {a.serial} reboot")
    print(f"Verify after reboot: python install_toolbox_export.py --serial {a.serial} --verify")


def verify(serial):
    out, _ = su(serial, (
        f"echo MODULE:; ls {MDIR} 2>/dev/null || echo '(not installed)'; "
        "echo EXPORT_ON_FLASH:; mkdir -p /mnt/tbx; "
        "mount -t vfat -o ro /dev/block/by-name/CE_FLASH /mnt/tbx 2>/dev/null; "
        "ls -la /mnt/tbx/android_bt_config.conf /mnt/tbx/android_macs.conf 2>/dev/null "
        "|| echo '(no export yet -- reboot Android once)'; "
        "umount /mnt/tbx 2>/dev/null; "
        f"echo LOG:; tail -n 8 {MDIR}/toolbox_export.log 2>/dev/null || echo '(no log yet -- reboot first)'"
    ))
    print(out)


if __name__ == "__main__":
    main()
