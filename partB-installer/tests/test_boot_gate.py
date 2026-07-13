#!/usr/bin/env python3
"""Drift tests for the u-boot boot gate -- the strings that decide whether the box boots.

Dependency-free on purpose: run it directly.

    python tests/test_boot_gate.py

The bug these guard against: the gate is not written in one place. It is written in FOUR,
each on a different machine, in a different language:

  build/envtool.py            the PC installer (stage1, stage2, finish_install)
  addon/.../envcodec.py       the Kodi addon, running on CoreELEC ("Set default boot OS")
  app/.../EnvFlip.kt          the Android switcher app (flips boot_ce)
  payload/flash/user-update.sh  the CoreELEC OS-update hook, in the initramfs

Nothing makes them agree. Change `bootcmd` in envtool.py and the addon keeps writing the
old one, on the same box, to the same partition -- and the two disagree about what a normal
reboot does. These are the bytes u-boot executes; a mismatch is not a cosmetic drift.

They are also DEVICE-INDEPENDENT: stick and box run the same SoC and the same u-boot, so
gate_vars() takes a slot and a default and nothing else. A gate that varied by device would
be a bug in itself, so that is asserted too.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))
sys.path.insert(0, os.path.join(ROOT, "addon", "script.coreelec.toolbox", "resources", "lib"))

import envtool    # noqa: E402  -- the PC-side codec
import envcodec   # noqa: E402  -- the addon's copy that runs on CoreELEC

HOOK = os.path.join(ROOT, "payload", "flash", "user-update.sh")
KOTLIN = os.path.join(ROOT, "app", "RebootToCoreELEC", "app", "src", "main", "java",
                      "com", "jamal2367", "coreelec", "EnvFlip.kt")

SLOTS = ("_a", "_b")
DEFAULTS = ("android", "coreelec")

_FAILURES = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        _FAILURES.append(f"{name}: {e}")
        print(f"  FAIL  {name}: {e}")
    except Exception as e:                                       # noqa: BLE001
        _FAILURES.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
    else:
        print(f"  ok    {name}")


# --- 1. envtool (PC) vs envcodec (CoreELEC addon) ---------------------------------------
def gate_vars_identical():
    for slot in SLOTS:
        for dflt in DEFAULTS:
            t = envtool.gate_vars(slot, dflt)
            c = envcodec.gate_vars(slot, dflt)
            assert t == c, (
                f"envtool and envcodec disagree for slot={slot} default={dflt}.\n"
                + "\n".join(f"    {k}:\n      envtool : {t.get(k)}\n      envcodec: {c.get(k)}"
                            for k in sorted(set(t) | set(c)) if t.get(k) != c.get(k)))


def codec_roundtrip_identical():
    """Both codecs must serialize the same dict to the same bytes -- they write the same
    partition, so a one-byte difference is a different CRC and a different env."""
    env = envtool.serialize({"bootcmd": "run storeboot", "boot_ce": "0", "serial": "abc123"})
    assert envtool.crc_ok(env)[0], "envtool produced a bad CRC"
    assert envcodec.crc_ok(env), "envcodec rejects envtool's own output"
    a, b = envtool.parse(env), envcodec.parse(env)
    assert a == b, f"parsers disagree: {a} != {b}"
    assert envtool.serialize(a) == envcodec.serialize(b), "serializers produce different bytes"


def env_size_agrees():
    assert envtool.ENV_SIZE == envcodec.ENV_SIZE == 0x10000, (
        f"env size disagrees: envtool={envtool.ENV_SIZE:#x} envcodec={envcodec.ENV_SIZE:#x}")
    kt = open(KOTLIN, encoding="utf-8").read()
    m = re.search(r"ENV_SIZE\s*=\s*(0x[0-9a-fA-F]+)", kt)
    assert m, "could not find ENV_SIZE in EnvFlip.kt"
    assert int(m.group(1), 16) == envtool.ENV_SIZE, (
        f"EnvFlip.kt ENV_SIZE={m.group(1)} != envtool {envtool.ENV_SIZE:#x}")


# --- 2. the update hook ------------------------------------------------------------------
def hook_fallback_bootcmd_is_a_real_gate():
    """The hook's fw_setenv fallback hardcodes a bootcmd. It can only be the ANDROID-default
    one (an OS update has just reset bootcmd, so the box's real choice is unrecoverable) --
    but it must at least BE that string, not some third variant that has drifted."""
    src = open(HOOK, encoding="utf-8").read()
    m = re.search(r"fw_setenv -c \"\$FWCFG\" bootcmd '([^']*)'", src)
    assert m, "no hardcoded bootcmd found in user-update.sh"
    want = envtool.gate_vars("_a", "android")["bootcmd"]
    assert m.group(1) == want, (
        f"user-update.sh's fallback bootcmd has drifted from envtool.gate_vars(android):\n"
        f"    hook   : {m.group(1)}\n    envtool: {want}")


def hook_fallback_runs_only_without_an_env_image():
    """v2 ran the fw_setenv fallback UNCONDITIONALLY, after restoring the gated env image --
    so on a coreelec-default box an OS update silently rewrote the default back to Android.
    The fallback must be gated on the restore having failed."""
    src = open(HOOK, encoding="utf-8").read()
    assert "ENV_RESTORED" in src, "the hook no longer tracks whether the env image was restored"
    assert re.search(r'if \[ "\$ENV_RESTORED" = 1 \]', src), (
        "the fw_setenv fallback is not gated on ENV_RESTORED -- it would overwrite the boot "
        "default that the restored env image already carries")


def hook_validates_the_env_blob_before_writing_it():
    """It dd's 64 KiB onto a boot-critical partition, unattended, from a FAT32 filesystem."""
    src = open(HOOK, encoding="utf-8").read()
    assert "ENV_SIZE=65536" in src, "the hook does not know the expected env size"
    assert re.search(r'-ne "\$ENV_SIZE"', src), "the hook does not size-check env_dualboot.bin"
    assert "imgread kernel ${BOOTP}" in src, (
        "the hook does not check that env_dualboot.bin carries the gate for the slot it "
        "detected (a blob from the other slot or another unit would be written blindly)")


def hook_is_device_independent():
    """stick and box number their partitions identically today, but the hook must not depend
    on that: everything is resolved by PARTNAME."""
    src = open(HOOK, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    hits = re.findall(r"mmcblk0p\d+", code)
    assert not hits, f"the hook hardcodes a partition index: {hits} -- resolve it by name instead"


# --- 3. the gate itself is device-independent --------------------------------------------
def gate_does_not_vary_by_device():
    """gate_vars takes (slot, default) and nothing else. The stick and box share the SoC and
    u-boot, so one gate serves both -- this asserts nobody quietly adds a device argument."""
    import inspect
    for fn in (envtool.gate_vars, envcodec.gate_vars):
        params = list(inspect.signature(fn).parameters)
        assert params == ["ce_slot", "default"], (
            f"{fn.__module__}.gate_vars takes {params} -- the boot gate must depend only on "
            f"the slot and the boot default, never on which unit it is")


def both_defaults_are_distinguishable():
    """detect_default() reads the box's choice back out of bootcmd. If the two bootcmds ever
    became indistinguishable to it, stage2/the addon would silently flip the boot default."""
    for slot in SLOTS:
        for dflt in DEFAULTS:
            d = envcodec.gate_vars(slot, dflt)
            got = envcodec.detect_default(d)
            assert got == dflt, f"detect_default({dflt}) returned {got} for slot {slot}"
            assert envcodec.detect_ce_slot(d) == slot, f"detect_ce_slot failed for {slot}"


if __name__ == "__main__":
    print("boot gate -- drift between the four places it is written")
    check("envtool.gate_vars == envcodec.gate_vars (all slots x defaults)", gate_vars_identical)
    check("both codecs parse/serialize identically", codec_roundtrip_identical)
    check("ENV_SIZE agrees across envtool / envcodec / EnvFlip.kt", env_size_agrees)
    check("user-update.sh fallback bootcmd == envtool(android)", hook_fallback_bootcmd_is_a_real_gate)
    check("user-update.sh fallback only runs without an env image", hook_fallback_runs_only_without_an_env_image)
    check("user-update.sh validates env_dualboot.bin before writing", hook_validates_the_env_blob_before_writing_it)
    check("user-update.sh is device-independent (no mmcblk0pN)", hook_is_device_independent)
    print("the gate is device-independent")
    check("gate_vars depends only on (slot, default)", gate_does_not_vary_by_device)
    check("detect_default/detect_ce_slot round-trip both defaults", both_defaults_are_distinguishable)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all boot-gate checks passed")
