#!/usr/bin/env python3
"""
Staged installer/orchestrator for the CoreELEC internal dual-boot.

Two execution contexts: ANDROID phase (--serial, adb) and COREELEC phase
(--host, ssh, only after first CoreELEC boot). [Xiaomi]=twilight-only,
[generic]=any Google TV / CoreELEC box.

  ANDROID phase (--serial):
    stage_magisk  flash Magisk-patched init_boot_a via fastboot  [Xiaomi]
                   run BEFORE stage0 -- gives root that stage0 requires.
                   Reboots to bootloader, flashes, reboots back to Android.
    stage0  preflight + PC-side backups      [Xiaomi]   read-only + pull
    stage1  CORE install -> first reboot     [Xiaomi]   GPT/CE/kernel/dtb/misc/env
                                                         + arm userdata reformat  DESTRUCTIVE
    --- reboot: recovery reformats userdata, boots Android, then ---
    stage2  apps + modules                   [mixed]    RebootToCoreELEC APK,
              flash-recovery [Xiaomi], toolbox_export module [generic],
              blockgms GMS system-update block [generic]
    stage2a Xiaomi auto-update block         [Xiaomi]   OPTIONAL (blockota)
    verify  layout/env readiness             [Xiaomi]   read-only
  --- reboot into CoreELEC (first CE boot), enable SSH, then ---
  COREELEC phase (--host):
    stage3  CoreELEC-side setup              [mixed]    Toolbox addon [generic],
              Kodi sources PM4K+jamal2362 [generic], Xiaomi remote keymap [Xiaomi,
              auto-detected; --xiaomi forces, --no-keymap skips]

The [generic] stage3 pieces (Toolbox addon, Kodi sources) also run standalone on
any CoreELEC box -- see deploy_toolbox_addon.py / deploy_kodi_sources.py.

Usage:
  python install.py stage_magisk --serial <ip:port>              # auto-find init_boot_patched.img
  python install.py stage_magisk --serial <ip:port> --magisk-img <path>
  python install.py stage1  --serial <ip:port> --yes
  python install.py stage2  --serial <ip:port>
  python install.py stage2a --serial <ip:port>
  python install.py stage3  --host <coreelec-ip>          # device booted in CoreELEC
  python install.py all     --serial <ip:port> --yes      # stage_magisk+stage0+stage1, guides the rest
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "..", "artifacts")
PY = sys.executable


def run(script, *args):
    r = subprocess.run([PY, os.path.join(HERE, script), *args])
    return r.returncode


def adb(serial, *args, **kw):
    return subprocess.run(["adb", "-s", serial, *args], **kw)


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out", "su -c '" + cmd.replace("'", "'\\''") + "'"],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


# ---- stage_magisk: install Magisk APK + flash patched init_boot_a via fastboot -
def stage_magisk(a):
    import time, glob as _glob
    print("== stage_magisk: install Magisk + flash patched init_boot_a ==")

    magisk_dir = os.path.abspath(os.path.join(HERE, "..", "magisk"))

    # ---- A. Install Magisk APK --------------------------------------------------
    apks = sorted(_glob.glob(os.path.join(magisk_dir, "Magisk*.apk")) +
                  _glob.glob(os.path.join(ART, "Magisk*.apk")))
    if apks:
        apk = apks[-1]
        print(f"  installing {os.path.basename(apk)} ...")
        r = adb(a.serial, "install", "-r", apk, capture_output=True)
        out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        if r.returncode == 0:
            print("  Magisk APK installed OK")
        else:
            print(f"  WARNING: APK install failed: {out}")
    else:
        print("  (no Magisk*.apk found in magisk/ or artifacts/ -- skipping APK install)")

    # ---- B. Locate the pre-patched init_boot image ------------------------------
    img = getattr(a, "magisk_img", None) or ""
    if not img:
        r = subprocess.run(["adb", "-s", a.serial, "shell", "getprop", "ro.product.device"],
                           capture_output=True, text=True)
        device = r.stdout.strip()
        dev_name = f"{device}-init_boot-patched.img" if device else ""
        candidates = []
        if dev_name:
            candidates += [os.path.join(magisk_dir, dev_name),
                           os.path.join(ART, dev_name)]
        candidates += [os.path.join(ART, "init_boot_patched.img"),
                       os.path.join(HERE, "..", "init_boot_patched.img")]
        for c in candidates:
            if os.path.exists(c):
                img = os.path.abspath(c)
                break
    if not img:
        return None  # signal to caller: no image found, skip
    if not os.path.exists(img):
        sys.exit(f"Patched init_boot image not found at: {img}\nPass --magisk-img <path>")
    print(f"  image: {img}  ({os.path.getsize(img):,} B)")

    # ---- C. Reboot to fastboot and flash ----------------------------------------
    fs = getattr(a, "fastboot_serial", None) or ""
    fb = ["fastboot"] + (["-s", fs] if fs else [])

    print(f"  rebooting {a.serial!r} into bootloader ...")
    adb(a.serial, "reboot", "bootloader")

    print("  waiting for fastboot device (up to 60 s) ...")
    found = False
    for _ in range(60):
        try:
            r = subprocess.run(fb + ["devices"], capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit("fastboot not found on PATH -- install Android platform-tools")
        devlines = [l for l in r.stdout.splitlines()
                    if l.strip() and not l.startswith("List")]
        if devlines:
            print(f"  fastboot: {devlines[0].strip()}")
            found = True
            break
        time.sleep(1)
    if not found:
        sys.exit("fastboot device did not appear within 60 s -- "
                 "check USB cable and driver (Xiaomi bootloader driver / WinUSB)")

    print("  fastboot flash init_boot_a ...")
    r = subprocess.run(fb + ["flash", "init_boot_a", img])
    if r.returncode != 0:
        sys.exit("fastboot flash init_boot_a FAILED")
    print("  init_boot_a flashed OK")

    # ---- D. Reboot to Android and verify root -----------------------------------
    print("  rebooting to Android ...")
    subprocess.run(fb + ["reboot"])

    print(f"  waiting for ADB {a.serial!r} to reconnect (up to 90 s) ...")
    for _ in range(90):
        r = subprocess.run(["adb", "-s", a.serial, "get-state"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == "device":
            print("  ADB reconnected.")
            time.sleep(5)  # let the system settle before checking root
            root_out, _ = su(a.serial, "id")
            if "uid=0" in root_out:
                print("  Root verified: Magisk is active.")
            else:
                print("  Root not yet confirmed -- open the Magisk app to complete")
                print("  any first-time setup, then verify: adb shell su -c id")
            return 0
        time.sleep(1)
    print("  init_boot_a flashed successfully.")
    print("  ADB did not reconnect on the same serial within 90 s.")
    if ":" in a.serial:
        print("  Device rebooted -- reconnect with: adb connect <ip:port>")
    print("  Then continue: python install.py stage0 --serial <serial>")
    sys.exit(0)


# ---- stage 0: preflight + backups (flash_to_coreelec dry-run does both) -------
def stage0(a):
    print("== stage0: preflight + PC-side backups (read-only) ==")
    return run("flash_to_coreelec.py", "--serial", a.serial, "--dry-run")


# ---- stage 1b: re-install Magisk APK after the stage1 factory reset ---------
def stage1b(a):
    import time, glob as _glob
    print("== stage1b: re-install Magisk APK (factory reset wiped userdata) ==")
    print("  (init_boot_a is still patched -- no fastboot needed)")

    magisk_dir = os.path.abspath(os.path.join(HERE, "..", "magisk"))
    apks = sorted(_glob.glob(os.path.join(magisk_dir, "Magisk*.apk")) +
                  _glob.glob(os.path.join(ART, "Magisk*.apk")))
    if not apks:
        sys.exit("no Magisk*.apk found in magisk/ or artifacts/")
    apk = apks[-1]
    print(f"  installing {os.path.basename(apk)} ...")
    r = adb(a.serial, "install", "-r", apk, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0:
        sys.exit(f"  APK install failed: {out}")
    print("  Magisk APK installed OK")
    print("  Open the Magisk app on the device and complete first-time setup,")
    print("  then approve the root-access dialog for ADB shell.")
    print("  Waiting for root (up to 120 s -- tap Allow on device when prompted) ...")
    for _ in range(120):
        root_out, _ = su(a.serial, "id")
        if "uid=0" in root_out:
            print("  Root verified: Magisk is active.")
            print("\nstage1b done. Run stage2 now:")
            print(f"  python install.py stage2 --serial {a.serial}")
            return 0
        time.sleep(1)
    print("  Root not confirmed within 120 s.")
    print("  Verify manually:  adb shell su -c id")
    print("  Then run:         python install.py stage2")
    return 0


# ---- stage 1: core destructive install ---------------------------------------
def stage1(a):
    print("== stage1: CORE install (destructive) ==")
    args = ["--serial", a.serial, "--default", a.default] + (["--yes"] if a.yes else ["--dry-run"])
    rc = run("flash_to_coreelec.py", *args)
    if rc == 0 and a.yes:
        print("\nstage1 done. The NEXT reboot enters recovery and reformats userdata")
        print("(factory-reset-like) to the new size, then boots Android. Reboot now,")
        print("let it finish the wipe + Android first-boot setup, re-enable ADB, then:")
        print(f"  adb -s {a.serial} reboot")
        print(f"  python install.py stage1b --serial <new ip:port>   # re-install Magisk APK")
        print(f"  python install.py stage2  --serial <new ip:port>   # after root confirmed")
    return rc


# ---- ce_slot.conf: drop the slot file on /flash (belt-and-suspenders) ---------
def write_ce_slot_conf(serial):
    """Mount CE_FLASH, detect CE slot from the env gate, write /flash/ce_slot.conf.
    The hook (user-update.sh v2) reads the slot from the env partition directly, so
    this is a fallback -- but cheap and robust."""
    gate, _ = su(serial, "dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null "
                         "| tr '\\000' '\\n' | grep 'imgread kernel boot_' | head -1")
    slot = "a" if "boot_a" in gate else ("b" if "boot_b" in gate else "")
    if not slot:
        print("  ce_slot.conf: could not detect slot from env -- skipped")
        return
    out, rc = su(serial,
                 "mkdir -p /mnt/ceflash; mount -t vfat -o rw /dev/block/by-name/CE_FLASH /mnt/ceflash 2>/dev/null; "
                 "mount -o rw,remount /mnt/ceflash 2>/dev/null; "
                 f"printf 'CE_SLOT={slot}\\n' > /mnt/ceflash/ce_slot.conf && sync && "
                 "umount /mnt/ceflash 2>/dev/null; echo OK")
    print(f"  ce_slot.conf: CE_SLOT={slot} {'written' if 'OK' in out else 'FAILED: ' + out}")


# ---- stage 2: apps + universal OTA block -------------------------------------
def stage2(a):
    print("== stage2: apps + universal GMS OTA block (Google TV) ==")
    # The stage1 reboot runs a recovery factory-reset (to reformat userdata) which on
    # this SoC resets the u-boot env to stock -- dropping the boot gate AND the generic
    # boot helpers it needs. Re-apply the FULL gate now, post-reset, so it persists.
    # Idempotent: if the env still has the gate, reassert_env_gate just re-asserts it.
    print("-- (re)assert env boot gate (stage1's factory reset clears env) --")
    rc = run("reassert_env_gate.py", "--serial", a.serial, "--default", a.default)
    if rc != 0:
        sys.exit("env gate (re)assert failed -- CoreELEC would be unreachable; fix before continuing")
    apk = os.path.join(ART, "RebootToCoreELEC.apk")
    if not os.path.exists(apk):
        sys.exit("missing RebootToCoreELEC.apk")
    print("-- install RebootToCoreELEC app --")
    r = adb(a.serial, "install", "-r", apk, capture_output=True)
    print("  " + r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    print("-- /flash recovery files (ce_slot.conf, env_dualboot.bin, hook) [Xiaomi] --")
    run("deploy_flash_recovery.py", "--serial", a.serial, "--default", a.default)
    print("-- toolbox_export module: Android->CoreELEC BT/MAC export [generic] --")
    run("install_toolbox_export.py", "--serial", a.serial)
    print("-- blockgms: GMS system-update components [generic Google TV] --")
    rc = run("install_blockgms.py", "--serial", a.serial)
    if rc == 0:
        print("\nstage2 done. Reboot to activate the modules; then optionally stage2a (Xiaomi).")
        print("After rebooting into CoreELEC (first CE boot), run stage3:")
        print("  python install.py stage3 --host <coreelec-ip>")
    return rc


# ---- stage 2a: Xiaomi updater block (optional) -------------------------------
def stage2a(a):
    print("== stage2a: Xiaomi auto-update block (optional, Xiaomi only) ==")
    return run("install_blockota.py", "--serial", a.serial)


# ---- stage 3: CoreELEC-side setup (device in CoreELEC, SSH/--host) ------------
def stage3(a):
    print("== stage3: CoreELEC-side setup (device booted into CoreELEC, SSH) ==")
    if not a.host:
        sys.exit("stage3 needs --host <coreelec-ip> (boot into CoreELEC, enable SSH)")
    print("-- CoreELEC Toolbox addon: BT-sync / boot-default / WiFi-MAC [generic] --")
    rc = run("deploy_toolbox_addon.py", "--host", a.host)
    print("-- Kodi sources: PM4K + jamal2362 [generic] --")
    run("deploy_kodi_sources.py", "--host", a.host)
    if a.no_keymap:
        print("-- Xiaomi remote keymap: skipped (--no-keymap) --")
    else:
        tag = "forced (--xiaomi)" if a.xiaomi else "auto-detect"
        print(f"-- Xiaomi remote keymap [{tag}] --")
        km = ["--host", a.host] + ([] if a.xiaomi else ["--auto"])
        run("deploy_remote_keymap.py", *km)
    print("\nstage3 done. Install PM4K (script.plexmod) / TinyPPI (script.tinyppi) from the "
          "new sources:\n  Add-ons > Install from zip file > <source>.")
    return rc


# ---- verify: layout/env readiness (Android, read-only) -----------------------
def verify(a):
    print("== verify: layout/env readiness (read-only) ==")
    if not a.serial:
        sys.exit("verify needs --serial <ip:port> (device in Android)")
    return run("validate_nondestructive.py", "--serial", a.serial)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["stage_magisk", "stage0", "stage1", "stage1b", "stage2", "stage2a",
                                      "stage3", "verify", "all"])
    ap.add_argument("--serial", help="adb serial for the Android stages (ip:port or USB id); "
                    "omit to auto-pick the only attached device")
    ap.add_argument("--host", help="CoreELEC IP (stage3; device booted into CoreELEC)")
    ap.add_argument("--yes", action="store_true", help="perform destructive stage1 writes")
    ap.add_argument("--default", choices=["android", "coreelec"], default="android",
                    help="which OS a normal reboot boots (default android). 'coreelec' = "
                         "CoreELEC default + reboot-to-eMMC/nand -> Android.")
    ap.add_argument("--xiaomi", action="store_true",
                    help="stage3: force the Xiaomi remote keymap (skip auto-detect)")
    ap.add_argument("--no-keymap", dest="no_keymap", action="store_true",
                    help="stage3: skip the Xiaomi remote keymap (generic CoreELEC box)")
    ap.add_argument("--magisk-img", dest="magisk_img", default="",
                    help="stage_magisk: path to Magisk-patched init_boot image "
                         "(auto-found if init_boot_patched.img is in artifacts/ or bundle root)")
    ap.add_argument("--fastboot-serial", dest="fastboot_serial", default="",
                    help="stage_magisk: fastboot device serial (auto-detected if omitted)")
    a = ap.parse_args()

    if a.stage in {"stage_magisk", "stage0", "stage1", "stage1b", "stage2", "stage2a", "verify", "all"}:
        import adb_serial
        a.serial = adb_serial.resolve(a.serial)

    if a.stage == "all":
        print("Running stage_magisk (if image found) + stage0 + stage1.")
        print("After stage1 reboot into Android and re-run:")
        print("  python install.py stage2 --serial <ip:port>     (then stage2a optional)")
        print("Then boot CoreELEC and:  python install.py stage3 --host <coreelec-ip>")
        if stage_magisk(a) is None:
            print("  (stage_magisk skipped: no init_boot_patched.img found in artifacts/ or bundle root)")
        stage0(a)
        sys.exit(stage1(a))
    sys.exit({"stage_magisk": stage_magisk, "stage0": stage0, "stage1": stage1,
              "stage1b": stage1b, "stage2": stage2, "stage2a": stage2a,
              "stage3": stage3, "verify": verify}[a.stage](a))


if __name__ == "__main__":
    main()
