#!/usr/bin/env python3
"""
Deploy the Xiaomi-remote button mapping to a RUNNING CoreELEC (over SSH).

Two files, both under /storage so they survive CoreELEC OS updates:
  /storage/.config/hwdb.d/99-xiaomi-remote.hwdb   evdev remap of the remote's
        special buttons (Netflix/Voice/PrimeVideo/OK) to keys Kodi can bind.
  /storage/.kodi/userdata/keymaps/xiaomi.xml      Kodi keymap binding the
        remapped colored keys to RunAddon(...) (PM4K / TinyPPI).

After copying it runs `systemd-hwdb update` + `udevadm trigger` (applies the
remap live, no reboot) and `kodi-send ReloadKeymaps` (applies the keymap live).

  python deploy_remote_keymap.py --host 192.168.1.195 [--pass coreelec]

Idempotent; re-run any time. Needs paramiko (pip install paramiko).
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE = os.path.join(HERE, "..", "payload", "remote")
HWDB_LOCAL = os.path.join(REMOTE, "99-xiaomi-remote.hwdb")
KEYMAP_LOCAL = os.path.join(REMOTE, "xiaomi.xml")

HWDB_DEST = "/storage/.config/hwdb.d/99-xiaomi-remote.hwdb"
KEYMAP_DEST = "/storage/.kodi/userdata/keymaps/xiaomi.xml"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.195", help="CoreELEC IP/hostname")
    ap.add_argument("--user", default="root")
    ap.add_argument("--pass", dest="pw", default="coreelec")
    ap.add_argument("--auto", action="store_true",
                    help="skip (exit 0) unless the Xiaomi remote (uhid 0005:2717:32B9) is present")
    a = ap.parse_args()

    try:
        import paramiko
    except ImportError:
        sys.exit("paramiko not installed -- pip install paramiko")
    for f in (HWDB_LOCAL, KEYMAP_LOCAL):
        if not os.path.exists(f):
            sys.exit(f"missing payload file: {f}")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(a.host, username=a.user, password=a.pw, timeout=15,
                look_for_keys=False, allow_agent=False)

    def sh(cmd):
        _, o, e = cli.exec_command(cmd, timeout=60)
        out = o.read().decode(errors="replace")
        rc = o.channel.recv_exit_status()
        return rc, out, e.read().decode(errors="replace")

    if a.auto:
        _, det, _ = sh("ls -d /sys/devices/virtual/misc/uhid/0005:2717:32B9* 2>/dev/null; "
                       "grep -l 'Product=32b9' /proc/bus/input/devices 2>/dev/null")
        if "32b9" not in det.lower():
            cli.close()
            print("Xiaomi remote (uhid 0005:2717:32B9) not detected -- skipping remote keymap.")
            print("If this IS a Xiaomi unit (remote may be asleep/unpaired), force with --xiaomi.")
            return

    sh("mkdir -p /storage/.config/hwdb.d /storage/.kodi/userdata/keymaps")
    sftp = cli.open_sftp()
    sftp.put(HWDB_LOCAL, HWDB_DEST)
    sftp.put(KEYMAP_LOCAL, KEYMAP_DEST)
    sftp.close()
    print(f"  -> {HWDB_DEST}")
    print(f"  -> {KEYMAP_DEST}")

    rc, out, err = sh("systemd-hwdb update && udevadm trigger --action=add "
                      "--subsystem-match=input && udevadm settle && echo HWDB_OK")
    if "HWDB_OK" not in out:
        cli.close(); sys.exit(f"hwdb activation failed: {out}{err}")
    print("  hwdb updated + input re-triggered")

    rc, out, _ = sh("kodi-send -a ReloadKeymaps 2>/dev/null && echo KM_OK || echo KM_SKIP")
    print("  keymap reloaded" if "KM_OK" in out else "  keymap will load on next Kodi start")

    # verify the remap took (EVIOCGKEYCODE readback, no button press)
    vfy = (r"python3 - <<'PY'" "\n"
           r"import os,glob,fcntl,struct" "\n"
           r"W={0xc0041:28,0xc008e:398,0xc00cf:399,0xc00b0:400}" "\n"
           r"ok=0" "\n"
           r"for d in glob.glob('/dev/input/event*'):" "\n"
           r" try: fd=os.open(d,os.O_RDONLY)" "\n"
           r" except: continue" "\n"
           r" for sc,want in W.items():" "\n"
           r"  try:" "\n"
           r"   r=fcntl.ioctl(fd,0x80084504,struct.pack('II',sc,0))" "\n"
           r"   if struct.unpack('II',r)[1]==want: ok+=1" "\n"
           r"  except Exception: pass" "\n"
           r" os.close(fd)" "\n"
           r"print('REMAP_HITS',ok)" "\n"
           r"PY")
    _, out, _ = sh(vfy)
    print("  " + out.strip())
    cli.close()
    print("OK -- Xiaomi remote mapping deployed (Netflix->PM4K, Voice/Prime->TinyPPI, OK->Select).")


if __name__ == "__main__":
    main()
