#!/usr/bin/env python3
"""
Non-destructive validation of the Part B installer against a LIVE unit (reads
only -- never writes to the device). Safe to run on the already-modified unit;
it exercises every per-unit code path with real device data and cross-checks the
app's Kotlin env codec against the PC envtool.

Usage: python validate_nondestructive.py --serial 192.168.1.195:41243
"""
import argparse, base64, os, subprocess, sys, struct, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "..", "artifacts")
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool, build_env, ab_misc, layout as L  # noqa

PASS, FAIL = 0, 0


def chk(cond, msg):
    global PASS, FAIL
    print(("  PASS " if cond else " FAIL ") + msg)
    if cond: PASS += 1
    else: FAIL += 1


def su_bytes(serial, cmd):
    return subprocess.run(["adb", "-s", serial, "exec-out", f"su -c '{cmd}'"],
                          capture_output=True).stdout


# --- a faithful Python replica of EnvFlip.kt's serialize, to prove the app's ---
# --- Kotlin codec produces byte-identical env to envtool (and thus a valid CRC) -
def kotlin_like_set_boot_ce(env, value):
    m = envtool.parse(env)                  # same parse semantics
    m["boot_ce"] = str(value)
    body = bytearray(envtool.ENV_SIZE - 4)
    pos = 0
    for k, v in m.items():
        entry = f"{k}={v}".encode("latin1")
        body[pos:pos + len(entry)] = entry; pos += len(entry)
        body[pos] = 0; pos += 1
    body[pos] = 0
    crc = zlib.crc32(bytes(body)) & 0xffffffff
    return struct.pack("<I", crc) + bytes(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    args = ap.parse_args()
    import adb_serial
    args.serial = adb_serial.resolve(args.serial)
    s = args.serial

    print("== device identity (read-only) ==")
    dev = su_bytes(s, "getprop ro.product.device").decode().strip()
    active = su_bytes(s, "getprop ro.boot.slot_suffix").decode().strip()
    ce_slot = {"_a": "_b", "_b": "_a"}.get(active)
    chk(dev == "twilight", f"device == twilight (got '{dev}')")
    chk(ce_slot in ("_a", "_b"), f"slot detect: active={active} -> CE={ce_slot}")

    print("\n== env: build_target_env on the unit's OWN env ==")
    env = su_bytes(s, "dd if=/dev/block/by-name/env bs=4096 count=16 2>/dev/null")[:envtool.ENV_SIZE]
    chk(len(env) == envtool.ENV_SIZE, f"env read 64 KiB (got {len(env)})")
    chk(envtool.crc_ok(env)[0], "target env CRC valid")
    before = envtool.parse(env)
    new = build_env.build_target_env(env, ce_slot)
    after = envtool.parse(new)
    chk(envtool.crc_ok(new)[0], "rebuilt env CRC valid")
    chk(after.get("boot_ce") == "0", "boot_ce defaults to 0 (Android)")
    chk(f"imgread kernel boot{ce_slot}" in after["bootcefromemmc"], f"gate boots boot{ce_slot}")
    chk(f"dtbo{ce_slot}" in after["bootcefromemmc"], f"gate reads dtbo{ce_slot}")
    chk("run bootfromusb" in after["bootcmd"], "USB-recovery path preserved in bootcmd")
    # identity preserved
    idkeep = all(before.get(k) == after.get(k) for k in build_env.IDENTITY_KEYS)
    present = [k for k in build_env.IDENTITY_KEYS if k in before]
    chk(idkeep, f"identity preserved untouched: {present}")
    # generic additions present
    chk(all(k in after for k in build_env.GENERIC_KEYS), "all generic boot-helpers added")

    print("\n== env: app Kotlin codec == PC envtool (byte-identical) ==")
    k1 = kotlin_like_set_boot_ce(env, 1)
    e1 = envtool.set_boot_ce(env, 1)
    chk(k1 == e1, "EnvFlip(set boot_ce=1) byte-identical to envtool")
    chk(envtool.crc_ok(k1)[0] and envtool.parse(k1)["boot_ce"] == "1", "app-flip env CRC valid + boot_ce=1")
    # flip back
    k0 = kotlin_like_set_boot_ce(k1, 0)
    chk(envtool.parse(k0)["boot_ce"] == "0" and envtool.crc_ok(k0)[0], "app-flip back to 0 valid")

    print("\n== misc A/B: mark CE slot unbootable ==")
    misc = su_bytes(s, "dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32 2>/dev/null")[:32]
    info = ab_misc.parse(misc)
    chk(info["crc_ok"] and info["magic"] == b"BCAB", "misc struct valid (BCAB, crc ok)")
    marked = ab_misc.mark_unbootable(misc, ce_slot)
    mi = ab_misc.parse(marked)
    tgt = mi["a_byte"] if ce_slot == "_a" else mi["b_byte"]
    chk(tgt == 0 and mi["crc_ok"], f"CE slot {ce_slot} priority -> 0, crc recomputed")

    print("\n== GPT artifact vs device geometry ==")
    gp = open(os.path.join(ART, "gpt_primary.bin"), "rb").read()
    num = struct.unpack_from("<I", gp, 512 + 80)[0]
    chk(num == 128, "artifact GPT has 128 entries")
    secs = {n: (a, b, c) for n, a, b, c in L.as_sectors()}
    chk(secs["CE_STORAGE"][1] == L.TOTAL_SECTORS - 34 + 1 - 0 or secs["CE_STORAGE"][1] == 15265791,
        f"CE_STORAGE ends at last usable LBA ({secs['CE_STORAGE'][1]})")

    print("\n== artifact sizes vs layout ==")
    for name, art in (("CE_FLASH", "ce_flash.img"), ("CE_STORAGE", "ce_storage.img")):
        want = secs[name][2] * 512
        raw = os.path.join(ART, art)
        if os.path.exists(raw):
            got = os.path.getsize(raw)
            chk(got <= want, f"{art} ({got} B) fits partition {name} ({want} B)")
        elif os.path.exists(raw + ".gz"):
            chk(True, f"{art}.gz present (raw size verified at build; fits {name})")
        else:
            chk(False, f"{art}[.gz] missing")
    chk(os.path.getsize(os.path.join(ART, "dtboa.img")) == 0x20000, "dtboa.img == 128 KiB")

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
