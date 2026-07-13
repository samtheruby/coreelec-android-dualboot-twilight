#!/usr/bin/env python3
"""
Block the Xiaomi OTA updater on the twilight unit, over adb (install.py stage2a).

An applied A/B OTA writes boot+dtbo to the INACTIVE slot -- which is exactly where CoreELEC
lives -- and flips the active slot, clobbering the dual-boot. So the Xiaomi updater is
disabled, persistently.

The DURABLE block is the `pm disable-user` applied HERE, from adb, with the framework fully
up: a package's disabled state survives reboots (and survives `pm clear` -- measured, not
assumed). The Magisk module dropped alongside it is only a re-assert for the case where
something turns the updater back on.

That distinction used to be theory. In the field it was neither verified nor true: this
script ran `pm disable-user ... 2>/dev/null || true` -- discarding the failure of the ONE
command that matters -- and then never read the state back. On the dev stick the updater sat
ENABLED through 17 boots while the module's own log recorded a disable failure every single
time, and nothing ever said so. This stage now proves its own end state before it claims
success.

  python install_blockota.py --serial <serial>          # install + block + VERIFY
  python install_blockota.py --serial <serial> --verify  # check an existing install
  python install_blockota.py --serial <serial> --revert  # re-enable the updater
"""
import argparse, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import magisk_module as M  # noqa: E402

MODID = "blockota_twilight"
MDIR = f"/data/adb/modules/{MODID}"
PKG = "com.xiaomi.mitv.updateservice"
# module source: repo layout, then shipped-bundle layout
MOD = M.find_source(os.path.join(HERE, "..", "modules", "blockota"),
                    os.path.join(HERE, "..", "blockota"))
# Globals the updater flow also consults. Both, not just the first one: the boot module sets
# both, while this script used to set only ota_disable_automatic_update -- so auto_update_system
# stayed unset (null) on the dev stick, because the module's boot-time attempt always failed.
GLOBALS = {"ota_disable_automatic_update": "1", "auto_update_system": "0"}


def is_blocked(serial):
    """True iff the updater is really disabled, per the package manager itself."""
    out, _ = M.su(serial, "pm list packages -d")
    return f"package:{PKG}" in out.split()


def block(serial, tries=3):
    """Disable the updater and PROVE it. Returns True iff it ended up disabled.

    `pm clear` runs after the disable, and that order is deliberate and measured: clearing
    does NOT reset the enabled state (verified on the device), so the disable holds, and
    clearing afterwards also wipes any OTA already downloaded/staged by the app.
    """
    for attempt in range(1, tries + 1):
        out, _ = M.su(serial, f"pm disable-user --user 0 {PKG}")
        if is_blocked(serial):
            print(f"  {PKG}: disabled ({out.strip().splitlines()[-1] if out.strip() else 'ok'})")
            out, _ = M.su(serial, f"pm clear --user 0 {PKG}")
            print(f"  {PKG}: data cleared ({out.strip() or 'no output'}) -- any staged OTA is gone")
            if not is_blocked(serial):        # belt and braces; measured not to happen
                print("  WARNING: `pm clear` re-enabled the package -- re-disabling")
                M.su(serial, f"pm disable-user --user 0 {PKG}")
            return is_blocked(serial)
        print(f"  disable did not take (attempt {attempt}/{tries}): "
              f"{out.strip() or '(no output)'}")
        time.sleep(2)
    return False


def apply_globals(serial):
    for k, v in GLOBALS.items():
        M.su(serial, f"settings put global {k} {v}")
    got = {k: M.su(serial, f"settings get global {k}")[0].strip() for k in GLOBALS}
    for k, v in GLOBALS.items():
        print(f"  {'OK  ' if got[k] == v else 'WARN'} settings global {k}={got[k]} (want {v})")
    return [k for k, v in GLOBALS.items() if got[k] != v]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick the only device")
    ap.add_argument("--verify", action="store_true", help="report the current block state")
    ap.add_argument("--revert", action="store_true", help="re-enable the Xiaomi updater")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    if a.verify:
        sys.exit(0 if verify(a.serial) else 1)

    M.require_rooted_android(a.serial)

    if a.revert:
        out, _ = M.su(a.serial, f"pm enable --user 0 {PKG}")
        print(f"  {out.strip() or '(no output)'}")
        print(f"Remove the module to fully undo:  adb -s {a.serial} shell su -c 'rm -rf {MDIR}'")
        return

    # Nothing to block on a unit without the Xiaomi updater. That is a legitimate, complete
    # outcome (the stage's goal is "no Xiaomi OTA"), not a failure -- so say so and stop,
    # rather than installing a module that can only ever no-op.
    if PKG not in M.su(a.serial, f"pm path {PKG}")[0]:
        print(f"{PKG} is not installed on this unit -- there is no Xiaomi updater to block.")
        print("Nothing to do. (stage2a is Xiaomi-only; the generic Google TV OTA block is "
              "blockgms, installed by stage2.)")
        return

    M.install(a.serial, MOD, MODID)

    # THE point of this stage. Applied from adb, where the framework is up -- the module's
    # boot-time context cannot do this reliably (see modules/blockota/service.sh).
    print(f"\n-- disable the Xiaomi updater (persistent; this is the durable block) --")
    if not block(a.serial):
        sys.exit(f"FAILED to disable {PKG} -- it is still enabled, so a Xiaomi OTA could "
                 f"still overwrite the CoreELEC slot. The module was placed but it cannot "
                 f"do this itself at boot. Retry, or disable it by hand:\n"
                 f"  adb -s {a.serial} shell su -c 'pm disable-user --user 0 {PKG}'")
    print("\n-- automatic-update globals --")
    apply_globals(a.serial)

    print(f"\nInstalled '{MODID}' and VERIFIED the updater is disabled.")
    print(f"  Reboot to activate the module's boot-time re-assert: adb -s {a.serial} reboot")
    print(f"  Check any time: python install_blockota.py --serial {a.serial} --verify")


def verify(serial):
    """True iff the updater is blocked. Prints the evidence."""
    blocked = is_blocked(serial)
    print(f"  {'OK  ' if blocked else 'FAIL'} {PKG} disabled: {blocked}")
    for k, v in GLOBALS.items():
        got = M.su(serial, f"settings get global {k}")[0].strip()
        print(f"  {'OK  ' if got == v else 'warn'} settings global {k}={got} (want {v})")
    mod, _ = M.su(serial, f"ls {MDIR} 2>/dev/null")
    print(f"  module: {' '.join(mod.split()) or '(not installed)'}")
    log, _ = M.su(serial, f"tail -n 6 {MDIR}/blockota.log 2>/dev/null")
    print("  log:\n" + ("\n".join("    " + l for l in log.strip().splitlines())
                        or "    (no log yet -- reboot first)"))
    if not blocked:
        print(f"\n  The Xiaomi updater is ENABLED. A Xiaomi OTA could overwrite the CoreELEC "
              f"slot.\n  Fix: python install_blockota.py --serial {serial}")
    return blocked


if __name__ == "__main__":
    main()
