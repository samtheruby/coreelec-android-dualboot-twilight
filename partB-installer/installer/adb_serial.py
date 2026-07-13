#!/usr/bin/env python3
"""
Resolve the adb device serial for the installer scripts.

`--serial` takes the USB device id shown by `adb devices`. When `--serial` is
omitted and exactly one device is attached, that one is used (the common case for a
single USB stick); with none or several attached, it exits with guidance.
"""
import os, subprocess, sys


def _prepend_bundled_platform_tools():
    """Put a bundled platform-tools/ first on PATH so adb/fastboot resolve to it
    without a separate Android SDK install. Imported by every adb/fastboot-using
    script (they all import adb_serial), so this one hook covers them all.

    Checks, relative to this file, both the dist layout (partB-installer/platform-tools,
    one level up) and the source layout (repo-root/platform-tools, two levels up).
    Silently no-ops when absent -- callers then fall back to whatever's on PATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    exe = "adb.exe" if os.name == "nt" else "adb"
    for cand in (os.path.join(here, os.pardir, "platform-tools"),            # dist bundle
                 os.path.join(here, os.pardir, os.pardir, "platform-tools")):  # source checkout
        cand = os.path.abspath(cand)
        if os.path.isfile(os.path.join(cand, exe)):
            os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")
            return cand
    return None


_BUNDLED_PT = _prepend_bundled_platform_tools()


def list_devices():
    """[(serial, state)] from `adb devices` (state e.g. 'device', 'unauthorized', 'offline')."""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("adb not found on PATH -- install Android platform-tools "
                 "(or drop a platform-tools/ folder next to the installer)")
    devs = []
    for ln in out.splitlines()[1:]:        # skip the "List of devices attached" header
        p = ln.split()
        if len(p) >= 2:
            devs.append((p[0], p[1]))
    return devs


def resolve(serial, required=True):
    """Return an explicit --serial unchanged (USB device id). Otherwise auto-pick the
    sole ready device, or exit with guidance.

    required=False returns None instead of exiting when no device is ready. Only
    stage_unlock uses it: that stage can also run against a unit sitting in fastboot
    (no adb at all), and must not be blocked here before it gets the chance to look.
    An ambiguous choice (several devices) still exits either way -- guessing which
    box to factory-reset is not an option.
    """
    if serial:
        return serial
    devs = list_devices()
    ready = [s for s, st in devs if st == "device"]
    if len(ready) == 1:
        print(f"  (auto-selected the only adb device: {ready[0]})")
        return ready[0]
    if not ready:
        if not required:
            return None
        extra = f"  seen but not ready: {devs}" if devs else ""
        sys.exit("no ready adb device. Plug in USB + enable USB debugging (authorize the "
                 "on-screen prompt), then retry." + extra)
    sys.exit("multiple adb devices attached -- pass --serial <one of: "
             + ", ".join(ready) + "> (USB device id)")
