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

  python install_toolbox_export.py --serial <serial>            # install
  python install_toolbox_export.py --serial <serial> --verify    # check (after reboot)
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import magisk_module as M  # noqa: E402

MODID = "toolbox_export"
MDIR = f"/data/adb/modules/{MODID}"
# module source: repo layout, then shipped-bundle layout
MOD = M.find_source(os.path.join(HERE, "..", "modules", "toolbox_export"),
                    os.path.join(HERE, "..", "toolbox_export"))


def su(serial, cmd):
    return M.su(serial, cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick the only device")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    if a.verify:
        verify(a.serial)
        return
    M.require_rooted_android(a.serial)
    M.install(a.serial, MOD, MODID)
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
