# wifi_mac.py -- set/override the CoreELEC WiFi MAC via wifi.cfg (MacOverride/MacAddr).
#
# Needed ONLY on devices where CoreELEC ships a wrong/shared default WiFi MAC. On most
# boxes the WiFi chip provides the correct unique MAC from its efuse and this is unneeded.
import os
import re
import shutil

import xbmcgui
import xbmcaddon

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")
TEMPLATE = "/usr/lib/kernel-overlays/base/lib/firmware/wifi.cfg"
DEST = "/storage/.config/firmware/wifi.cfg"
MACS_EXPORT = "/flash/android_macs.conf"     # Android-side helper writes WIFI_MAC= here
MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _android_wifi_mac():
    try:
        for line in open(MACS_EXPORT):
            if line.strip().startswith("WIFI_MAC="):
                return line.strip().split("=", 1)[1].strip().lower()
    except Exception:
        pass
    return None


def _ensure_cfg():
    if os.path.exists(DEST):
        return True
    if os.path.exists(TEMPLATE):
        os.makedirs(os.path.dirname(DEST), exist_ok=True)
        shutil.copyfile(TEMPLATE, DEST)
        return True
    return False


def _write_cfg(mac, override):
    if not _ensure_cfg():
        xbmcgui.Dialog().ok(NAME, "wifi.cfg template not found -- this device's WiFi driver "
                                  "may not support MAC override.")
        return False
    out, seen_ov, seen_mac = [], False, False
    for ln in open(DEST).read().splitlines():
        s = ln.strip()
        if s.startswith("MacOverride"):
            out.append(f"MacOverride {1 if override else 0}")
            seen_ov = True
        elif s.startswith("MacAddr") and mac:
            out.append(f"MacAddr {mac}")
            seen_mac = True
        else:
            out.append(ln)
    if not seen_ov:
        out.append(f"MacOverride {1 if override else 0}")
    if mac and not seen_mac:
        out.append(f"MacAddr {mac}")
    open(DEST, "w").write("\n".join(out) + "\n")
    return True


def _apply(mac):
    mac = mac.strip().lower()
    if not MAC_RE.match(mac):
        xbmcgui.Dialog().ok(NAME, f"Invalid MAC address:\n{mac}")
        return
    if _write_cfg(mac, True):
        xbmcgui.Dialog().ok(NAME, f"WiFi MAC set to [B]{mac}[/B].\n\nReboot to apply -- WiFi "
                                  "drops and you'll reconnect (may need the WiFi password again).")


def _manual():
    mac = xbmcgui.Dialog().input("Enter WiFi MAC (AA:BB:CC:DD:EE:FF)", type=xbmcgui.INPUT_ALPHANUM)
    if mac.strip():
        _apply(mac)


def _reset():
    if _write_cfg(None, False):
        xbmcgui.Dialog().ok(NAME, "Override disabled -- CoreELEC will use the WiFi chip's "
                                  "hardware MAC.\n\nReboot to apply.")


def run():
    amac = _android_wifi_mac()
    actions = []
    if amac:
        actions.append((f"Use Android WiFi MAC ({amac})", lambda: _apply(amac)))
    actions.append(("Enter MAC manually", _manual))
    actions.append(("Reset to hardware MAC (no override)", _reset))

    sel = xbmcgui.Dialog().select("Fix WiFi MAC  (usually not needed)", [a[0] for a in actions])
    if sel != -1:
        actions[sel][1]()
