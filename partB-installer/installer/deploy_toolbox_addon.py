#!/usr/bin/env python3
"""
Install the "CoreELEC Toolbox" Kodi addon on a RUNNING CoreELEC (over SSH) and make
Kodi ready to install 3rd-party add-ons from zip.

Generic -- works on ANY CoreELEC box (the addon's features: sync Bluetooth remotes
from Android, set default boot OS, fix WiFi MAC). No Xiaomi specifics.

Steps:
  1. Extract script.coreelec.toolbox-*.zip into /storage/.kodi/addons/ (under /storage,
     so it survives CoreELEC OS updates) and rescan (`UpdateLocalAddons`).
  2. Configure Kodi via JSON-RPC (best-effort; turns Kodi's web server on first if off):
       - addons.unknownsources = true     -> "Unknown sources" (install zips at all)
       - addons.updatemode     = 1        -> "Update official add-ons from: Any repositories"
       - enable the toolbox addon         -> a fresh local addon lands DISABLED otherwise
  Falls back to printing the GUI steps if the JSON-RPC path can't run.

Setting IDs/values verified against CoreELEC's Kodi tree (CoreELEC/xbmc, Kodi 22):
  addons.unknownsources (boolean); addons.updatemode (integer: 0=OFFICIAL_ONLY, 1=ANY_REPOSITORY).

  python deploy_toolbox_addon.py --host 192.168.1.195 [--restart] [--no-configure]

Needs paramiko (pip install paramiko).
"""
import argparse, glob, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ID = "script.coreelec.toolbox"
ADDONS_DIR = "/storage/.kodi/addons"
GUISETTINGS = "/storage/.kodi/userdata/guisettings.xml"
WEB_USER = "kodi"          # Kodi web server username is always "kodi"
WEB_PASS = "kodi"          # default; Kodi 18+ requires a non-empty password. Override --webpass


def find_zip():
    # repo: ../script.coreelec.toolbox-*.zip   bundle: ../artifacts/ or alongside
    for pat in (os.path.join(HERE, "..", f"{ADDON_ID}-*.zip"),
                os.path.join(HERE, "..", "artifacts", f"{ADDON_ID}-*.zip"),
                os.path.join(HERE, f"{ADDON_ID}-*.zip")):
        hit = sorted(glob.glob(pat))
        if hit:
            return hit[-1]
    return None


def rpc(sh, method, params=None, pw=WEB_PASS):
    """One JSON-RPC call to the local Kodi web server (curl on the device). The JSON
    has only double quotes, so single-quoting it for the shell is safe."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}})
    return sh(f"curl -s -m 8 --user {WEB_USER}:{pw} -H 'Content-Type: application/json' "
              f"--data '{payload}' http://127.0.0.1:8080/jsonrpc")


def ping(sh, pw=WEB_PASS):
    return '"result":"pong"' in rpc(sh, "JSONRPC.Ping", pw=pw).replace(" ", "")


def wait_ping(sh, pw=WEB_PASS, secs=45):
    for _ in range(secs):
        time.sleep(1)
        if ping(sh, pw=pw):
            return True
    return False


def patch_guisettings(cli, web_pass=WEB_PASS):
    """Called while Kodi is STOPPED (else Kodi rewrites guisettings on exit and clobbers
    it). Sets the web server on (so JSON-RPC works) + creds + unknown sources + update-
    from-any-repo directly in guisettings.xml. Returns True if the file was edited."""
    import xml.etree.ElementTree as ET
    sftp = cli.open_sftp()
    try:
        data = sftp.open(GUISETTINGS).read()
    except IOError:
        sftp.close()
        return False                       # no guisettings yet -> can't reliably edit
    root = ET.fromstring(data)

    def setval(sid, val):
        for s in root.findall("setting"):
            if s.get("id") == sid:
                s.text = val
                return
        e = ET.SubElement(root, "setting")
        e.set("id", sid)
        e.text = val

    setval("services.webserver", "true")
    setval("services.webserverport", "8080")
    setval("services.webserverusername", WEB_USER)
    setval("services.webserverpassword", web_pass)   # Kodi 18+ needs a non-empty pass
    setval("addons.unknownsources", "true")
    setval("addons.updatemode", "1")
    f = sftp.open(GUISETTINGS, "w")
    f.write(ET.tostring(root, encoding="unicode"))
    f.close()
    sftp.close()
    return True


def configure_kodi(cli, sh, web_pass=WEB_PASS, candidates=("kodi", "123")):
    """Best-effort: ensure the web server, then enable unknown sources + update-from-any-
    repo + the toolbox addon, all via JSON-RPC. Returns a results dict. Never raises.

    The web server may already be on with auth set to 'kodi' or '123' -- probe both. If
    neither answers, force known creds (web_pass) into guisettings while Kodi is stopped."""
    res = {"webserver": False, "unknownsources": False, "updatemode": False, "addon": False}
    try:
        pw = next((c for c in candidates if ping(sh, pw=c)), None)
        if pw is None:
            # web server off (or unknown pass) -> force web_pass + the add-on settings while stopped
            sh("systemctl stop kodi")
            patch_guisettings(cli, web_pass=web_pass)
            sh("systemctl start kodi")
            if wait_ping(sh, pw=web_pass):
                pw = web_pass
        if pw is None:
            return res
        res["webserver"] = True

        # set the two add-on settings live (covers the web-server-already-on path; a no-op
        # if patch_guisettings already set them). unknownsources FIRST -- updatemode is a
        # child of it and is only writable once unknown sources is enabled.
        rpc(sh, "Settings.SetSettingValue", {"setting": "addons.unknownsources", "value": True}, pw=pw)
        rpc(sh, "Settings.SetSettingValue", {"setting": "addons.updatemode", "value": 1}, pw=pw)
        res["unknownsources"] = '"value":true' in rpc(
            sh, "Settings.GetSettingValue", {"setting": "addons.unknownsources"}, pw=pw).replace(" ", "")
        res["updatemode"] = '"value":1' in rpc(
            sh, "Settings.GetSettingValue", {"setting": "addons.updatemode"}, pw=pw).replace(" ", "")

        def try_enable():
            rpc(sh, "Addons.SetAddonEnabled", {"addonid": ADDON_ID, "enabled": True}, pw=pw)
            return '"enabled":true' in rpc(sh, "Addons.GetAddonDetails",
                    {"addonid": ADDON_ID, "properties": ["enabled"]}, pw=pw).replace(" ", "")

        ok = try_enable()
        if not ok:                          # fresh addon maybe not registered yet -> restart, retry
            sh("systemctl restart kodi")
            wait_ping(sh, pw=pw)
            ok = try_enable()
        res["addon"] = ok
    except Exception as ex:                 # noqa: BLE001 -- configure is best-effort
        print(f"  (configure hit an error: {ex})")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.195")
    ap.add_argument("--user", default="root")
    ap.add_argument("--pass", dest="pw", default="coreelec")
    ap.add_argument("--restart", action="store_true",
                    help="restart Kodi after extract (instead of live UpdateLocalAddons)")
    ap.add_argument("--no-configure", dest="no_configure", action="store_true",
                    help="skip the JSON-RPC configure (unknown sources / update mode / enable)")
    ap.add_argument("--webpass", default=WEB_PASS,
                    help=f"Kodi web server password to set/expect (default {WEB_PASS!r}); "
                         "user is always 'kodi'. 'kodi' and '123' are both probed.")
    a = ap.parse_args()
    try:
        import paramiko
    except ImportError:
        sys.exit("paramiko not installed -- pip install paramiko")

    zpath = find_zip()
    if not zpath:
        sys.exit(f"{ADDON_ID}-*.zip not found (build it / place it next to the installer)")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(a.host, username=a.user, password=a.pw, timeout=15,
                look_for_keys=False, allow_agent=False)

    def sh(cmd, t=60):
        _, o, e = cli.exec_command(cmd, timeout=t)
        out = o.read().decode(errors="replace")
        o.channel.recv_exit_status()
        return out + e.read().decode(errors="replace")

    sftp = cli.open_sftp()
    sftp.put(zpath, "/storage/_toolbox_addon.zip")
    sftp.close()
    print(f"  uploaded {os.path.basename(zpath)}")

    out = sh(f"mkdir -p {ADDONS_DIR} && unzip -oq /storage/_toolbox_addon.zip -d {ADDONS_DIR} "
             f"&& rm -f /storage/_toolbox_addon.zip && echo EXTRACT_OK")
    if "EXTRACT_OK" not in out:
        cli.close(); sys.exit(f"extract failed: {out}")
    ver = sh(f"grep -v '<?xml' {ADDONS_DIR}/{ADDON_ID}/addon.xml | "
             "sed -n 's/.*version=\"\\([0-9.]*\\)\".*/\\1/p' | head -1").strip()
    print(f"  extracted -> {ADDONS_DIR}/{ADDON_ID}  (version {ver or '?'})")

    if a.restart:
        sh("systemctl restart kodi")
        print("  Kodi restarted")
    else:
        sh("kodi-send -a UpdateLocalAddons 2>/dev/null")
        print("  Kodi rescanned local addons (UpdateLocalAddons)")

    r = {"addon": False, "unknownsources": False, "updatemode": False, "webserver": False}
    if not a.no_configure:
        cands = ("kodi", "123") if a.webpass in ("kodi", "123") else (a.webpass, "kodi", "123")
        r = configure_kodi(cli, sh, web_pass=a.webpass, candidates=cands)
        print(f"  unknown sources: {'ON' if r['unknownsources'] else 'FAILED'}  |  "
              f"update from any repo: {'ON' if r['updatemode'] else 'FAILED'}  |  "
              f"toolbox addon: {'ENABLED' if r['addon'] else 'NOT enabled'}")
        if r["webserver"]:
            print("  (Kodi web server is on at :8080 -- used to apply these; disable in "
                  "Settings > Services > Control if you don't want it.)")
    cli.close()

    if r["addon"] and r["unknownsources"]:
        print(f"OK -- {ADDON_ID} installed + enabled; Kodi ready to install zips. "
              "Find the addon under Add-ons > Program add-ons.")
    else:
        print(f"OK -- {ADDON_ID} extracted. If the addon is hidden or zip-install is "
              "blocked, set these once in the GUI: Settings > System > Add-ons > "
              "'Unknown sources' = On, 'Update official add-ons from' = Any repositories; "
              "and Add-ons > My add-ons > Program add-ons > CoreELEC Toolbox > Enable.")


if __name__ == "__main__":
    main()
