#!/usr/bin/env python3
"""Drift tests for values written in more than one place, and for constructs known to be wrong.

Dependency-free on purpose: run it directly.

    python tests/test_drift.py

Same argument as test_boot_gate.py, applied to the rest of the tree. A value that appears
in two files, in two languages, with nothing forcing them to agree, drifts -- and here the
drift is silent, because both halves keep running and only one of them is right.

The `pm path` check is a different shape: not two copies of a value, but a construct that
looked correct and was not. It shipped, and PR #9 found it in the field on a Xiaomi TV
Box S 3rd Gen, where stage2a printed "nothing to block" and returned success while leaving
the Xiaomi OTA updater fully enabled. Once a defect has cost a real box its protection,
the pattern is worth a permanent gate rather than a memory.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

INSTALLER = os.path.join(ROOT, "installer")
MODULES = os.path.join(ROOT, "modules")

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


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def installer_sources():
    return sorted(f for f in os.listdir(INSTALLER) if f.endswith(".py"))


# install_blockgms.py gates on `GMS not in pm path ...` and is deliberately NOT covered by
# the check below: the maintainer confirmed the substring form is fine for GMS specifically.
# Scoped by filename rather than skipped globally, so the gate still holds everywhere else.
PM_PATH_EXEMPT = {"install_blockgms.py"}


# --- 1. `pm path` must never be tested by substring ---------------------------------------
def pm_path_is_not_tested_by_substring():
    """`pm path <pkg>` prints the APK's LOCATION, not the package name.

    On a unit whose system directory is not named after the package -- e.g.
    /system/priv-app/updateservice/updateservice.apk for com.xiaomi.mitv.updateservice --
    `PKG in output` is false even though the package is installed, so a presence gate
    written that way takes the "not installed" branch on a device that very much has it.

    The correct signals are emptiness (pm path prints nothing for a missing package) or
    the exit status, which is what modules/blockota/service.sh already uses.
    """
    bad = []
    # `PKG not in <anything> pm path` and the positive `PKG in <anything> pm path`
    pattern = re.compile(r"^(?P<line>.*\b(?P<var>\w+)\s+(?:not\s+)?in\b.*pm path.*)$", re.M)
    for name in installer_sources():
        if name in PM_PATH_EXEMPT:
            continue
        src = read(INSTALLER, name)
        for m in pattern.finditer(src):
            lineno = src[:m.start()].count("\n") + 1
            bad.append(f"{name}:{lineno}: {m.group('line').strip()}")
    assert not bad, (
        "`pm path` output tested by substring containment -- it prints the APK path, not "
        "the package name, so this is false on any unit whose system dir is not named "
        "after the package:\n    " + "\n    ".join(bad)
        + "\n    Test for empty output, or for the exit status, instead.")


if __name__ == "__main__":
    print("drift -- values written twice, and constructs known to be wrong")
    check("no installer gates on `pm path` by substring", pm_path_is_not_tested_by_substring)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all drift checks passed")
