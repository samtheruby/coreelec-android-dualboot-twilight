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


if __name__ == "__main__":
    check("current gate -> OK", gate_current_is_ok)
    check("stale bootcefromemmc -> NEEDS_FIX", stale_bootcefromemmc_needs_fix)
    check("stale restore image -> NEEDS_FIX", stale_restore_image_needs_fix)
    check("no gate -> NOT_APPLICABLE", no_gate_is_not_applicable)
    check("bad CRC -> UNKNOWN", bad_crc_is_unknown)
    check("file match -> OK", file_match_is_ok)
    check("file differs -> NEEDS_FIX", file_differs_needs_fix)
    check("file missing -> NEEDS_FIX", file_missing_needs_fix)
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)"); sys.exit(1)
    print("all repair detection checks passed")
