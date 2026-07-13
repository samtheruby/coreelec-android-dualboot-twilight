# hdr_fix.py -- repair the boot gate so HDR10/HLG is not tonemapped to SDR.
#
# On the eMMC boot path the bootloader regenerates bootargs carrying "hdmitx=,444,8bit",
# which pins the HDMI attr to 8-bit; the kernel then cannot switch to 10/12-bit and the VPP
# tonemaps HDR10/HLG down to SDR. A trailing (empty) "hdmitx=" in the gate's bootargs clears
# the pin. Installs made before that fix carry the old gate and need it rewritten -- and the
# gate lives in the u-boot env, so nothing at runtime in CoreELEC can fix it (the amhdmitx
# 'attr' node is read-only; the pin arrives on the kernel command line).
#
# This rewrites ONLY the gate, keeping the current default boot OS.
import os
import subprocess

import xbmc
import xbmcgui
import xbmcaddon

from resources.lib import envcodec

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")
ENV_DUALBOOT = "/flash/env_dualboot.bin"


def log(m):
    xbmc.log(f"[{NAME}] {m}", xbmc.LOGINFO)


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
    default = envcodec.detect_default(d)

    want = envcodec.gate_vars(ce_slot, default)
    if d.get("bootcefromemmc") == want["bootcefromemmc"]:
        xbmcgui.Dialog().ok(
            NAME,
            "The boot gate is [B]already correct[/B] -- nothing to fix.\n\n"
            "If HDR still plays as SDR, this is not the cause: check that the kernel command "
            "line ends with a bare 'hdmitx=' (cat /proc/cmdline), and that the TV/HDMI cable "
            "carry HDR at the current mode.")
        return

    if not xbmcgui.Dialog().yesno(
            NAME,
            "The boot gate pins HDMI to 8-bit, so HDR10/HLG is tonemapped to SDR.\n\n"
            f"Rewrite the gate (default boot OS stays [B]{default.upper()}[/B])?"):
        return

    d.update(want)
    live = envcodec.serialize(d)
    # env_dualboot.bin is what the CoreELEC OS-update hook restores, so it must carry the
    # fixed gate too -- otherwise the next OS update puts the 8-bit pin back. Same rule as
    # boot_default: for an android-default box, boot_ce=1 so a CE update re-enters CoreELEC.
    dual_d = dict(d)
    if default == "android":
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

    log(f"boot gate rewritten with hdmitx= (slot {ce_slot}, default {default})")
    xbmcgui.Dialog().ok(
        NAME,
        "Boot gate fixed.\n\n[B]Reboot for it to take effect.[/B]\n\n"
        "After the reboot, 'cat /proc/cmdline' ends with a bare 'hdmitx=' and HDR10/HLG "
        "plays as HDR instead of SDR.")
