#!/usr/bin/env python3
"""
Staged installer/orchestrator for the CoreELEC internal dual-boot.

Two execution contexts: ANDROID phase (--serial, adb) and COREELEC phase
(--host, ssh, only after first CoreELEC boot). [Xiaomi]=twilight-only,
[generic]=any Google TV / CoreELEC box.

  ANDROID phase (--serial):
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
  python install.py stage1  --serial <ip:port> --yes
  python install.py stage2  --serial <ip:port>
  python install.py stage2a --serial <ip:port>
  python install.py stage3  --host <coreelec-ip>          # device booted in CoreELEC
  python install.py all     --serial <ip:port> --yes      # stage0+stage1, guides the rest
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


# ---- stage 0: preflight + backups (flash_to_coreelec dry-run does both) -------
def stage0(a):
    print("== stage0: preflight + PC-side backups (read-only) ==")
    return run("flash_to_coreelec.py", "--serial", a.serial, "--dry-run")


# ---- stage 1: core destructive install ---------------------------------------
def stage1(a):
    print("== stage1: CORE install (destructive) ==")
    args = ["--serial", a.serial, "--default", a.default] + (["--yes"] if a.yes else ["--dry-run"])
    rc = run("flash_to_coreelec.py", *args)
    if rc == 0 and a.yes:
        print("\nstage1 done. The NEXT reboot enters recovery and reformats userdata")
        print("(factory-reset-like) to the new size, then boots Android. Reboot now,")
        print("let it finish the wipe + Android first-boot setup, re-enable ADB, then stage2:")
        print(f"  adb -s {a.serial} reboot")
        print(f"  python install.py stage2 --serial <new ip:port>")
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
    ap.add_argument("stage", choices=["stage0", "stage1", "stage2", "stage2a",
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
    a = ap.parse_args()

    if a.stage in {"stage0", "stage1", "stage2", "stage2a", "verify", "all"}:
        import adb_serial
        a.serial = adb_serial.resolve(a.serial)

    if a.stage == "all":
        print("Running stage0 + stage1. After stage1 reboot into Android and re-run:")
        print("  python install.py stage2 --serial <ip:port>     (then stage2a optional)")
        print("Then boot CoreELEC and:  python install.py stage3 --host <coreelec-ip>")
        stage0(a)
        sys.exit(stage1(a))
    sys.exit({"stage0": stage0, "stage1": stage1, "stage2": stage2, "stage2a": stage2a,
              "stage3": stage3, "verify": verify}[a.stage](a))


if __name__ == "__main__":
    main()
