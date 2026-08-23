#!/usr/bin/env python3
"""The shared Magisk module installer, with the device calls stubbed.

Dependency-free on purpose: run it directly.

    python tests/test_magisk_module.py

magisk_module.py exists because blockota, blockgms and toolbox_export each grew their own
copy of this and drifted -- its docstring records that only one of the three checked that
the module source had been found (the other two raised TypeError: join(None, ...) instead
of saying so), and that NONE of them checked what actually landed on the device.

Both of those are behaviours, not implementation details, so both are pinned here. The
read-back matters most: service.sh runs as ROOT on every boot, and the module refuses to
leave a half-written root boot script in place. A test that let that refusal rot would let
the most privileged file this installer writes go unverified.

adb and su are stubbed. Nothing here talks to a device; what is under test is the decision
logic around those calls.
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))

import magisk_module as M  # noqa: E402

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


def quiet(*_a, **_kw):
    """install() calls log() unconditionally -- unlike bundle.verify(), it does not accept
    None. Swallow the progress lines rather than changing the module to suit the test."""


class Ok:
    """A subprocess.run result that succeeded."""
    returncode = 0
    stdout = b""
    stderr = b""


def module_dir(**files):
    d = tempfile.mkdtemp(prefix="magisk_mod_")
    for name, data in files.items():
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(data)
    return d


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Device:
    """Stubs magisk_module's adb push and su with a fake device.

    `hashes` is what `sha256sum` will report for the placed files -- so a test can say
    "the bytes that landed are not the bytes that were sent" without a device.
    """

    def __init__(self, hashes, placed=True):
        self.hashes = hashes
        self.placed = placed

    def __enter__(self):
        self._su, self._run = M.su, M.subprocess.run
        M.subprocess.run = lambda *a, **kw: Ok()

        def fake_su(serial, cmd):
            if "sha256sum" in cmd:
                lines = [f"{h}  /data/adb/modules/x/{f}" for f, h in self.hashes.items()]
                return "\n".join(lines) + "\n", 0
            return ("PLACED\n" if self.placed else "\n"), 0

        M.su = fake_su
        return self

    def __exit__(self, *exc):
        M.su, M.subprocess.run = self._su, self._run
        return False


# --- find_source ---------------------------------------------------------------------------
def find_source_prefers_the_first_candidate_that_exists():
    """Repo layout first, shipped-bundle layout second -- order is the contract."""
    a = module_dir(**{"module.prop": b"id=x"})
    b = module_dir(**{"module.prop": b"id=x"})
    try:
        assert M.find_source(a, b) == a, "the first candidate holding module.prop must win"
        assert M.find_source(os.path.join(a, "nope"), b) == b, "should fall through to b"
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def find_source_returns_none_when_nothing_matches():
    """None is the signal install() turns into a readable error -- it must not be an
    exception here, and a directory without module.prop is not a module."""
    empty = module_dir()
    try:
        assert M.find_source(empty) is None, "a dir with no module.prop is not a source"
        assert M.find_source(None, os.path.join(empty, "missing")) is None
    finally:
        shutil.rmtree(empty, ignore_errors=True)


# --- install: refusals before anything is pushed ---------------------------------------------
def install_says_so_when_the_source_was_not_found():
    """The documented old bug: two of the three copies raised TypeError: join(None, ...)
    instead of saying the module source was missing."""
    try:
        M.install("SERIAL", None, "blockota_twilight", log=quiet)
    except SystemExit as e:
        assert "not found" in str(e), f"unhelpful message: {e}"
        return
    except TypeError as e:
        raise AssertionError(f"raised TypeError instead of explaining: {e}")
    raise AssertionError("install accepted a source of None")


def install_refuses_an_incomplete_module():
    src = module_dir(**{"module.prop": b"id=x"})      # no service.sh
    try:
        M.install("SERIAL", src, "x", log=quiet)
    except SystemExit as e:
        assert "service.sh" in str(e), f"should name the missing file: {e}"
        return
    finally:
        shutil.rmtree(src, ignore_errors=True)
    raise AssertionError("install accepted a module with no service.sh")


# --- install: the read-back ------------------------------------------------------------------
def install_verifies_the_bytes_that_landed():
    prop, svc = b"id=x\n", b"#!/system/bin/sh\ntrue\n"
    src = module_dir(**{"module.prop": prop, "service.sh": svc})
    try:
        with Device({"module.prop": sha(prop), "service.sh": sha(svc)}):
            got = M.install("SERIAL", src, "x", log=quiet)
        assert got == "/data/adb/modules/x", f"returned {got!r}"
    finally:
        shutil.rmtree(src, ignore_errors=True)


def install_aborts_when_the_device_copy_differs():
    """A truncated push must not leave a half-written ROOT boot script registered."""
    prop, svc = b"id=x\n", b"#!/system/bin/sh\ntrue\n"
    src = module_dir(**{"module.prop": prop, "service.sh": svc})
    try:
        with Device({"module.prop": sha(prop), "service.sh": sha(b"truncated")}):
            M.install("SERIAL", src, "x", log=quiet)
    except SystemExit as e:
        assert "service.sh" in str(e), f"should name the bad file: {e}"
        return
    finally:
        shutil.rmtree(src, ignore_errors=True)
    raise AssertionError("install accepted a device copy that did not match what was sent")


def install_aborts_when_placement_did_not_confirm():
    """The placement command echoes PLACED. No PLACED means the cp/chmod chain broke."""
    prop, svc = b"id=x\n", b"#!/system/bin/sh\ntrue\n"
    src = module_dir(**{"module.prop": prop, "service.sh": svc})
    try:
        with Device({"module.prop": sha(prop), "service.sh": sha(svc)}, placed=False):
            M.install("SERIAL", src, "x", log=quiet)
    except SystemExit as e:
        assert "placement failed" in str(e), f"unhelpful message: {e}"
        return
    finally:
        shutil.rmtree(src, ignore_errors=True)
    raise AssertionError("install continued after placement did not confirm")


def install_aborts_when_a_file_is_missing_on_the_device():
    """sha256sum reporting nothing for service.sh is not 'unchanged' -- it is absent."""
    prop, svc = b"id=x\n", b"#!/system/bin/sh\ntrue\n"
    src = module_dir(**{"module.prop": prop, "service.sh": svc})
    try:
        with Device({"module.prop": sha(prop)}):        # service.sh never reported
            M.install("SERIAL", src, "x", log=quiet)
    except SystemExit as e:
        assert "service.sh" in str(e), f"should name the missing file: {e}"
        return
    finally:
        shutil.rmtree(src, ignore_errors=True)
    raise AssertionError("install accepted a module whose service.sh is not on the device")


if __name__ == "__main__":
    print("magisk_module -- the shared module installer")
    check("find_source prefers the first candidate that exists", find_source_prefers_the_first_candidate_that_exists)
    check("find_source returns None when nothing matches", find_source_returns_none_when_nothing_matches)
    check("install explains a source of None instead of TypeError", install_says_so_when_the_source_was_not_found)
    check("install refuses a module with no service.sh", install_refuses_an_incomplete_module)
    check("install verifies the bytes that landed", install_verifies_the_bytes_that_landed)
    check("install aborts when the device copy differs", install_aborts_when_the_device_copy_differs)
    check("install aborts when placement did not confirm", install_aborts_when_placement_did_not_confirm)
    check("install aborts when a file is missing on the device", install_aborts_when_a_file_is_missing_on_the_device)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all magisk_module checks passed")
