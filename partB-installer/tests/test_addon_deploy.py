#!/usr/bin/env python3
"""Which addon zip stage3 actually deploys.

Dependency-free on purpose: run it directly.

    python tests/test_addon_deploy.py

deploy_toolbox_addon.py finds the addon zip by globbing artifacts/ and picking one. That
choice is the whole install: whatever it returns is what gets unpacked into Kodi's addon
tree on the box. Picking the wrong file is not a crash, it is a quiet downgrade -- Kodi
reports the addon installed either way.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "build"))
sys.path.insert(0, os.path.join(ROOT, "installer"))

import addon_zip  # noqa: E402
import deploy_toolbox_addon as D  # noqa: E402

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


def with_artifact_zips(*names):
    """A throwaway tree laid out like the repo, holding exactly these addon zips.

    Returns (tmpdir, fake_installer_dir). find_zip() resolves everything relative to the
    module's HERE, so pointing HERE at the fake installer dir is enough to redirect it.
    """
    tmp = tempfile.mkdtemp(prefix="toolbox_zip_")
    art = os.path.join(tmp, "artifacts")
    inst = os.path.join(tmp, "installer")
    os.makedirs(art)
    os.makedirs(inst)
    for n in names:
        with open(os.path.join(art, n), "wb") as fh:
            fh.write(b"PK\x03\x04")          # enough to be a file; nothing opens it here
    return tmp, inst


def picks_the_highest_version_not_the_highest_string():
    """1.1.10 is a NEWER addon than 1.1.2, and sorts BEFORE it as a string.

    Sorting the glob lexicographically and taking the last entry works for exactly as long
    as no component reaches double digits. The first release after 1.1.9 silently starts
    deploying the older zip, and keeps doing it for every release after that.
    """
    tmp, inst = with_artifact_zips(f"{D.ADDON_ID}-1.1.2.zip", f"{D.ADDON_ID}-1.1.10.zip")
    real_here = D.HERE
    try:
        D.HERE = inst
        got = os.path.basename(D.find_zip() or "")
    finally:
        D.HERE = real_here
        shutil.rmtree(tmp, ignore_errors=True)
    assert got == f"{D.ADDON_ID}-1.1.10.zip", (
        f"picked {got!r}; 1.1.10 is newer than 1.1.2 but sorts earlier as a string")


def single_zip_is_returned():
    """The ordinary case: one zip in artifacts/, and it is the one deployed."""
    tmp, inst = with_artifact_zips(f"{D.ADDON_ID}-1.1.2.zip")
    real_here = D.HERE
    try:
        D.HERE = inst
        got = os.path.basename(D.find_zip() or "")
    finally:
        D.HERE = real_here
        shutil.rmtree(tmp, ignore_errors=True)
    assert got == f"{D.ADDON_ID}-1.1.2.zip", f"picked {got!r}"


def no_zip_returns_none():
    """No zip is not an exception -- main() turns None into a readable sys.exit."""
    tmp, inst = with_artifact_zips()
    real_here = D.HERE
    try:
        D.HERE = inst
        got = D.find_zip()
    finally:
        D.HERE = real_here
        shutil.rmtree(tmp, ignore_errors=True)
    assert got is None, f"expected None with no zips present, got {got!r}"


def version_ordering_is_numeric():
    """The shared rule itself, independent of any filesystem.

    make_dist.py picks the zip that goes into the shipped bundle using the same helper, so
    a bug here ships the wrong addon to everyone, not just to one box.
    """
    cases = [
        (["a-1.1.2.zip", "a-1.1.10.zip"], "a-1.1.10.zip"),
        (["a-1.9.0.zip", "a-1.10.0.zip"], "a-1.10.0.zip"),
        (["a-2.0.0.zip", "a-10.0.0.zip"], "a-10.0.0.zip"),
        (["a-1.1.2.zip"], "a-1.1.2.zip"),
        # a prerelease must not outrank the release it precedes
        (["a-1.2.0.zip", "a-1.2.0.beta.zip"], "a-1.2.0.beta.zip"),
    ]
    bad = []
    for paths, want in cases:
        got = addon_zip.newest(paths)
        if got != want:
            bad.append(f"{paths} -> {got!r}, expected {want!r}")
    assert not bad, "version ordering wrong:\n    " + "\n    ".join(bad)
    assert addon_zip.newest([]) is None, "newest([]) should be None"


if __name__ == "__main__":
    print("addon deploy -- which zip stage3 installs")
    check("version ordering is numeric, not lexicographic", version_ordering_is_numeric)
    check("picks the highest VERSION, not the highest string", picks_the_highest_version_not_the_highest_string)
    check("a single zip is the one returned", single_zip_is_returned)
    check("no zip present returns None", no_zip_returns_none)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all addon deploy checks passed")
