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
KERNELGATE_KT = os.path.join(_KT, "KernelGate.kt")
MAIN_KT = os.path.join(_KT, "MainActivity.kt")
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
                     "md5sum", "du", "sort", "stat", "awk")


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


def _kt_branch(src, label):
    """Body of the `is KernelGate.State.<label> ->` when-branch: brace-matched if it opens a
    block, otherwise the rest of the line. Matching the branch exactly matters -- a regex that
    runs past it happily finds the `true` belonging to the NEXT branch."""
    i = src.index(f"is KernelGate.State.{label} ->")
    rest = src[i:]
    nl, brace = rest.index("\n"), rest.find("{")
    if brace == -1 or brace > nl:
        return rest[:nl]
    depth = 0
    for k in range(brace, len(rest)):
        if rest[k] == "{":
            depth += 1
        elif rest[k] == "}":
            depth -= 1
            if depth == 0:
                return rest[brace:k + 1]
    raise AssertionError(f"unbalanced braces in the {label} branch")


def kernelgate_kt_compares_the_whole_image():
    """The corruption that stranded the stick began at byte 23,973,888 of a 24,633,344-byte
    image -- the kernel region and the first 97% of the ramdisk were byte-perfect. Any check
    that hashes a fixed prefix, a sample, or the boot header calls that image healthy. So the
    comparison must be sized from kernel.img itself and must cover all of it."""
    src = _kt_source(KERNELGATE_KT)
    assert re.search(r"SZ=\$\{'\$'\}\(stat -c %s", src), (
        "KernelGate no longer sizes the comparison from /flash/kernel.img itself")
    assert "FULL=" in src and "REM=" in src, (
        "KernelGate must hash FULL 512-byte blocks plus any remainder -- a fixed block count "
        "silently compares only part of the image")
    assert re.search(r"count=\$\{'\$'\}FULL", src), (
        "the boot-partition hash no longer reads the number of blocks kernel.img implies")
    for bad in ("count=1 ", "bs=4096 count=16"):
        assert bad not in src, f"KernelGate appears to hash only a prefix ({bad!r})"


def kernelgate_kt_will_not_repair_from_an_unverified_source():
    """CoreELEC ships kernel.img.md5 beside the image. If /flash/kernel.img fails it, copying
    that file onto boot_<slot> would put the corruption where u-boot actually reads."""
    src = _kt_source(KERNELGATE_KT)
    assert re.search(r'echo "SHIPPED[^"]*kernel\.img\.md5|kernel\.img\.md5.*?echo "SHIPPED', src, re.S), (
        "the shell no longer reports /flash/kernel.img.md5 as SHIPPED, so the source checksum "
        "is never seen")
    assert 'f["SHIPPED"]' in src, "KernelGate no longer reads the SHIPPED checksum back"
    assert re.search(r"shipped[^\n]*equals\(src", src), (
        "the shipped checksum is read but never compared against the source image")
    assert re.search(r"SourceBad", src), "KernelGate no longer distinguishes a bad SOURCE"
    m = re.search(r"fun repair\(.*?\n(.*?)\n    }", src, re.S)
    assert m, "could not find KernelGate.repair()"
    assert "SourceBad" in m.group(1), (
        "repair() does not bail out on a SourceBad pre-check, so it can copy a corrupt "
        "/flash/kernel.img onto the boot partition")


def mainactivity_preflights_the_kernel_before_flipping_boot_ce():
    """boot_ce is consumed by bootcmd BEFORE bootcefromemmc runs, so a reboot into a bad kernel
    costs the flag as well as the boot: the box panics, comes back on Android, and the button
    looks like it did nothing. Check the image first, and do not flip the flag when a verified
    mismatch could not be repaired."""
    src = _kt_source(MAIN_KT)
    flip = src.index("EnvFlip.bootCoreElec")
    pre = src.rindex("kernelImageUsable()", 0, flip)
    assert pre < flip, "kernelImageUsable() must run BEFORE boot_ce is flipped"
    m = re.search(r"private fun kernelImageUsable\(\).*?\n    }\n", src, re.S)
    assert m, "kernelImageUsable() is gone"
    body = m.group(0)
    split = body.index("when (val after")
    outer, inner = body[:split], body[split:]

    # Fails OPEN on "cannot tell": blocking here would regress boxes whose CE_FLASH Android
    # cannot mount, for a boot that would have worked.
    for label in ("Unknown", "SourceBad"):
        br = _kt_branch(outer, label)
        assert "return true" in br and "return false" not in br, (
            f"an unverifiable kernel image ({label}) must not block a boot that would have "
            f"worked:\n{br}")

    # Fails CLOSED once the mismatch is verified AND the repair did not take. repair() confirms
    # by reading the partition back, so anything but Ok means boot_<slot> is still wrong.
    for label in ("Stale", "SourceBad", "Unknown"):
        br = _kt_branch(inner, label)
        assert "false" in br and "true" not in br, (
            f"after a failed repair the {label} branch still lets the reboot proceed; the box "
            f"will panic and land back on Android:\n{br}")
    assert "true" in _kt_branch(inner, "Ok"), "a verified repair must allow the reboot"


def mainactivity_checks_the_kernel_on_the_gate_repair_path_too():
    """The gate-repair path is the one that MOST needs the kernel check: a CoreELEC update
    stomps bootcmd and re-syncs boot_<slot> in the same pass, so both break together. Checking
    only on the gate-intact fast path leaves the common post-update case rebooting blind.

    So no route may hand `reboot = true` to EnvGate.reassert() -- that reboots from inside
    reassert, before anything has looked at the image. The reboot has to happen after the
    check, which is why GateResult.Ok reboots explicitly."""
    src = _kt_source(MAIN_KT)
    bad = re.findall(r"EnvGate\.reassert\([^)]*reboot\s*=\s*true[^)]*\)", src)
    assert not bad, (
        "reassert() is asked to reboot on its own, so the gate-repair path reboots without "
        f"checking the kernel image: {bad}")
    m = re.search(r"is EnvGate\.GateResult\.Ok ->.*?\n            }", src, re.S)
    assert m, "could not find the GateResult.Ok branch"
    assert "kernelImageUsable()" in m.group(0), (
        "a re-asserted gate reboots without checking the image it hands off to")
    assert re.search(r'if \(kernelImageUsable\(\)\)\s*EnvFlip\.runSu\("reboot"\)', m.group(0)), (
        "the reboot on the gate-repair path is not gated on the kernel check")


def hook_checks_what_the_kernel_dd_actually_wrote():
    """A real stick was stranded because this dd wrote all but the last 659,456 bytes of
    kernel.img to boot_b, exited 0, and the hook logged "kernel written". The tail of the
    zstd initramfs stayed behind from the previous kernel; u-boot loaded the image, the
    kernel could not unpack the ramdisk, freed it, and panicked with "Requested init /init
    failed (error -2)". boot_ce had already been consumed, so the next boot went to Android
    and the failure looked like the switcher app doing nothing.

    There is no cmp and no md5sum in this initramfs, so the hook cannot compare content. What
    it CAN do is stop throwing away the one report dd makes about its own work: `dd` writes
    "<N>+0 records out" to stderr, and `2>/dev/null` discarded it."""
    src = open(HOOK, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert re.search(r"/flash/kernel\.img[^\n]*\$BOOTDEV", code), (
        "the hook no longer writes /flash/kernel.img to $BOOTDEV")
    stderr_binned = [ln.strip() for ln in code.splitlines()
                     if "dd " in ln and "kernel.img" in ln and "2>/dev/null" in ln]
    assert not stderr_binned, (
        "a dd reading kernel.img still sends its stderr to /dev/null -- that stderr carries "
        f"the 'records out' count, the only evidence of a short write available here: {stderr_binned}")
    assert "records out" in code, (
        "nothing in the hook checks dd's 'records out' count, so a short kernel write is "
        "still reported as success")
    assert re.search(r'dd if="\$_src" of="\$_dst"[^\n]*2>"\$DD_LOG"', code), (
        "the verified-write helper must capture dd's stderr to inspect it")


def hook_flushes_the_source_before_reading_it():
    """The CoreELEC updater writes /flash/kernel.img and then calls this hook. If those writes
    are not on the platter yet, the hook can copy a partly-stale file to boot_<slot>. `sync`
    commits them; dropping the caches then forces the read to come from eMMC rather than from
    a page cache that may disagree. Order matters: sync BEFORE drop_caches, or the drop can
    discard the updater's not-yet-written data and the re-read returns the OLD file."""
    src = open(HOOK, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    m = re.search(r"^[^\n]*/flash/kernel\.img[^\n]*\$BOOTDEV[^\n]*$", code, re.M)
    assert m, "the hook no longer writes /flash/kernel.img to $BOOTDEV"
    pre = code[:m.start()]
    syncs = [x.start() for x in re.finditer(r"^\s*sync\s*$", pre, re.M)]
    sync_at = syncs[-1] if syncs else -1
    drop_at = pre.rfind("drop_caches")
    assert sync_at != -1, "no sync before the kernel write -- the source may not be on disk yet"
    assert drop_at != -1, (
        "nothing drops the page cache before the kernel write, so a stale cached kernel.img "
        "can be copied to boot_<slot>")
    assert sync_at < drop_at, (
        "drop_caches runs before sync; that can discard the updater's pending writes and make "
        "the re-read return the PREVIOUS kernel.img")


def hook_never_claims_an_unverified_kernel_write_succeeded():
    """The hook cannot verify content here (no cmp, no md5sum). It must say so rather than
    log a bare "kernel written", which is what made this failure invisible for two updates."""
    src = open(HOOK, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r'log\s+"\s*kernel written"', code), (
        "the hook still logs an unqualified 'kernel written'; it can only attest to the byte "
        "count dd reported, and the log should not imply more than that")


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
    check("KernelGate.kt compares the whole kernel image", kernelgate_kt_compares_the_whole_image)
    check("KernelGate.kt will not repair from an unverified source",
          kernelgate_kt_will_not_repair_from_an_unverified_source)
    check("MainActivity pre-flights the kernel before flipping boot_ce",
          mainactivity_preflights_the_kernel_before_flipping_boot_ce)
    check("MainActivity checks the kernel on the gate-repair path too",
          mainactivity_checks_the_kernel_on_the_gate_repair_path_too)
    check("user-update.sh checks dd's 'records out' on the kernel write",
          hook_checks_what_the_kernel_dd_actually_wrote)
    check("user-update.sh syncs then drops caches before reading kernel.img",
          hook_flushes_the_source_before_reading_it)
    check("user-update.sh does not claim an unverified kernel write succeeded",
          hook_never_claims_an_unverified_kernel_write_succeeded)
    check("user-update.sh is device-independent (no mmcblk0pN)", hook_is_device_independent)
    print("the gate is device-independent")
    check("gate_vars depends only on (slot, default)", gate_does_not_vary_by_device)
    check("detect_default/detect_ce_slot round-trip both defaults", both_defaults_are_distinguishable)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all boot-gate checks passed")
