#!/usr/bin/env python3
"""
Install the Block-GMS-System-Update Magisk module on a Google/Android TV box.

Disables ONLY the com.google.android.gms .update.* COMPONENTS (the system-update
feature), not GMS itself -- so Settings "Check for updates" can't fetch/apply an
A/B OTA that would clobber an internal CoreELEC dual-boot, while Play/accounts/
casting keep working. The component-disabled state is PERSISTENT (survives reboots);
this applies it via adb-su (which works, unlike the boot context on some boxes) and
drops the module so it also re-asserts each boot where it can.

  python install_blockgms.py --serial <serial>           # install + apply
  python install_blockgms.py --serial <serial> --verify   # check
  python install_blockgms.py --serial <serial> --revert   # pm enable + (manual) remove module

Reversible: --revert re-enables the components; remove the Magisk module to undo fully.
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import magisk_module as M  # noqa: E402

MODID = "blockgms_sysupdate"
MDIR = f"/data/adb/modules/{MODID}"
# module source: repo layout, then shipped-bundle layout
MOD = M.find_source(os.path.join(HERE, "..", "modules", "blockgms"),
                    os.path.join(HERE, "..", "blockgms"))

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
    return M.su(serial, cmd)


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
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick the only device")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    if a.verify:
        verify(a.serial)
        return

    M.require_rooted_android(a.serial)

    if a.revert:
        print("re-enabling GMS system-update components:")
        for c, ok, msg in apply_disable(a.serial, "enable"):
            print(f"  {'OK  ' if ok else 'skip'} {c}  {msg}")
        print(f"\nNow remove the module to fully undo:  adb -s {a.serial} shell su -c 'rm -rf {MDIR}' ; reboot")
        return

    if GMS not in su(a.serial, f"pm path {GMS}")[0]:
        sys.exit(f"{GMS} not present -- is this a Google/Android TV (GMS) box?")

    M.install(a.serial, MOD, MODID)

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
