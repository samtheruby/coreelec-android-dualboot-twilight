#!/usr/bin/env python3
"""Detection tests for the Toolbox repair suite. Dependency-free: python tests/test_repair.py"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "addon", "script.coreelec.toolbox", "resources", "lib"))

import envcodec       # noqa: E402
import repair_core as rc  # noqa: E402

_FAILURES = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        _FAILURES.append(f"{name}: {e}"); print(f"  FAIL  {name}: {e}")
    except Exception as e:  # noqa: BLE001
        _FAILURES.append(f"{name}: {type(e).__name__}: {e}"); print(f"  ERROR {name}: {e}")
    else:
        print(f"  ok    {name}")


def env_with(**overrides):
    """A valid env carrying the current gate for slot _a / android, with optional overrides."""
    d = {"bootargs": "x"}
    d.update(envcodec.gate_vars("_a", "android"))
    d.update(overrides)
    return envcodec.serialize(d)


def gate_current_is_ok():
    env = env_with()
    r = rc.check_boot_gate(env, env)
    assert r.status == rc.OK, r


def stale_bootcefromemmc_needs_fix():
    old = envcodec.gate_vars("_a", "android")["bootcefromemmc"].replace(
        "vout=1080p60hz,dis frac_rate_policy=0 hdmitx= hdr_policy=1", "hdmitx=")
    env = env_with(bootcefromemmc=old)
    r = rc.check_boot_gate(env, env)
    assert r.status == rc.NEEDS_FIX and r.reboot, r


def stale_restore_image_needs_fix():
    env = env_with()                       # live env current
    stale_dual = env_with(bootcefromemmc="setenv bootargs junk")
    r = rc.check_boot_gate(env, stale_dual)
    assert r.status == rc.NEEDS_FIX, r


def no_gate_is_not_applicable():
    env = envcodec.serialize({"bootcmd": "run storeboot"})
    r = rc.check_boot_gate(env, env)
    assert r.status == rc.NOT_APPLICABLE, r


def bad_crc_is_unknown():
    env = bytearray(env_with()); env[0] ^= 0xFF
    r = rc.check_boot_gate(bytes(env), None)
    assert r.status == rc.UNKNOWN, r


def file_match_is_ok():
    r = rc.check_file("dovi_ko", "dovi.ko", b"abc", b"abc")
    assert r.status == rc.OK, r


def file_differs_needs_fix():
    r = rc.check_file("dovi_ko", "dovi.ko", b"abc", b"xyz")
    assert r.status == rc.NEEDS_FIX and r.reboot, r


def file_missing_needs_fix():
    r = rc.check_file("dovi_ko", "dovi.ko", None, b"xyz")
    assert r.status == rc.NEEDS_FIX and r.detail == "missing", r


def build_fixed_env_repairs_and_preserves_default():
    old = envcodec.gate_vars("_a", "android")["bootcefromemmc"].replace(
        "vout=1080p60hz,dis frac_rate_policy=0 hdmitx= hdr_policy=1", "hdmitx=")
    env = env_with(bootcefromemmc=old, boot_ce="0")
    live, dual = rc.build_fixed_env(env)
    assert envcodec.crc_ok(live) and envcodec.crc_ok(dual)
    want = envcodec.gate_vars("_a", "android")
    assert envcodec.parse(live)["bootcefromemmc"] == want["bootcefromemmc"]
    assert envcodec.parse(live)["bootcmd"] == want["bootcmd"]          # default preserved
    assert envcodec.parse(dual)["boot_ce"] == "1"                      # android default -> updates re-enter CE


def build_fixed_env_rejects_gateless():
    try:
        rc.build_fixed_env(envcodec.serialize({"bootcmd": "run storeboot"}))
    except ValueError:
        return
    assert False, "expected ValueError on a gateless env"


DOVI_SHA256 = "f6c26659a255447685ceac9441e399c999b1fae9c6435c48d70e14a14dd7f8f7"
REPAIR_DIR = os.path.join(ROOT, "addon", "script.coreelec.toolbox", "resources", "repair")


def bundled_dovi_matches_pin():
    p = os.path.join(REPAIR_DIR, "dovi.ko")
    got = hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert got == DOVI_SHA256, f"bundled dovi.ko {got} != pinned {DOVI_SHA256}"


def bundled_hook_matches_payload():
    a = open(os.path.join(REPAIR_DIR, "user-update.sh"), "rb").read()
    b = open(os.path.join(ROOT, "payload", "flash", "user-update.sh"), "rb").read()
    assert a == b, "bundled user-update.sh has drifted from payload/flash/user-update.sh"


if __name__ == "__main__":
    check("current gate -> OK", gate_current_is_ok)
    check("stale bootcefromemmc -> NEEDS_FIX", stale_bootcefromemmc_needs_fix)
    check("stale restore image -> NEEDS_FIX", stale_restore_image_needs_fix)
    check("no gate -> NOT_APPLICABLE", no_gate_is_not_applicable)
    check("bad CRC -> UNKNOWN", bad_crc_is_unknown)
    check("file match -> OK", file_match_is_ok)
    check("file differs -> NEEDS_FIX", file_differs_needs_fix)
    check("file missing -> NEEDS_FIX", file_missing_needs_fix)
    check("build_fixed_env repairs + preserves default", build_fixed_env_repairs_and_preserves_default)
    check("build_fixed_env rejects gateless env", build_fixed_env_rejects_gateless)
    check("bundled dovi.ko == pinned sha256", bundled_dovi_matches_pin)
    check("bundled user-update.sh == payload copy", bundled_hook_matches_payload)
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)"); sys.exit(1)
    print("all repair detection checks passed")
