#!/usr/bin/env python3
"""
Install the Block-GMS-System-Update Magisk module on a Google/Android TV box.

Disables ONLY the com.google.android.gms .update.* COMPONENTS (the system-update
feature), not GMS itself -- so Settings "Check for updates" can't fetch/apply an
A/B OTA that would clobber an internal CoreELEC dual-boot, while Play/accounts/
casting keep working. The component-disabled state is PERSISTENT (survives reboots);
this applies it via adb-su (which works, unlike the boot context on some boxes) and
drops the module so it also re-asserts each boot where it can.

  python install_blockgms.py --serial <ip:port>           # install + apply
  python install_blockgms.py --serial <ip:port> --verify   # check
  python install_blockgms.py --serial <ip:port> --revert   # pm enable + (manual) remove module

Reversible: --revert re-enables the components; remove the Magisk module to undo fully.
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = next((p for p in (os.path.join(HERE, "..", "app", "blockgms"),
                        os.path.join(HERE, "..", "blockgms"))
            if os.path.exists(os.path.join(p, "module.prop"))), None)
MODID = "blockgms_sysupdate"
MDIR = f"/data/adb/modules/{MODID}"

GMS = "com.google.android.gms"
COMPS = [
    ".update.SystemUpdateService",
    ".update.SystemUpdateGcmTaskService",
    ".update.SystemUpdatePersistentListenerService",
    ".update.SystemUpdateActivity",           # phone
    ".update.SystemUpdatePanoActivity",       # TV
    ".update.OtaSuggestionActivity",          # phone
    ".update.OtaPanoSetupActivity",           # TV
    ".update.phone.PopupDialog",              # phone
]


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out",
                        "su -c '" + cmd.replace("'", "'\\''") + "'"], capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


def apply_disable(serial, verb):
    """verb = 'disable' or 'enable'. Returns list of (component, ok)."""
    out = []
    for c in COMPS:
        full = f"{GMS}/{GMS}{c}"
        o, _ = su(serial, f"pm {verb} {full}")
        ok = ("new state: disabled" in o) if verb == "disable" else ("new state: enabled" in o)
        out.append((c, ok, o.strip().splitlines()[-1] if o.strip() else ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    if "uid=0" not in su(a.serial, "id")[0]:
        sys.exit("no root")

    if a.verify:
        verify(a.serial)
        return

    if a.revert:
        print("re-enabling GMS system-update components:")
        for c, ok, msg in apply_disable(a.serial, "enable"):
            print(f"  {'OK  ' if ok else 'skip'} {c}  {msg}")
        print(f"\nNow remove the module to fully undo:  adb -s {a.serial} shell su -c 'rm -rf {MDIR}' ; reboot")
        return

    if not su(a.serial, "[ -d /data/adb/magisk ] && echo y")[0].strip() == "y":
        sys.exit("/data/adb/magisk not found -- Magisk-rooted + booted into Android required")
    if GMS not in su(a.serial, f"pm path {GMS}")[0]:
        sys.exit(f"{GMS} not present -- is this a Google/Android TV (GMS) box?")

    # place module
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
    print("  module placed in " + MDIR)

    # apply the persistent component-disable now (this is the durable mechanism)
    print("\ndisabling GMS .update.* components (persistent):")
    any_ok = False
    for c, ok, msg in apply_disable(a.serial, "disable"):
        any_ok = any_ok or ok
        print(f"  {'OK  ' if ok else 'skip'} {c}  {msg}")
    if not any_ok:
        sys.exit("no components disabled -- aborting (none matched / pm rejected). Module placed but inert.")
    print(f"\nInstalled '{MODID}'. Reboot to activate its boot-time re-assert:")
    print(f"  adb -s {a.serial} reboot")
    print(f"Verify: python install_blockgms.py --serial {a.serial} --verify")
    print("If the box ever bootloops after a GMS Play-update: Magisk safe-mode (3 failed boots) "
          "disables modules; or remove with --revert.")


def verify(serial):
    out, _ = su(serial, f"dumpsys package {GMS}")
    dis = out
    print("component states (disabled = blocked):")
    n_dis = 0
    for c in COMPS:
        full = f"{GMS}{c}"
        # a disabled component appears under 'disabledComponents:' in dumpsys
        state = "DISABLED" if full in dis.split("disabledComponents:")[-1].split("enabledComponents:")[0] \
                else ("present" if full in dis else "absent")
        if state == "DISABLED":
            n_dis += 1
        print(f"  {state:<8} {c}")
    print(f"\n{n_dis} component(s) disabled.")
    print("update_engine:", (su(serial, "ps -A | grep update_engine | grep -v grep")[0].strip() or "(not running)"))
    log, _ = su(serial, f"tail -n 12 {MDIR}/blockgms.log 2>/dev/null")
    print("LOG:\n" + (log.strip() or "(no log yet -- reboot first)"))


if __name__ == "__main__":
    main()
