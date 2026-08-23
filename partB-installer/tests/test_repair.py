#!/usr/bin/env python3
"""Detection tests for the Toolbox repair suite. Dependency-free: python tests/test_repair.py"""
import hashlib
import os
import re
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


def _boot_img(payload, part_size=64 * 1024 * 1024):
    """A boot_<slot> partition image: the kernel image, then whatever the partition already
    held. Partitions are far larger than kernel.img, so the trailing bytes are always there."""
    return payload + b"\xa5" * (part_size - len(payload))


def kernel_image_current_is_ok():
    ki = b"ANDROID!" + os.urandom(4096)
    r = rc.check_kernel_image(_boot_img(ki), ki)
    assert r.status == rc.OK, r


def kernel_image_stale_tail_needs_fix():
    """The failure that stranded a real stick: the CE update hook's dd wrote all but the last
    659,456 bytes of kernel.img to boot_b and still exited 0, so the kernel region matched and
    the tail of the zstd initramfs was left over from the PREVIOUS kernel. u-boot loaded it,
    the kernel could not unpack the ramdisk, freed it, and panicked on a missing /init.
    A check that samples the head -- or trusts dd's exit status -- does not see this."""
    ki = b"ANDROID!" + os.urandom(200_000)
    written = ki[:150_000] + os.urandom(len(ki) - 150_000)   # correct prefix, stale tail
    r = rc.check_kernel_image(_boot_img(written), ki)
    assert r.status == rc.NEEDS_FIX, r
    assert r.reboot is True, r


def kernel_image_unreadable_needs_fix():
    ki = b"ANDROID!" + os.urandom(4096)
    r = rc.check_kernel_image(None, ki)
    assert r.status == rc.NEEDS_FIX, r


def kernel_image_missing_source_is_unknown():
    """No /flash/kernel.img means nothing to compare against and nothing to repair FROM."""
    r = rc.check_kernel_image(_boot_img(b"whatever"), None)
    assert r.status == rc.UNKNOWN, r


def kernel_image_corrupt_source_is_unknown_not_needs_fix():
    """If /flash/kernel.img itself fails the md5 CoreELEC ships next to it, boot_<slot> may
    well be fine and the source is the broken one. Reporting NEEDS_FIX here would invite a
    repair that copies the corruption onto the boot partition."""
    ki = b"ANDROID!" + os.urandom(4096)
    r = rc.check_kernel_image(_boot_img(ki), ki, kernel_md5_text="%s  target/KERNEL\n" % ("0" * 32))
    assert r.status == rc.UNKNOWN, r


def kernel_image_honours_a_matching_shipped_md5():
    ki = b"ANDROID!" + os.urandom(4096)
    md5 = hashlib.md5(ki).hexdigest()
    r = rc.check_kernel_image(_boot_img(ki), ki, kernel_md5_text="%s  target/KERNEL\n" % md5)
    assert r.status == rc.OK, r


REPAIR_PY = os.path.join(ROOT, "addon", "script.coreelec.toolbox", "resources", "lib", "repair.py")


def every_check_the_scan_runs_has_a_fixer():
    """repair.py imports xbmc, so it cannot be imported here -- but the one thing that must not
    drift is checkable from the source: every check id scan() can return must have an entry in
    FIXERS. Adding a detection without its fix means Repair lists the problem, the user presses
    "Fix", and FIXERS[r.id] raises KeyError. (check_kernel_image shipped exactly that way for a
    few minutes.) The reverse also matters: a fixer nothing scans for is dead code."""
    src = open(REPAIR_PY, encoding="utf-8").read()
    fixers = set(re.findall(r'^\s*"([a-z_]+)":', src[src.index("FIXERS = {"):], re.M))

    scanned = set(re.findall(r'rc\.check_file\(\s*"([a-z_]+)"', src))
    # The dedicated checks own their id, so ask repair_core rather than restating it here.
    if "rc.check_boot_gate(" in src:
        scanned.add(rc.check_boot_gate(b"", None).id)
    if "rc.check_kernel_image(" in src:
        scanned.add(rc.check_kernel_image(None, b"x").id)

    assert scanned, "no checks found in repair.py -- the parse is wrong, not the code"
    assert not (scanned - fixers), (
        f"scan() can report {sorted(scanned - fixers)} but FIXERS has no entry, so pressing "
        f"'Fix' raises KeyError")
    assert not (fixers - scanned), (
        f"FIXERS has {sorted(fixers - scanned)} that nothing scans for")


def kernel_image_check_is_actually_wired_into_the_scan():
    """The detection is worthless sitting in repair_core unused."""
    src = open(REPAIR_PY, encoding="utf-8").read()
    scan = src[src.index("def scan("):src.index("def _summary(")]
    assert "rc.check_kernel_image(" in scan, "scan() does not run the kernel image check"
    assert "kernel.img.md5" in scan, (
        "the scan does not pass the shipped checksum, so a corrupt /flash/kernel.img would be "
        "reported as a stale boot partition and 'repaired' by copying it")


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
    check("kernel image current -> OK", kernel_image_current_is_ok)
    check("kernel image stale tail -> NEEDS_FIX", kernel_image_stale_tail_needs_fix)
    check("kernel image unreadable -> NEEDS_FIX", kernel_image_unreadable_needs_fix)
    check("kernel image missing source -> UNKNOWN", kernel_image_missing_source_is_unknown)
    check("kernel image corrupt source -> UNKNOWN", kernel_image_corrupt_source_is_unknown_not_needs_fix)
    check("kernel image matching shipped md5 -> OK", kernel_image_honours_a_matching_shipped_md5)
    check("every check the scan runs has a fixer", every_check_the_scan_runs_has_a_fixer)
    check("kernel image check is wired into scan()", kernel_image_check_is_actually_wired_into_the_scan)
    check("bundled dovi.ko == pinned sha256", bundled_dovi_matches_pin)
    check("bundled user-update.sh == payload copy", bundled_hook_matches_payload)
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)"); sys.exit(1)
    print("all repair detection checks passed")
