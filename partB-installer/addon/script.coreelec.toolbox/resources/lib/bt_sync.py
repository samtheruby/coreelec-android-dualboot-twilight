# bt_sync.py -- import Android BLE remote pairings into CoreELEC (BlueZ).
#
# Android userdata is encrypted, so CoreELEC can't read bt_config.conf directly.
# The Android-side helper (installed with the dual-boot) exports it, decrypted, to
# /flash/android_bt_config.conf each Android boot. Here we parse it, keep ONLY HID
# input devices (remotes/keyboards -- audio devices are skipped), convert the BLE
# keys (LE_KEY_PENC/PID) to BlueZ format, and write them under the adapter's store.
# No MAC override is needed when CoreELEC + Android share the chip's efuse BT MAC.
import os
import time
import subprocess
import configparser

import xbmc
import xbmcgui
import xbmcaddon

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")
EXPORT = "/flash/android_bt_config.conf"
BLUEZ_BASE = "/storage/.cache/bluetooth"
MAIN_CONF = "/etc/bluetooth/main.conf"
MAIN_CONF_OVR = "/storage/.config/bluetooth-main.conf"
PRIVACY_UNIT = "/storage/.config/system.d/bluetooth-privacy.service"


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


def adapter_powered():
    """True while the controller is powered (HCI_UP). Kodi's Bluetooth settings
    report 'no adapter' when it is not."""
    try:
        return "UP RUNNING" in subprocess.check_output(["hciconfig"], text=True)
    except Exception:
        return False


def stop_bluetooth():
    """Stop bluetoothd and power the controller down before touching its store.

    bluetoothd caches the store in memory, so bonds written under a live daemon can
    be ignored or rewritten. Powering down matters too: the kernel REJECTS Set
    Privacy on a powered controller, so bluetoothd can only adopt the imported IRK
    if it finds the controller down at startup."""
    subprocess.run(["systemctl", "stop", "bluetooth"], check=False)
    subprocess.run(["btmgmt", "power", "off"], check=False)


def start_bluetooth():
    """Start bluetoothd and make sure the controller comes back powered.

    bluetooth.service has TimeoutStopSec=1s, so a busy bluetoothd (mid LE connect)
    is SIGKILLed on stop; the half-torn-down mgmt state has been seen to leave the
    controller powered off for good -- Kodi then shows no Bluetooth adapter at all.
    AutoEnable normally powers it back on; if it did not, do it ourselves."""
    subprocess.run(["systemctl", "start", "bluetooth"], check=False)
    for _ in range(15):
        if adapter_powered():
            return True
        time.sleep(1)
    log("bluetoothd left the controller powered off -- powering it on")
    subprocess.run(["btmgmt", "power", "on"], check=False)
    time.sleep(1)
    return adapter_powered()


def is_input_remote(dev):
    """True for HID input devices (remotes/keyboards/gamepads); False for audio etc."""
    svc = dev.get("Service", "").lower()
    return ("00001812" in svc) or bool(dev.get("HidDescriptor") or dev.get("HidReport"))


def gen_info(name, pid, penc):
    # The export is a file we parse off a FAT partition; a truncated or non-hex key would
    # otherwise raise ValueError out of int(...,16) and surface as a Kodi crash dialog
    # rather than "this bond is unusable". Callers treat None as "skip this device".
    try:
        irk = pid[:32].upper()
        ltk = penc[:32].upper()
        rand = int(_rev(penc[32:48]), 16)
        ediv = int(_rev(penc[48:52]), 16)
    except ValueError:
        return None
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


def ensure_local_identity(adapter, cfg):
    """Present Android's BLE identity, not just its device bonds.

    Android pairs with privacy on: it connects from an RPA and distributes its
    local IRK. A remote that holds the host's IRK REJECTS connections from the
    bare public address at the link layer (0x3e connect/drop loop, several per
    second, no SMP traffic at all) -- so importing the LTK alone is not enough.
    Adopt Android's local IRK as BlueZ's identity and turn privacy on so the
    kernel also connects from an RPA the remote can resolve.

    /etc is squashfs, so Privacy=device goes into a copy of main.conf that a
    oneshot unit bind-mounts before bluetooth.service starts (bluetooth.service
    itself is stripped to CAP_NET_* and cannot mount).

    Call with bluetoothd stopped and the controller down (see stop_bluetooth):
    the kernel only accepts Set Privacy on an unpowered controller."""
    irk = cfg["Adapter"].get("LE_LOCAL_KEY_IRK", "").strip().upper() \
        if cfg.has_section("Adapter") else ""
    if len(irk) == 32:
        ident = os.path.join(BLUEZ_BASE, adapter, "identity")
        want = f"[General]\nIdentityResolvingKey={irk}\n"
        try:
            cur = open(ident).read()
        except OSError:
            cur = ""
        if cur != want:
            with open(ident, "w") as f:
                f.write(want)
            os.chmod(ident, 0o600)
            log("installed Android local IRK as adapter identity")

    if not os.path.exists(MAIN_CONF_OVR) or \
            "\nPrivacy = device" not in open(MAIN_CONF_OVR).read():
        conf = open(MAIN_CONF).read()
        if "\nPrivacy = device" not in conf:
            if "#Privacy = off" in conf:
                conf = conf.replace("#Privacy = off", "Privacy = device", 1)
            else:
                conf = conf.replace("[General]", "[General]\nPrivacy = device", 1)
        with open(MAIN_CONF_OVR, "w") as f:
            f.write(conf)
        log("wrote privacy-enabled main.conf override")

    if not os.path.exists(PRIVACY_UNIT):
        os.makedirs(os.path.dirname(PRIVACY_UNIT), exist_ok=True)
        with open(PRIVACY_UNIT, "w") as f:
            f.write(
                "[Unit]\n"
                "Description=Bind privacy-enabled Bluetooth main.conf (dual-boot BLE identity)\n"
                "Before=bluetooth.service\n"
                f"ConditionPathExists={MAIN_CONF_OVR}\n"
                "\n"
                "[Service]\n"
                "Type=oneshot\n"
                "RemainAfterExit=yes\n"
                "ExecStart=/bin/sh -c \"grep -q ' /etc/bluetooth/main.conf ' /proc/mounts || "
                f"mount --bind {MAIN_CONF_OVR} /etc/bluetooth/main.conf\"\n"
                "\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n")
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "bluetooth-privacy.service"], check=False)
        log("installed + enabled bluetooth-privacy.service")
    subprocess.run(["systemctl", "start", "bluetooth-privacy.service"], check=False)


def existing_ltk(dest):
    """The LTK CoreELEC already holds for this device, or None if it has no bond."""
    p = os.path.join(dest, "info")
    if not os.path.exists(p):
        return None
    c = configparser.ConfigParser(strict=False)
    c.optionxform = str
    try:
        c.read(p)
    except Exception:
        return None
    # a native CE bond stores the peripheral's key under Peripheral/SlaveLongTermKey
    # (LE Secure Connections); an imported legacy bond uses LongTermKey.
    for sec in ("LongTermKey", "PeripheralLongTermKey", "SlaveLongTermKey"):
        if c.has_section(sec) and c[sec].get("Key"):
            return c[sec]["Key"].strip().upper()
    return None


def _verdict(trace, mac):
    """Read the encryption result for ONE device out of an btmon trace.

    btmon shows every device on the controller, so a verdict may not be taken from the trace
    as a whole -- another remote encrypting successfully in the same window would otherwise
    read as our success. Scope it to the Encryption Change event carrying our address:

        > HCI Event: Encryption Change (0x08) plen 4
                Status: PIN or Key Missing (0x06)
                Handle: 16 (LE-ACL) Address: C0:5D:39:AB:7B:49 (...)
                Encryption: Disabled (0x00)
    """
    lines = trace.splitlines()
    for i, ln in enumerate(lines):
        if "Encryption Change" not in ln:
            continue
        block = "\n".join(lines[i:i + 5]).upper()
        if mac.upper() not in block:
            continue
        if "PIN OR KEY MISSING" in block:
            return "stale"
        if "ENCRYPTION: ENABLED" in block:
            return "ok"
    return "unreachable"


def verify_bond(mac):
    """Connect once and watch the link. -> "ok" | "stale" | "unreachable".

    The failure this catches is invisible from the files: a BLE remote remembers ONE
    pairing per host, and both OSes share this box's BT address -- so if the remote was
    re-paired anywhere else after Android stored its keys, the export we just imported is
    dead. The link then comes up and the remote REJECTS the LTK:

        LE Enhanced Connection Complete -- Status: Success
        Encryption Change -- Status: PIN or Key Missing (0x06)
        Disconnect -- Reason: Authentication Failure (0x05)

    ...forever, several times a second. Nothing in BlueZ's own logs says "your key is
    stale", so read it off the HCI link itself with btmon.
    """
    try:
        mon = subprocess.Popen(["btmon"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, errors="replace")
    except OSError:
        return "unreachable"                 # no btmon -- cannot tell, do not lie about it
    try:
        subprocess.run(["bluetoothctl", "connect", mac], check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)                        # let the encryption result land in the trace
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        mon.terminate()
    try:
        trace = mon.communicate(timeout=5)[0] or ""
    except subprocess.TimeoutExpired:
        mon.kill()
        trace = ""

    return _verdict(trace, mac)              # "unreachable" == never advertised/asleep/out of range


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

    # Plan the import first, while bluetoothd is still up: a conflict has to be answered
    # BEFORE we take Bluetooth down, or the user may have no working input left to answer with.
    plan, skipped, conflicts = [], [], []
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
        info = gen_info(name, pid, penc)
        if info is None:                    # keys present but not valid hex -- unusable bond
            skipped.append(name)
            log(f"skipped {name} ({sec.upper()}): malformed LE keys")
            continue
        mac = sec.upper()
        dest = os.path.join(BLUEZ_BASE, adapter, mac)
        held = existing_ltk(dest)
        if held and held != penc[:32].upper():
            conflicts.append(name)
        plan.append((name, mac, dest, info))

    if not plan:
        msg = "No remotes were imported."
        if skipped:
            msg += "\n\nSkipped (not input remotes):\n - " + "\n - ".join(skipped)
        xbmcgui.Dialog().ok(NAME, msg)
        return

    if conflicts:
        # A BLE remote remembers ONE pairing per host, and both OSes share this box's BT
        # address -- so the remote holds the keys of whichever OS paired it LAST. Importing
        # Android's keys over a newer CoreELEC pairing kills a working remote.
        if not xbmcgui.Dialog().yesno(
                NAME,
                "CoreELEC already has its own pairing for:\n - " + "\n - ".join(conflicts) +
                "\n\nA remote remembers only ONE pairing for this box (both OSes share its "
                "Bluetooth address), so importing REPLACES it. Do this only if Android is "
                "where you paired the remote [B]last[/B] -- otherwise the imported keys are "
                "stale and the remote will connect, then drop, forever.\n\nReplace anyway?"):
            return

    stop_bluetooth()
    ensure_local_identity(adapter, cfg)
    imported = []
    for name, mac, dest, info in plan:
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "info"), "w") as f:
            f.write(info)
        imported.append((name, mac))
        log(f"imported {name} ({mac})")
    powered = start_bluetooth()

    msg = "Imported:\n - " + "\n - ".join(n for n, _ in imported)
    if skipped:
        msg += "\n\nSkipped (not input remotes):\n - " + "\n - ".join(skipped)
    if not powered:
        msg += "\n\n[COLOR red]The Bluetooth adapter did not come back on. Reboot.[/COLOR]"
        xbmcgui.Dialog().ok(NAME, msg)
        return

    # Prove the imported keys actually work, instead of leaving the user to discover a
    # connect/drop loop by themselves.
    if not xbmcgui.Dialog().yesno(NAME, msg + "\n\nTest a remote now to prove the keys work?",
                                  yeslabel="Test", nolabel="Skip"):
        return
    for name, mac in imported:
        xbmcgui.Dialog().ok(NAME, f"[B]{name}[/B]\n\nPress a button on the remote now to wake it, "
                                  "then select OK. This takes up to 30 seconds.")
        res = verify_bond(mac)
        log(f"verify {name} ({mac}): {res}")
        if res == "ok":
            xbmcgui.Dialog().ok(NAME, f"[B]{name}[/B]: connected and encrypted.\n\n"
                                      "The import works -- the remote will reconnect on its own "
                                      "from now on.")
        elif res == "stale":
            xbmcgui.Dialog().ok(
                NAME,
                f"[COLOR red][B]{name}[/B]: the remote REJECTED the imported key.[/COLOR]\n\n"
                "Android's export is stale -- the remote has been re-paired since (pairing it "
                "in CoreELEC does this too), and a remote only remembers its LAST pairing.\n\n"
                "Fix: pair the remote in [B]Android[/B] again, boot Android once so the export "
                "refreshes, then run this import again. Do not pair it in CoreELEC.")
        else:
            xbmcgui.Dialog().ok(NAME, f"[B]{name}[/B]: could not reach the remote (asleep or out "
                                      "of range), so the keys are untested. Press a button on it "
                                      "and give it a moment; if it never connects, run this test "
                                      "again.")
