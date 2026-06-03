# boot_default.py -- choose which OS a normal reboot boots (Android / CoreELEC).
# Edits the u-boot env gate (bootcmd) and refreshes /flash/env_dualboot.bin so the
# choice survives CoreELEC OS updates (which reset bootcmd).
import os
import subprocess

import xbmcgui
import xbmcaddon

from resources.lib import envcodec

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")
ENV_DUALBOOT = "/flash/env_dualboot.bin"


def run():
    try:
        raw = envcodec.read_env()
    except Exception as e:
        xbmcgui.Dialog().ok(NAME, f"Cannot read u-boot env:\n{e}")
        return
    if not envcodec.crc_ok(raw):
        xbmcgui.Dialog().ok(NAME, "u-boot env CRC invalid -- aborting.")
        return
    d = envcodec.parse(raw)
    ce_slot = envcodec.detect_ce_slot(d)
    if not ce_slot:
        xbmcgui.Dialog().ok(NAME, "No CoreELEC boot gate in the env.\nRun the dual-boot installer first.")
        return
    cur = envcodec.detect_default(d)

    sel = xbmcgui.Dialog().select(f"Default boot OS  (currently: {cur.upper()})",
                                  ["Android", "CoreELEC"],
                                  preselect=(1 if cur == "coreelec" else 0))
    if sel == -1:
        return
    target = "coreelec" if sel == 1 else "android"
    if target == cur:
        xbmcgui.Dialog().notification(NAME, f"Already {target}", xbmcgui.NOTIFICATION_INFO, 3000)
        return

    d.update(envcodec.gate_vars(ce_slot, target))
    live = envcodec.serialize(d)
    # env_dualboot.bin (restored by the update hook): for android-default, boot_ce=1
    # so a CoreELEC update auto-enters the freshly-updated CE; coreelec-default already does.
    dual_d = dict(d)
    if target == "android":
        dual_d["boot_ce"] = "1"
    dual = envcodec.serialize(dual_d)
    if not (envcodec.crc_ok(live) and envcodec.crc_ok(dual)):
        xbmcgui.Dialog().ok(NAME, "Internal CRC error -- aborting.")
        return

    try:
        envcodec.write_env(live)
        if os.path.isdir("/flash"):
            subprocess.run(["mount", "-o", "remount,rw", "/flash"], check=False)
            with open(ENV_DUALBOOT, "wb") as f:
                f.write(dual)
            subprocess.run(["sync"], check=False)
            subprocess.run(["mount", "-o", "remount,ro", "/flash"], check=False)
    except Exception as e:
        xbmcgui.Dialog().ok(NAME, f"Write failed:\n{e}")
        return

    if target == "coreelec":
        tip = "A normal reboot now boots CoreELEC.\nUse 'reboot to eMMC/nand' to reach Android."
    else:
        tip = "A normal reboot now boots Android.\nUse the Reboot-to-CoreELEC app to enter CoreELEC."
    xbmcgui.Dialog().ok(NAME, f"Default boot set to [B]{target.upper()}[/B].\n\n{tip}")
