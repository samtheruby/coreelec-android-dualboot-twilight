#!/usr/bin/env python3
"""
Re-assert the boot_ce gate in the u-boot env, from Android.

Two cases, both handled:
  * Gate partially present (e.g. a CoreELEC OS update rewrote `bootcmd` to a stock
    version that DROPS our `if ${boot_ce} = 1 ... run bootcefromemmc` check, while
    bootcefromemmc itself survives) -> re-apply the gate (apply_gate).
  * Gate FULLY gone (e.g. the stage1 recovery factory-reset reset the env to stock,
    dropping the gate AND the generic boot helpers the gated bootcmd runs) -> rebuild
    the whole gated env from the current env via build_target_env (helpers + gate),
    deriving the CE slot from the active Android slot.
Then optionally set boot_ce, write via push+dd (env is non-carve -> reliable), verify.

The CE update-hook (user-update.sh) runs in the initramfs where fw_setenv is
unavailable, so it cannot re-assert the env itself. Run this from Android after a
CoreELEC update if the switcher stops working.

  python reassert_env_gate.py --serial <ip:port>             # re-gate, keep boot_ce
  python reassert_env_gate.py --serial <ip:port> --boot-ce 1  # re-gate + boot CE next reboot
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool  # noqa: E402


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out", "su -c '" + cmd.replace("'", "'\\''") + "'"],
                       capture_output=True)
    return r.stdout, r.returncode


def read_env(serial):
    raw, _ = su(serial, "dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null")
    return raw[:envtool.ENV_SIZE]


def getprop(serial, p):
    out, _ = su(serial, f"getprop {p}")
    return out.decode("utf-8", "replace").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--boot-ce", choices=["0", "1"], default=None)
    ap.add_argument("--default", choices=["coreelec", "android"], default=None,
                    help="which OS a normal reboot boots. Default: keep current direction.")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)

    raw = read_env(a.serial)
    if len(raw) < envtool.ENV_SIZE or not envtool.crc_ok(raw)[0]:
        sys.exit("env read/CRC invalid")
    d = envtool.parse(raw)
    g = d.get("bootcefromemmc", "")
    ce_slot = "_a" if "imgread kernel boot_a" in g else ("_b" if "imgread kernel boot_b" in g else None)

    if ce_slot:
        # gate present -> re-assert it, preserving the current direction unless --default
        # overrides. apply_gate is enough here (the generic helpers are still in the env).
        bc = d.get("bootcmd", "")
        cur_default = "coreelec" if ("run bootcefromemmc; fi; run storeboot" in bc
                                     and "boot_ce} = 1" not in bc) else "android"
        default = a.default or cur_default
        print(f"CE slot = {ce_slot} | gate present: yes | default: {cur_default} -> {default}")
        new = envtool.apply_gate(raw, ce_slot, default)
    else:
        # NO gate. A recovery factory-reset (the one stage1 arms to reformat userdata)
        # resets the u-boot env to stock -- dropping the gate AND the generic boot helpers
        # (bootfromsd/usb...) that the gated bootcmd runs. So rebuild the FULL gated env
        # from the current env via build_target_env (helpers + gate). Derive the CE slot
        # from the active Android slot.
        import build_env
        active = getprop(a.serial, "ro.boot.slot_suffix")
        ce_slot = {"_a": "_b", "_b": "_a"}.get(active)
        if not ce_slot:
            sys.exit(f"no gate in env AND bad slot_suffix '{active}' -- cannot rebuild gate")
        if getprop(a.serial, "ro.product.device") != "twilight":
            sys.exit("device != twilight -- refusing to build a twilight env")
        default = a.default or "android"
        print(f"CE slot = {ce_slot} (from active {active}) | gate present: no "
              f"-> rebuilding full env (generic helpers + gate) | default: {default}")
        new = build_env.build_target_env(raw, ce_slot, default)

    if a.boot_ce is not None:
        new = envtool.set_boot_ce(new, int(a.boot_ce))
    if not envtool.crc_ok(new)[0]:
        sys.exit("serialize produced bad CRC -- aborting")

    tmp = os.path.join(HERE, "_envr.bin")
    open(tmp, "wb").write(new)
    subprocess.run(["adb", "-s", a.serial, "push", tmp, "/data/local/tmp/_envr.bin"], capture_output=True)
    os.remove(tmp)
    out, rc = su(a.serial, "dd if=/data/local/tmp/_envr.bin of=/dev/block/by-name/env conv=fsync 2>&1; "
                           "rm -f /data/local/tmp/_envr.bin; echo done")
    if rc != 0:
        sys.exit("env write failed: " + out.decode("utf-8", "replace"))

    v = read_env(a.serial)
    dv = envtool.parse(v) if envtool.crc_ok(v)[0] else {}
    bcn = dv.get("bootcmd", "")
    now = "bootcefromemmc" in bcn and (("boot_ce} = 1" in bcn)
                                       or ("run bootcefromemmc; fi; run storeboot" in bcn))
    print(f"bootcmd gate present after:  {now} | default={default} | boot_ce={dv.get('boot_ce')}")
    if not now:
        sys.exit("FAILED to re-assert gate")
    if default == "coreelec":
        print("OK -- normal reboot now boots CoreELEC; CoreELEC's 'reboot to eMMC/nand' -> Android.")
    else:
        print("OK -- normal reboot boots Android" + ("; boot_ce=1 -> CoreELEC next reboot" if dv.get("boot_ce") == "1" else "; app -> CoreELEC"))


if __name__ == "__main__":
    main()
