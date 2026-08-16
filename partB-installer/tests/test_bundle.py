#!/usr/bin/env python3
"""The SHA256SUMS gate that runs before anything is flashed.

Dependency-free on purpose: run it directly.

    python tests/test_bundle.py

bundle.verify() is the only thing that proves a file on the PC is the file that was built,
and it runs before the first write to a boot-critical region. Its own docstring is careful
about the distinction that matters here: a return of 0 means "nothing could be checked",
NOT "all good". A caller that reads 0 as success has no integrity check at all and cannot
tell, which is exactly why the zero cases are pinned below alongside the mismatch case.
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))

import bundle  # noqa: E402

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


class FakeBundle:
    """A throwaway bundle root, with bundle's module-level ROOT/SUMS pointed at it.

    bundle resolves both at import time from its own location, so a test has to redirect
    them; everything else about the module is exercised for real.
    """

    def __init__(self, files, manifest=None, binary_marker=False, backslashes=False):
        self.dir = tempfile.mkdtemp(prefix="bundle_")
        self.paths = {}
        for name, data in files.items():
            p = os.path.join(self.dir, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
            self.paths[name] = p
        if manifest is not None:
            lines = []
            for name, digest in manifest.items():
                shown = name.replace("/", "\\") if backslashes else name
                lines.append(f"{digest}  {'*' if binary_marker else ''}{shown}")
            with open(os.path.join(self.dir, "SHA256SUMS.txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    def __enter__(self):
        self._root, self._sums = bundle.ROOT, bundle.SUMS
        bundle.ROOT = self.dir
        bundle.SUMS = os.path.join(self.dir, "SHA256SUMS.txt")
        return self

    def __exit__(self, *exc):
        bundle.ROOT, bundle.SUMS = self._root, self._sums
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def sha(data):
    return hashlib.sha256(data).hexdigest()


def no_manifest_checks_nothing():
    """A source checkout ships no SHA256SUMS.txt. That must return 0, not pass silently
    as though the file had been verified."""
    with FakeBundle({"a.img": b"payload"}) as b:
        assert bundle.verify(b.paths["a.img"], log=None) == 0


def a_matching_file_is_counted():
    data = b"payload"
    with FakeBundle({"a.img": data}, manifest={"a.img": sha(data)}) as b:
        assert bundle.verify(b.paths["a.img"], log=None) == 1


def a_mismatch_aborts():
    """The whole point: a corrupt or modified file must stop the run before it is flashed."""
    with FakeBundle({"a.img": b"tampered"}, manifest={"a.img": sha(b"original")}) as b:
        try:
            bundle.verify(b.paths["a.img"], log=None)
        except SystemExit as e:
            assert "MISMATCH" in str(e), f"unhelpful abort message: {e}"
            return
        raise AssertionError("a file whose hash does not match the manifest was accepted")


def a_file_absent_from_the_manifest_is_not_counted():
    """An init_boot the user patched themselves is not in the manifest. Skipping it is
    correct -- counting it as checked would be a lie."""
    data = b"payload"
    with FakeBundle({"a.img": data, "mine.img": b"custom"},
                    manifest={"a.img": sha(data)}) as b:
        assert bundle.verify(b.paths["mine.img"], log=None) == 0
        assert bundle.verify([b.paths["a.img"], b.paths["mine.img"]], log=None) == 1


def a_missing_file_is_skipped_not_failed():
    data = b"payload"
    with FakeBundle({"a.img": data}, manifest={"a.img": sha(data)}) as b:
        gone = os.path.join(b.dir, "not_here.img")
        assert bundle.verify(gone, log=None) == 0


def a_single_path_and_a_list_both_work():
    data = b"payload"
    with FakeBundle({"a.img": data}, manifest={"a.img": sha(data)}) as b:
        assert bundle.verify(b.paths["a.img"], log=None) == 1
        assert bundle.verify([b.paths["a.img"]], log=None) == 1


def sha256sum_binary_marker_is_stripped():
    """sha256sum writes '*path' for a binary file. Every image in this bundle is binary,
    so failing to strip that marker would silently skip all of them."""
    data = b"\x00\x01binary"
    with FakeBundle({"a.img": data}, manifest={"a.img": sha(data)}, binary_marker=True) as b:
        assert bundle.verify(b.paths["a.img"], log=None) == 1


def windows_separators_in_the_manifest_still_match():
    data = b"payload"
    with FakeBundle({"sub/a.img": data}, manifest={"sub/a.img": sha(data)},
                    backslashes=True) as b:
        assert bundle.verify(b.paths["sub/a.img"], log=None) == 1


if __name__ == "__main__":
    print("bundle -- the SHA256SUMS gate before the first write")
    check("no manifest checks nothing (returns 0)", no_manifest_checks_nothing)
    check("a matching file is counted", a_matching_file_is_counted)
    check("a mismatch aborts the run", a_mismatch_aborts)
    check("a file absent from the manifest is not counted", a_file_absent_from_the_manifest_is_not_counted)
    check("a missing file is skipped, not failed", a_missing_file_is_skipped_not_failed)
    check("a single path and a list both work", a_single_path_and_a_list_both_work)
    check("sha256sum's '*' binary marker is stripped", sha256sum_binary_marker_is_stripped)
    check("backslash paths in the manifest still match", windows_separators_in_the_manifest_still_match)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all bundle checks passed")
