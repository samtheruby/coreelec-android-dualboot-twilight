#!/usr/bin/env python3
"""Drift tests for the u-boot boot gate -- the strings that decide whether the box boots.

Dependency-free on purpose: run it directly.

    python tests/test_boot_gate.py

The bug these guard against: the gate is not written in one place. It is written in FIVE,
each on a different machine, in a different language:

  build/envtool.py            the PC installer (stage1, stage2, finish_install)
  addon/.../envcodec.py       the Kodi addon, running on CoreELEC ("Set default boot OS")
  app/.../EnvFlip.kt          the Android switcher app (flips boot_ce)
  app/.../EnvGate.kt          the Android switcher app's in-app re-assert (writes the WHOLE
                              gate, and ENV_ADDITIONS, after a CoreELEC update stomps bootcmd)
  payload/flash/user-update.sh  the CoreELEC OS-update hook, in the initramfs

Nothing makes them agree. Change `bootcmd` in envtool.py and the addon keeps writing the
old one, on the same box, to the same partition -- and the two disagree about what a normal
reboot does. These are the bytes u-boot executes; a mismatch is not a cosmetic drift.

They are also DEVICE-INDEPENDENT: stick and box run the same SoC and the same u-boot, so
gate_vars() takes a slot and a default and nothing else. A gate that varied by device would
be a bug in itself, so that is asserted too.
"""
import json
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
_KT = os.path.join(ROOT, "app", "RebootToCoreELEC", "app", "src", "main", "java",
                   "com", "jamal2367", "coreelec")
KOTLIN = os.path.join(_KT, "EnvFlip.kt")
ENVGATE_KT = os.path.join(_KT, "EnvGate.kt")
ENVADD_KT = os.path.join(_KT, "EnvAdditions.kt")
ADDITIONS_JSON = os.path.join(ROOT, "refdata", "env_additions.json")

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


# --- 1b. EnvGate.kt (the Android app's in-app re-assert) ---------------------------------
# EnvFlip.kt only flips boot_ce, so it never had to know the gate STRINGS. EnvGate.kt does:
# it rewrites bootcefromemmc/bootcmd on-device after a CoreELEC update stomps them, which
# makes it a fifth writer of the exact bytes u-boot executes -- in a language none of the
# other four are in, on the one machine that cannot be re-flashed from a PC if it is wrong.
# Kotlin cannot be imported, so the literals are extracted from the source and compared.

def _kt_source(path):
    """File contents with comments stripped -- prose inside a comment is often quoted
    (e.g. CoreELEC's "reboot to eMMC/nand") and would otherwise be read as a literal."""
    src = open(path, encoding="utf-8").read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _kt_str(expr):
    """Value of a Kotlin string expression built by `+`-concatenating literals."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', expr)
    return "".join(parts).replace('\\"', '"').replace("\\$", "$").replace("\\\\", "\\")


def envgate_kt_gate_matches_envtool():
    """The gate EnvGate.kt writes must be byte-identical to envtool.gate_vars, for every
    slot x default. A single byte of drift is a different bootcmd on a device that has
    already booted CoreELEC once -- i.e. a box that stops switching, or stops booting."""
    src = _kt_source(ENVGATE_KT)
    m = re.search(r"fun bootCeFromEmmc\(ceSlot: String\): String =(.*?)\n\n", src, re.S)
    assert m, "could not find bootCeFromEmmc() in EnvGate.kt"
    tpl = _kt_str(m.group(1))
    m = re.search(r"fun bootCmd\(default: String\): String =(.*?)\n\s*\}\n", src, re.S)
    assert m, "could not find bootCmd() in EnvGate.kt"
    assert "} else {" in m.group(1), "bootCmd() no longer has both default branches"
    ce_branch, android_branch = m.group(1).split("} else {")

    for slot in SLOTS:
        for dflt in DEFAULTS:
            want = envtool.gate_vars(slot, dflt)
            got = {"bootcefromemmc": tpl.replace("$ceSlot", slot),
                   "bootcmd": _kt_str(ce_branch if dflt == "coreelec" else android_branch)}
            for k, v in got.items():
                assert v == want[k], (
                    f"EnvGate.kt {k} has drifted from envtool.gate_vars"
                    f"(slot={slot}, default={dflt}):\n    EnvGate.kt: {v}\n    envtool   : {want[k]}")
    # the slot must actually be substituted, or every unit gets the same (wrong) gate
    assert "$ceSlot" in tpl, "bootCeFromEmmc() no longer interpolates the CE slot"


def envgate_kt_additions_mirror_the_json():
    """EnvAdditions.kt is a hand-regenerated mirror of refdata/env_additions.json (the app
    cannot read the installer's refdata). Order matters too: both feed a LinkedHashMap /
    ordered dict straight into serialize(), so a reordering is a different 64 KiB blob."""
    kt = _kt_source(ENVADD_KT)
    assert "linkedMapOf(" in kt, "EnvAdditions.kt no longer defines a linkedMapOf"
    body = kt.split("linkedMapOf(", 1)[1]
    pairs = re.findall(r'"([A-Za-z0-9_]+)" to ((?:\s*"(?:[^"\\]|\\.)*"\s*\+?)+),', body)
    got = {k: _kt_str(v) for k, v in pairs}
    want = json.load(open(ADDITIONS_JSON, encoding="utf-8"))

    assert list(got) == list(want), (
        f"EnvAdditions.kt key ORDER differs from env_additions.json:\n"
        f"    EnvAdditions.kt   : {list(got)}\n    env_additions.json: {list(want)}")
    for k in want:
        assert got[k] == want[k], (
            f"EnvAdditions.kt {k} has drifted from env_additions.json:\n"
            f"    EnvAdditions.kt   : {got[k]}\n    env_additions.json: {want[k]}")
    # the identity guard build_env.generic_additions() applies, applied here too
    for k in got:
        assert k not in build_env_identity_keys(), f"EnvAdditions.kt carries identity var {k}"


def build_env_identity_keys():
    import build_env
    return build_env.IDENTITY_KEYS


def envgate_kt_never_writes_identity():
    """EnvGate.kt edits the device's OWN env in place, so identity must be listed and
    guarded exactly as build_env does it -- a transplanted serial/MAC is not recoverable."""
    src = _kt_source(ENVGATE_KT)
    m = re.search(r"IDENTITY_KEYS\s*=\s*listOf\((.*?)\)", src, re.S)
    assert m, "EnvGate.kt no longer declares IDENTITY_KEYS"
    got = re.findall(r'"([^"]+)"', m.group(1))
    want = build_env_identity_keys()
    assert got == want, (
        f"EnvGate.kt IDENTITY_KEYS has drifted from build_env.IDENTITY_KEYS:\n"
        f"    EnvGate.kt: {got}\n    build_env : {want}")


def envgate_kt_repairs_a_stomped_bootcmd_without_guessing():
    """THE regression this whole path exists for: a CoreELEC update rewrites bootcmd to a
    stock one that drops the boot_ce test, while bootcefromemmc survives. reassert_env_gate.py
    branches on `if ce_slot:` -- slot READ, nothing inferred. If EnvGate.reassert() instead
    branches on the fully-wired flag, that exact (and most common) case falls through to the
    rebuild path, which INFERS the CE slot from the inactive Android slot and prompts the
    user -- a guess, where the answer was sitting in bootcefromemmc all along."""
    src = _kt_source(ENVGATE_KT)
    m = re.search(r"if \((state\.[A-Za-z]+)[^)]*\) \{\s*\n(?:.*?)ceSlot = state\.ceSlot", src, re.S)
    assert m, "could not find the repair-strategy branch in EnvGate.reassert()"
    assert m.group(1) == "state.ceSlot", (
        f"EnvGate.reassert() picks its repair strategy on `{m.group(1)}`, not `state.ceSlot`. "
        f"A CoreELEC update that stomps bootcmd but leaves bootcefromemmc intact would then "
        f"take the rebuild path and INFER the CE slot instead of reading it.")


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
    assert re.search(r'is_size\s+/flash/env_dualboot\.bin\s+"\$ENV_SIZE"', src), (
        "the hook does not size-check env_dualboot.bin against ENV_SIZE before writing it")
    assert "imgread kernel ${BOOTP}" in src, (
        "the hook does not check that env_dualboot.bin carries the gate for the slot it "
        "detected (a blob from the other slot or another unit would be written blindly)")


# The hook runs inside CoreELEC's OWN OS-update initramfs -- NOT the ramdisk in
# payload/flash/kernel.img, which is ours. That image is whatever CoreELEC shipped: busybox is
# the only binary and its applet table VARIES BY CE VERSION AND BOX, so the hook may depend only
# on applets present in EVERY such build. `stat` and `awk` were once assumed present -- checked
# against the WRONG image (ours) -- and a real box's update initramfs had neither, which refused
# a healthy env image ("cannot measure ... no stat, no ls+awk"). They are in this list now so a
# boot-critical path can never lean on them again:
INITRAMFS_MISSING = ("wc", "expr", "find", "od", "cmp", "xargs", "mktemp", "sha256sum",
                     "du", "sort", "stat", "awk")


def hook_uses_only_tools_the_initramfs_has():
    """The hook runs where busybox is the only binary, so a tool it lacks is a runtime failure
    on the box, unattended, with no one watching.

    `wc` is not a busybox applet here. v3 sized the env image with
    `SZ=$(wc -c < /flash/env_dualboot.bin 2>/dev/null || echo 0)`, so on every real box that
    substitution failed with "not found" and `|| echo 0` reported a perfectly healthy 65536 B
    image as 0 B. The hook then refused to restore the gate ("truncated or corrupt"), found no
    fw_setenv either (also absent from the initramfs), and left the box needing a MANUAL
    re-assert after every single CoreELEC update.
    """
    src = open(HOOK, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    for tool in INITRAMFS_MISSING:
        hit = re.search(rf"(?:^|[|;&(]|\$\()\s*{tool}\s", code, re.M)
        assert not hit, (
            f"the hook calls `{tool}`, which the CoreELEC initramfs busybox does not provide: "
            f"{hit.group(0).strip()!r}")


def hook_sizes_env_with_only_guaranteed_primitives():
    """The size check decides whether 64 KiB gets written to the boot-critical env partition, so
    it must not itself hinge on an OPTIONAL applet -- that only trades one "tool not found" for
    another. v3 used `wc` (absent); v4 used `stat`/`ls+awk` and a real box had neither, so it
    refused a healthy image ("cannot measure ... no stat, no ls+awk"). The check must use only
    what every update initramfs has: `dd` (it already writes the kernel with it) and the shell's
    own `[ -s ]`."""
    src = open(HOOK, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert re.search(r"\bis_size\b", code), "the hook no longer sizes the env via is_size()"
    assert '[ -s "$ENV_PROBE" ]' in code, (
        "is_size must decide file length with `[ -s ]` (a shell builtin, always present), not "
        "an external size tool")
    for tool in ("wc", "stat", "awk", "ls"):
        assert not re.search(rf"(?:^|[|;&(]|\$\()\s*{tool}\s", code), (
            f"the env size check calls `{tool}` -- a boot-critical write must not hinge on an "
            f"optional applet the update initramfs may lack")
    assert not re.search(r"\|\|\s*echo\s+0", code), (
        "a failed size measurement is being turned into a size of 0, which reads as a "
        "truncated file and makes the hook refuse to restore a healthy env image")


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
    print("boot gate -- drift between the five places it is written")
    check("envtool.gate_vars == envcodec.gate_vars (all slots x defaults)", gate_vars_identical)
    check("both codecs parse/serialize identically", codec_roundtrip_identical)
    check("ENV_SIZE agrees across envtool / envcodec / EnvFlip.kt", env_size_agrees)
    check("EnvGate.kt gate == envtool.gate_vars (all slots x defaults)", envgate_kt_gate_matches_envtool)
    check("EnvAdditions.kt == refdata/env_additions.json (values + order)", envgate_kt_additions_mirror_the_json)
    check("EnvGate.kt IDENTITY_KEYS == build_env.IDENTITY_KEYS", envgate_kt_never_writes_identity)
    check("EnvGate.kt repairs a stomped bootcmd without inferring the slot",
          envgate_kt_repairs_a_stomped_bootcmd_without_guessing)
    check("user-update.sh fallback bootcmd == envtool(android)", hook_fallback_bootcmd_is_a_real_gate)
    check("user-update.sh fallback only runs without an env image", hook_fallback_runs_only_without_an_env_image)
    check("user-update.sh validates env_dualboot.bin before writing", hook_validates_the_env_blob_before_writing_it)
    check("user-update.sh only calls tools the initramfs busybox has", hook_uses_only_tools_the_initramfs_has)
    check("user-update.sh sizes env with only dd + [ -s ]", hook_sizes_env_with_only_guaranteed_primitives)
    check("user-update.sh is device-independent (no mmcblk0pN)", hook_is_device_independent)
    print("the gate is device-independent")
    check("gate_vars depends only on (slot, default)", gate_does_not_vary_by_device)
    check("detect_default/detect_ce_slot round-trip both defaults", both_defaults_are_distinguishable)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all boot-gate checks passed")
