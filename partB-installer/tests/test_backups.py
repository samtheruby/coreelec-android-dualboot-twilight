#!/usr/bin/env python3
"""Tests for the verified pre-write backup pulls (flash_to_coreelec.backups_to_pc).

Dependency-free on purpose: run it directly.

    python tests/test_backups.py

The bug these guard against: pull() ran `dd ... 2>/dev/null | base64`, so a dd
failure or a truncated/corrupted adb transfer silently produced a short (even
0-byte) *_pre.bin that was only discovered at RESTORE time -- after the device
had already been flashed. Every pull is now verified end-to-end: the SHA-256 of
the bytes that landed on the PC must equal an independent on-device hash of the
same region, and any mismatch aborts BEFORE the first write.
"""
import base64
import hashlib
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
sys.path.insert(0, os.path.join(HERE, "..", "installer"))

import devices                                                    # noqa: E402
import flash_to_coreelec as F                                     # noqa: E402

PART_BYTES = 4096            # fake size for whole-partition pulls (boot/dtbo/...)

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


_DD = re.compile(r"dd if=(\S+) bs=512 skip=(\d+) count=(\d+)")


class BackupCtx(F.Ctx):
    """Ctx with a scripted device: deterministic per-sector content, no adb.

    Transport faults are injected on the su_bytes (pull) path ONLY, so the
    on-device hash still reflects the true region -- exactly what a dd error
    or corrupted adb transfer looks like.
    """

    def __init__(self, tmpdir, truncate=None, corrupt=None,
                 empty_sha=False, bad_size=False, no_magic=False):
        super().__init__("TESTSER", dry=True, port=5599)
        self.device = devices.BOX
        self.backup_dir = tmpdir
        self._truncate = truncate     # substring of dd_if: transfer comes back short
        self._corrupt = corrupt       # substring of dd_if: transfer comes back bit-flipped
        self._empty_sha = empty_sha   # device sha256sum returns nothing
        self._bad_size = bad_size     # blockdev --getsize64 fails
        self._no_magic = no_magic     # GPT primary lacks "EFI PART"

    def region(self, dd_if, skip, count):
        raw = bytearray()
        for s in range(skip, skip + count):
            raw += hashlib.sha256(f"{dd_if}:{s}".encode()).digest() * 16   # 512 B/sector
        if dd_if == F.DISK and skip == 0 and not self._no_magic:
            raw[512:520] = b"EFI PART"
        return bytes(raw)

    def su_bytes(self, cmd):
        m = _DD.search(cmd)
        assert m and "base64" in cmd, f"unexpected su_bytes: {cmd}"
        dd_if, skip, count = m.group(1), int(m.group(2)), int(m.group(3))
        data = self.region(dd_if, skip, count)
        if self._truncate and self._truncate in dd_if:
            data = data[:len(data) // 2]
        if self._corrupt and self._corrupt in dd_if:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return base64.b64encode(data)

    def su(self, cmd):
        if cmd.startswith("blockdev --getsize64"):
            return ("blockdev: not found\n", 127) if self._bad_size else (f"{PART_BYTES}\n", 0)
        if "sha256sum" in cmd:
            if self._empty_sha:
                return "", 0
            m = _DD.search(cmd)
            h = hashlib.sha256(self.region(m.group(1), int(m.group(2)),
                                           int(m.group(3)))).hexdigest()
            return f"{h}  -\n", 0
        return "", 0


def run_pull(**kw):
    tmp = tempfile.mkdtemp(prefix="test_backups_")
    try:
        g = BackupCtx(tmp, **kw)
        g.backups_to_pc("_a")
        return tmp, g, sorted(os.listdir(tmp))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def expect_abort(reason, **kw):
    try:
        tmp, _, _ = run_pull(**kw)
    except SystemExit:
        return
    shutil.rmtree(tmp, ignore_errors=True)
    raise AssertionError(f"{reason} must abort, not pass silently")


# ---- good path -----------------------------------------------------------------

def t_good_pull_writes_all_files():
    tmp, g, names = run_pull()
    try:
        want = ["boot_a_pre.bin", "dtbo_a_pre.bin", "env_pre.bin", "frp_pre.bin",
                "gpt_backup_pre.bin", "gpt_primary_pre.bin", "misc_pre.bin",
                "reserved_pre.bin"]
        assert names == want, f"files {names}"
        sizes = {"gpt_primary_pre.bin": 34 * 512,
                 "gpt_backup_pre.bin": devices.BOX.gpt_backup_span * 512,
                 "env_pre.bin": 128 * 512, "misc_pre.bin": 64 * 512}
        for n in names:
            got = os.path.getsize(os.path.join(tmp, n))
            assert got == sizes.get(n, PART_BYTES), f"{n}: {got} B"
        raw = open(os.path.join(tmp, "boot_a_pre.bin"), "rb").read()
        assert raw == g.region("/dev/block/by-name/boot_a", 0, PART_BYTES // 512), \
            "pulled bytes differ from the device region"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- transport faults must abort before any write -------------------------------

def t_truncated_transfer_aborts():
    expect_abort("truncated transfer", truncate="env")


def t_corrupt_transfer_aborts():
    expect_abort("corrupted transfer", corrupt="boot_a")


def t_empty_device_hash_aborts():
    expect_abort("empty on-device hash", empty_sha=True)


def t_unsizable_partition_aborts():
    expect_abort("unsizable partition", bad_size=True)


def t_bad_gpt_magic_aborts():
    # consistent transfer (PC hash == device hash) but the GPT itself is garbage
    expect_abort("missing EFI PART magic", no_magic=True)


if __name__ == "__main__":
    print("test_backups:")
    for n, f in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        check(n, f)
    if _FAILURES:
        sys.exit(f"{len(_FAILURES)} failure(s)")
    print("all ok")
