# bt_sync.py -- import Android BLE remote pairings into CoreELEC (BlueZ).
#
# Android userdata is encrypted, so CoreELEC can't read bt_config.conf directly.
# The Android-side helper (installed with the dual-boot) exports it, decrypted, to
# /flash/android_bt_config.conf each Android boot. Here we parse it, keep ONLY HID
# input devices (remotes/keyboards -- audio devices are skipped), convert the BLE
# keys (LE_KEY_PENC/PID) to BlueZ format, and write them under the adapter's store.
# No MAC override is needed when CoreELEC + Android share the chip's efuse BT MAC.
import os
import subprocess
import configparser

import xbmc
import xbmcgui
import xbmcaddon

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")
EXPORT = "/flash/android_bt_config.conf"
BLUEZ_BASE = "/storage/.cache/bluetooth"


def log(m):
    xbmc.log(f"[{NAME}] {m}", xbmc.LOGINFO)


def _rev(h):
    return ''.join(reversed([h[i:i + 2] for i in range(0, len(h), 2)]))


def live_adapter_mac():
    """BlueZ stores bonds under the live adapter's MAC. Return it (upper)."""
    try:
        out = subprocess.check_output(["hciconfig"], text=True)
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("BD Address"):
                return s.split()[2].upper()
    except Exception:
        pass
    try:
        for d in os.listdir(BLUEZ_BASE):
            if len(d) == 17 and d.count(":") == 5:
                return d.upper()
    except Exception:
        pass
    return None


def is_input_remote(dev):
    """True for HID input devices (remotes/keyboards/gamepads); False for audio etc."""
    svc = dev.get("Service", "").lower()
    return ("00001812" in svc) or bool(dev.get("HidDescriptor") or dev.get("HidReport"))


def gen_info(name, pid, penc):
    irk = pid[:32].upper()
    ltk = penc[:32].upper()
    rand = int(_rev(penc[32:48]), 16)
    ediv = int(_rev(penc[48:52]), 16)
    return f"""[General]
Name={name}
Appearance=0x0180
AddressType=public
SupportedTechnologies=LE;
Trusted=true
Blocked=false
Services=00001800-0000-1000-8000-00805f9b34fb;00001801-0000-1000-8000-00805f9b34fb;0000180a-0000-1000-8000-00805f9b34fb;0000180f-0000-1000-8000-00805f9b34fb;00001812-0000-1000-8000-00805f9b34fb;00001813-0000-1000-8000-00805f9b34fb;

[IdentityResolvingKey]
Key={irk}

[LongTermKey]
Key={ltk}
Authenticated=0
EncSize=16
EDiv={ediv}
Rand={rand}

[ConnectionParameters]
MinInterval=16
MaxInterval=16
Latency=49
Timeout=500
"""


def run():
    if not os.path.exists(EXPORT):
        xbmcgui.Dialog().ok(
            NAME,
            "No Android Bluetooth export found at:\n[B]/flash/android_bt_config.conf[/B]\n\n"
            "Pair the remote in Android first. The Android-side helper (installed with the "
            "dual-boot) copies the pairing here on each Android boot -- so boot into Android "
            "once after pairing, then run this again.")
        return

    cfg = configparser.ConfigParser(strict=False)
    cfg.optionxform = str
    try:
        cfg.read(EXPORT)
    except Exception as e:
        xbmcgui.Dialog().ok(NAME, f"Could not parse the export:\n{e}")
        return

    adapter = live_adapter_mac()
    if not adapter:
        xbmcgui.Dialog().ok(NAME, "No Bluetooth adapter found (is Bluetooth enabled?).")
        return
    cfg_adapter = cfg["Adapter"]["Address"].upper() if cfg.has_section("Adapter") else None
    if cfg_adapter and cfg_adapter != adapter:
        # The remote bonded to Android's adapter MAC; if CoreELEC's differs it won't
        # reconnect. (Won't happen when both share the chip's efuse BT MAC.)
        if not xbmcgui.Dialog().yesno(
                NAME,
                f"CoreELEC BT MAC ({adapter}) differs from Android's ({cfg_adapter}).\n"
                "Remotes may not reconnect. Import anyway?"):
            return

    imported, skipped = [], []
    for sec in cfg.sections():
        d = cfg[sec]
        if not (d.get("LE_KEY_PENC") and d.get("LE_KEY_PID")):
            continue  # not a BLE bond (classic audio, the adapter section, etc.)
        name = d.get("Name", sec)
        if not is_input_remote(d):
            skipped.append(name)
            continue
        penc, pid = d["LE_KEY_PENC"], d["LE_KEY_PID"]
        if len(penc) < 52 or len(pid) < 32:
            skipped.append(name)
            continue
        dest = os.path.join(BLUEZ_BASE, adapter, sec.upper())
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "info"), "w") as f:
            f.write(gen_info(name, pid, penc))
        imported.append(name)
        log(f"imported {name} ({sec.upper()})")

    if imported:
        subprocess.run(["systemctl", "restart", "bluetooth"], check=False)

    msg = ("Imported -- now work in CoreELEC:\n - " + "\n - ".join(imported)) if imported \
        else "No remotes were imported."
    if skipped:
        msg += "\n\nSkipped (not input remotes):\n - " + "\n - ".join(skipped)
    xbmcgui.Dialog().ok(NAME, msg)
