#!/usr/bin/env python3
"""
Resolve the adb device serial for the installer scripts.

`--serial` accepts ANY adb serial -- a wireless `ip:port` OR a USB device id. The
transport is irrelevant: every script just runs `adb -s <serial> ...`, and
`adb forward` / push / exec-out all work the same over USB or Wi-Fi. So nothing here
is wireless-specific. When `--serial` is omitted and exactly one device is attached,
that one is used (handy for a single USB stick); with none or several, it exits with
guidance.
"""
import subprocess, sys


def list_devices():
    """[(serial, state)] from `adb devices` (state e.g. 'device', 'unauthorized', 'offline')."""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("adb not found on PATH -- install Android platform-tools")
    devs = []
    for ln in out.splitlines()[1:]:        # skip the "List of devices attached" header
        p = ln.split()
        if len(p) >= 2:
            devs.append((p[0], p[1]))
    return devs


def resolve(serial):
    """Return an explicit --serial unchanged (USB id or ip:port). Otherwise auto-pick the
    sole ready device, or exit with guidance. Works identically for USB and wireless."""
    if serial:
        return serial
    devs = list_devices()
    ready = [s for s, st in devs if st == "device"]
    if len(ready) == 1:
        print(f"  (auto-selected the only adb device: {ready[0]})")
        return ready[0]
    if not ready:
        extra = f"  seen but not ready: {devs}" if devs else ""
        sys.exit("no ready adb device. Plug in USB + enable USB debugging (authorize the "
                 "on-screen prompt), or `adb connect <ip:port>` for wireless, then retry."
                 + extra)
    sys.exit("multiple adb devices attached -- pass --serial <one of: "
             + ", ".join(ready) + "> (USB id or ip:port)")
