#!/usr/bin/env python3
"""
Install the Block-OTA Magisk module on the twilight unit, over adb.

Run this AFTER the dual-boot install + first Android boot (the install reformats
userdata, which would erase the module otherwise). It places the module directly
into /data/adb/modules/<id>/ (Magisk loads it on next boot); no flashable zip
needed. Then reboot to activate.

  python install_blockota.py --serial <ip:port>          # install
  python install_blockota.py --serial <ip:port> --verify  # check (after reboot)
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
# module source lives at ../app/blockota (repo) or ../blockota (shipped bundle)
MOD = next((p for p in (os.path.join(HERE, "..", "app", "blockota"),
                        os.path.join(HERE, "..", "blockota"))
            if os.path.exists(os.path.join(p, "module.prop"))), None)
MODID = "blockota_twilight"
MDIR = f"/data/adb/modules/{MODID}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)
    g = (a.serial,)

    if a.verify:
        verify(a.serial)
        return

    # sanity: Magisk present
    if "uid=0" not in su(a.serial, "id")[0]:
        sys.exit("no root")
    if not su(a.serial, "[ -d /data/adb/magisk ] && echo y")[0].strip() == "y":
        sys.exit("/data/adb/magisk not found -- is this unit Magisk-rooted + booted into Android?")

    # push module files to a temp dir, then root-copy into the modules tree
    for f in ("module.prop", "service.sh"):
        src = os.path.join(MOD, f)
        r = subprocess.run(["adb", "-s", a.serial, "push", src, f"/data/local/tmp/{f}"],
                           capture_output=True)
        if r.returncode != 0:
            sys.exit("push failed: " + r.stderr.decode("utf-8", "replace"))
        print(f"  pushed {f}")

    script = (
        f"set -e; mkdir -p {MDIR}; "
        f"cp /data/local/tmp/module.prop {MDIR}/module.prop; "
        f"cp /data/local/tmp/service.sh {MDIR}/service.sh; "
        f"chmod 0755 {MDIR}/service.sh; chmod 0644 {MDIR}/module.prop; "
        f"rm -f /data/local/tmp/module.prop /data/local/tmp/service.sh; "
        # apply once now too (so it takes effect before the activating reboot)
        f"pm disable-user --user 0 com.xiaomi.mitv.updateservice 2>/dev/null || true; "
        f"pm clear --user 0 com.xiaomi.mitv.updateservice 2>/dev/null || true; "
        f"settings put global ota_disable_automatic_update 1 2>/dev/null || true; "
        f"ls -la {MDIR}"
    )
    out, rc = su(a.serial, script)
    print(out)
    if rc != 0:
        sys.exit("module install failed")
    print(f"\nInstalled module '{MODID}'. Reboot to activate its boot-time service:")
    print(f"  adb -s {a.serial} reboot")
    print(f"Then verify: python install_blockota.py --serial {a.serial} --verify")


def verify(serial):
    out, _ = su(serial, (
        f"echo MODULE:; ls {MDIR} 2>/dev/null; "
        f"echo STATE:; pm list packages -d | grep com.xiaomi.mitv.updateservice || echo '(updater not in disabled list!)'; "
        f"echo SETTING:; settings get global ota_disable_automatic_update; "
        f"echo LOG:; tail -n 8 {MDIR}/blockota.log 2>/dev/null || echo '(no log yet -- reboot first)'"
    ))
    print(out)


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out",
                        "su -c '" + cmd.replace("'", "'\\''") + "'"], capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


if __name__ == "__main__":
    main()
