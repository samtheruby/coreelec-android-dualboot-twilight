#!/usr/bin/env python3
"""
Restore the `env` and `misc` partitions to their FACTORY dumps, so a previously
modified twilight unit becomes a faithful fresh-stock baseline before re-running
flash_to_coreelec.py. (GPT/userdata are handled separately by restore_stock_gpt.py.)

Source, in priority order:
  1. pulled_backups/env_pre.bin (64 KiB) + misc_pre.bin (32 KiB) -- THIS unit's own
     pre-install env+misc, saved by stage0. Reverses a clean install on the same device
     (restores the unit's exact original env, including its own identity).
  2. device_backups/env_p2.bin + misc_p11.bin -- the dev Phase-0 reference dumps (fallback).

A bad env is not bricking (u-boot falls back to its built-in default and boots
Android). Each write is size-checked against the on-device partition and verified
by read-back (env: CRC + that the gate is absent; misc: factory A/B bytes).

  python restore_env_misc_factory.py --serial <ip:port>          # recon only
  python restore_env_misc_factory.py --serial <ip:port> --yes     # write
"""
import argparse, base64, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PB = os.path.join(HERE, "..", "pulled_backups")            # this unit's stage0 dumps
BK = os.path.join(HERE, "..", "..", "device_backups")      # dev Phase-0 dumps (fallback)
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool, build_env, ab_misc  # noqa: E402

# Prefer THIS unit's pre-install env+misc (stage0) over the dev reference dumps.
if os.path.exists(os.path.join(PB, "env_pre.bin")) and os.path.exists(os.path.join(PB, "misc_pre.bin")):
    ENV_SRC = os.path.join(PB, "env_pre.bin")
    MISC_SRC = os.path.join(PB, "misc_pre.bin")
    SRC_TAG = "pulled_backups (this unit's pre-install env+misc)"
else:
    ENV_SRC = os.path.join(BK, "env_p2.bin")
    MISC_SRC = os.path.join(BK, "misc_p11.bin")
    SRC_TAG = "device_backups (Phase-0 reference dumps)"


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


class Dev:
    def __init__(self, serial):
        self.serial = serial

    def su(self, cmd):
        r = subprocess.run(["adb", "-s", self.serial, "exec-out", f"su -c {shq(cmd)}"],
                           capture_output=True)
        return r.stdout.decode("utf-8", "replace"), r.returncode

    def su_bytes(self, cmd):
        return subprocess.run(["adb", "-s", self.serial, "exec-out", f"su -c {shq(cmd)}"],
                              capture_output=True).stdout

    def pull_raw(self, cmd):
        data = self.su_bytes(cmd + " 2>/dev/null | base64")
        return base64.b64decode(b"".join(bytes(data).split()))

    def getprop(self, p):
        return self.su(f"getprop {p}")[0].strip()

    def part_size(self, name):
        out, _ = self.su(f"blockdev --getsize64 /dev/block/by-name/{name}")
        try:
            return int(out.strip())
        except ValueError:
            return None

    def push(self, local, remote):
        r = subprocess.run(["adb", "-s", self.serial, "push", local, remote],
                           capture_output=True)
        if r.returncode != 0:
            sys.exit("push failed: " + r.stderr.decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)
    d = Dev(a.serial)
    print(f"=== restore env+misc to factory (serial={a.serial} "
          f"mode={'WRITE' if a.yes else 'RECON'}) ===")

    # ---- sources ----
    for p in (ENV_SRC, MISC_SRC):
        if not os.path.exists(p):
            sys.exit(f"missing factory dump: {p}")
    env_src = open(ENV_SRC, "rb").read()
    misc_src = open(MISC_SRC, "rb").read()
    print(f"\n-- factory sources [{SRC_TAG}] --")
    print(f"  {os.path.basename(ENV_SRC):<14} {len(env_src):>10,} B")
    print(f"  {os.path.basename(MISC_SRC):<14} {len(misc_src):>9,} B")

    # env source sanity: first 64K is a valid, UN-gated env
    eb = env_src[:envtool.ENV_SIZE]
    if len(eb) < envtool.ENV_SIZE or not envtool.crc_ok(eb)[0]:
        sys.exit("env_p2.bin first 64K has bad CRC -- refusing")
    ed = envtool.parse(eb)
    if "boot_ce" in ed or "bootcefromemmc" in ed:
        sys.exit("env_p2.bin already contains the gate -- not a factory env, refusing")
    print(f"  env source: crc OK, {len(ed)} keys, no gate (factory)")

    # ---- device ----
    print(f"\n-- device --")
    if d.getprop("ro.product.device") != "twilight":
        sys.exit("device != twilight -- abort")
    if "uid=0" not in d.su("id")[0]:
        sys.exit("su root not available")
    env_psz = d.part_size("env")
    misc_psz = d.part_size("misc")
    print(f"  by-name/env  partition = {env_psz:,} B")
    print(f"  by-name/misc partition = {misc_psz:,} B")
    if env_psz is None or misc_psz is None:
        sys.exit("could not read partition sizes")
    if len(env_src) > env_psz:
        sys.exit(f"env_p2.bin ({len(env_src)}) larger than env partition ({env_psz}) -- refusing")
    if len(misc_src) > misc_psz:
        sys.exit(f"misc_p11.bin ({len(misc_src)}) larger than misc partition ({misc_psz}) -- refusing")

    # ---- current on-device state (for context) ----
    cur_env = d.pull_raw("dd if=/dev/block/by-name/env bs=512 count=128")[:envtool.ENV_SIZE]
    ce = envtool.parse(cur_env) if envtool.crc_ok(cur_env)[0] else {}
    cur_misc = d.pull_raw("dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32")[:32]
    mi = ab_misc.parse(cur_misc)
    print(f"\n-- current on-device --")
    print(f"  env: gate present={'boot_ce' in ce} bootcefromemmc={'bootcefromemmc' in ce}")
    print(f"  misc A/B: a=0x{mi['a_byte']:02x} b=0x{mi['b_byte']:02x} crc_ok={mi['crc_ok']}")

    if not a.yes:
        print(f"\n-- write plan (RECON only) --")
        print(f"   push env_p2.bin  ; dd of=/dev/block/by-name/env  conv=fsync")
        print(f"   push misc_p11.bin; dd of=/dev/block/by-name/misc conv=fsync")
        print("\nRECON only. Re-run with --yes to write.")
        return

    # ---- WRITE ----
    print(f"\n-- writing --")
    d.push(ENV_SRC, "/data/local/tmp/_restore_env.bin")
    d.push(MISC_SRC, "/data/local/tmp/_restore_misc.bin")
    out, rc = d.su(
        "dd if=/data/local/tmp/_restore_env.bin of=/dev/block/by-name/env conv=fsync && "
        "dd if=/data/local/tmp/_restore_misc.bin of=/dev/block/by-name/misc conv=fsync && "
        "sync && rm -f /data/local/tmp/_restore_env.bin /data/local/tmp/_restore_misc.bin && echo WROTE")
    print("  " + out.strip().replace("\n", "\n  "))
    if rc != 0 or "WROTE" not in out:
        sys.exit("write failed")

    # ---- verify ----
    print(f"\n-- verify (read-back) --")
    v_env = d.pull_raw("dd if=/dev/block/by-name/env bs=512 count=128")[:envtool.ENV_SIZE]
    if not envtool.crc_ok(v_env)[0]:
        sys.exit("env verify: CRC bad")
    vd = envtool.parse(v_env)
    if "boot_ce" in vd or "bootcefromemmc" in vd:
        sys.exit("env verify: gate still present -- restore did not take")
    print(f"  env: crc OK, {len(vd)} keys, gate absent (factory) -- OK")
    v_misc = d.pull_raw("dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32")[:32]
    vmi = ab_misc.parse(v_misc)
    print(f"  misc A/B: a=0x{vmi['a_byte']:02x} b=0x{vmi['b_byte']:02x} crc_ok={vmi['crc_ok']} -- OK")
    print("\nenv+misc restored to factory. Now run the installer:")
    print(f"  python flash_to_coreelec.py --serial {a.serial} --dry-run")


if __name__ == "__main__":
    main()
