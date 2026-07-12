#!/usr/bin/env python3
"""Tests for the /data quiesce + carve stability probe (flash_to_coreelec).

Dependency-free on purpose: run it directly.

    python tests/test_quiesce.py

The bug these guard against: write_all streamed the CE images into LBAs the
still-mounted PRE-carve f2fs owned. f2fs is log-structured (writes land at
allocator write pointers spread across the kernel's stale full-size userdata
span) and its background GC runs when the box is idle, so a correctly-streamed
CE_FLASH was overwritten minutes later -- a SHA read-back FAIL on a good write.
The fix: quiesce /data (stop framework + background_gc=off) before the CE
streams, but ONLY when the kernel's userdata is still old-geometry, and refuse
to stream while sampled carve windows change between two reads.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
sys.path.insert(0, os.path.join(HERE, "..", "installer"))

import devices                                                    # noqa: E402
import flash_to_coreelec as F                                     # noqa: E402

BOX_SECS = {n: (a, b, c) for n, a, b, c in devices.BOX.as_sectors()}
CARVED_UD = BOX_SECS["userdata"][2]           # 14800 MiB in sectors
STOCK_UD = devices.BOX.stock_ud_last_lba - devices.BOX.stock_ud_first_lba + 1

_FAILURES = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        _FAILURES.append(f"{name}: {e}")
        print(f"  FAIL  {name}: {e}")
    except Exception as e:                                        # noqa: BLE001
        _FAILURES.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
    else:
        print(f"  ok    {name}")


class FakeCtx(F.Ctx):
    """Ctx with su() replaced by a scripted device: no adb, records every command."""

    def __init__(self, ud_sectors, gc_off_takes=True):
        super().__init__("TESTSER", dry=True, port=5599)
        self.device = devices.BOX
        self.calls = []
        self._ud = ud_sectors                 # kernel's view of userdata (None = unreadable)
        self._gc_off = gc_off_takes

    def su(self, cmd):
        self.calls.append(cmd)
        if cmd.startswith("readlink"):
            return ("/dev/block/mmcblk0p32\n" if self._ud is not None else "\n"), 0
        if cmd.startswith("cat /sys/class/block/"):
            return (f"{self._ud}\n" if self._ud is not None else "\n"), 0
        if cmd == "cat /proc/mounts":
            opts = "rw,lazytime" + (",background_gc=off" if self._gc_off else ",background_gc=on")
            return f"/dev/block/dm-53 /data f2fs {opts} 0 0\n", 0
        return "", 0


class ProbeCtx(FakeCtx):
    """FakeCtx whose sampled-window hashes are scripted per sampling pass."""

    def __init__(self, passes):
        super().__init__(STOCK_UD)
        self._passes = list(passes)           # list of hash-lists, one per sample()

    def su(self, cmd):
        if "sha256sum" in cmd:
            self.calls.append(cmd)
            hashes = self._passes.pop(0)
            return "".join(f"{h}  -\n" for h in hashes), 0
        return super().su(cmd)


# ---- _kernel_userdata_sectors ------------------------------------------------

def t_kernel_sectors_parses():
    g = FakeCtx(STOCK_UD)
    assert g._kernel_userdata_sectors() == STOCK_UD


def t_kernel_sectors_unreadable_is_none():
    g = FakeCtx(None)
    assert g._kernel_userdata_sectors() is None


# ---- quiesce_data --------------------------------------------------------------

def t_quiesce_runs_when_old_geometry():
    g = FakeCtx(STOCK_UD)
    assert g.quiesce_data(BOX_SECS) is True
    assert "stop" in g.calls, "framework must be stopped"
    assert any(c.startswith("mount -o remount,background_gc=off") for c in g.calls), \
        "f2fs background GC must be turned off"


def t_quiesce_skipped_when_already_carved():
    g = FakeCtx(CARVED_UD)
    assert g.quiesce_data(BOX_SECS) is False
    assert "stop" not in g.calls, "must not kill the framework post-reformat"


def t_quiesce_fails_safe_when_size_unreadable():
    g = FakeCtx(None)
    assert g.quiesce_data(BOX_SECS) is True, "unreadable size must be treated as live"
    assert "stop" in g.calls


# ---- _assert_carve_quiet -------------------------------------------------------

def t_probe_passes_when_stable():
    stable = [f"{i:064x}" for i in range(6)]
    g = ProbeCtx([stable, list(stable)])
    g._assert_carve_quiet(BOX_SECS)           # must not raise


def t_probe_aborts_when_a_window_changes():
    a = [f"{i:064x}" for i in range(6)]
    b = list(a)
    b[3] = "f" * 64                           # one window moved between reads
    g = ProbeCtx([a, b])
    try:
        g._assert_carve_quiet(BOX_SECS)
    except SystemExit:
        return
    raise AssertionError("changing window must abort before streaming")


def t_probe_aborts_on_short_read():
    a = [f"{i:064x}" for i in range(6)]
    g = ProbeCtx([a[:4], a[:4]])              # device returned too few hashes
    try:
        g._assert_carve_quiet(BOX_SECS)
    except SystemExit:
        return
    raise AssertionError("short hash list must abort, not pass silently")


def t_probe_samples_stay_inside_carve():
    g = ProbeCtx([[f"{i:064x}" for i in range(6)]] * 2)
    g._assert_carve_quiet(BOX_SECS)
    cmd = next(c for c in g.calls if "sha256sum" in c)
    lo, hi = BOX_SECS["CE_FLASH"][0], BOX_SECS["CE_STORAGE"][1]
    for part in cmd.split(";"):
        for tok in part.split():
            if tok.startswith("skip="):
                skip = int(tok[5:])
                assert lo <= skip <= hi - 8192 + 1, f"sample at {skip} leaves the carve"


if __name__ == "__main__":
    print("test_quiesce:")
    for n, f in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        check(n, f)
    if _FAILURES:
        sys.exit(f"{len(_FAILURES)} failure(s)")
    print("all ok")
