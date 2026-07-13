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

  python deploy_remote_keymap.py --host <coreelec-ip> [--pass coreelec]

Idempotent; re-run any time. Needs paramiko (pip install paramiko).
"""
import argparse, hashlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import bundle  # noqa: E402  -- SHA256SUMS.txt check on the files we upload

REMOTE = os.path.join(HERE, "..", "payload", "remote")
HWDB_LOCAL = os.path.join(REMOTE, "99-xiaomi-remote.hwdb")
KEYMAP_LOCAL = os.path.join(REMOTE, "xiaomi.xml")

HWDB_DEST = "/storage/.config/hwdb.d/99-xiaomi-remote.hwdb"
KEYMAP_DEST = "/storage/.kodi/userdata/keymaps/xiaomi.xml"
REMAP_SCANCODES = 4        # the four buttons the hwdb remaps (Netflix/Voice/Prime/OK)


def remap_hits(out):
    """How many of the remapped scancodes the kernel confirms, from the readback's
    'REMAP_HITS <n>' line. 0 when the line is absent or unparseable -- i.e. no evidence
    the remap took, which is treated the same as "it didn't"."""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "REMAP_HITS" and parts[1].isdigit():
            return int(parts[1])
    return 0


def main():
    ap = argparse.ArgumentParser()
    # No default: this SSHes in as root and writes /storage. A default IP means a bare run
    # reaches out to whatever machine happens to hold it on the user's LAN.
    ap.add_argument("--host", required=True, help="CoreELEC IP/hostname")
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

    # Probe for the remote ALWAYS, not just under --auto: whether it is connected decides
    # how to read the verification at the end (see below), not just whether to run at all.
    _, det, _ = sh("ls -d /sys/devices/virtual/misc/uhid/0005:2717:32B9* 2>/dev/null; "
                   "grep -l 'Product=32b9' /proc/bus/input/devices 2>/dev/null")
    remote_present = "32b9" in det.lower()

    if a.auto and not remote_present:
        cli.close()
        print("Xiaomi remote (uhid 0005:2717:32B9) not detected -- skipping remote keymap.")
        print("If this IS a Xiaomi unit (remote may be asleep/unpaired), force with --xiaomi.")
        return

    bundle.verify([HWDB_LOCAL, KEYMAP_LOCAL])   # vs SHA256SUMS.txt, when the bundle ships one

    sh("mkdir -p /storage/.config/hwdb.d /storage/.kodi/userdata/keymaps")
    sftp = cli.open_sftp()
    sftp.put(HWDB_LOCAL, HWDB_DEST)
    sftp.put(KEYMAP_LOCAL, KEYMAP_DEST)
    sftp.close()

    # Hash both files as they landed. Not for transit corruption (SSH authenticates every
    # packet, and paramiko's put(confirm=True) already size-checks): this catches the box
    # end -- a full /storage, or a write that did not commit. The hwdb file is then compiled
    # into a binary database by systemd-hwdb, which would happily accept a half-written one
    # and give the remote a remap nobody asked for.
    for local, dest in ((HWDB_LOCAL, HWDB_DEST), (KEYMAP_LOCAL, KEYMAP_DEST)):
        want = hashlib.sha256(open(local, "rb").read()).hexdigest()
        _, out, _ = sh(f"sha256sum {dest}")
        got = out.split()
        if not got or got[0].lower() != want:
            cli.close()
            sys.exit(f"{dest} did not survive the upload:\n  PC:  {want}\n"
                     f"  box: {got[0] if got else '(no output)'}")
        print(f"  -> {dest}  sha256 OK ({want[:16]})")

    _, out, err = sh("systemd-hwdb update && udevadm trigger --action=add "
                     "--subsystem-match=input && udevadm settle && echo HWDB_OK")
    if "HWDB_OK" not in out:
        cli.close(); sys.exit(f"hwdb activation failed: {out}{err}")
    print("  hwdb updated + input re-triggered")

    _, out, _ = sh("kodi-send -a ReloadKeymaps 2>/dev/null && echo KM_OK || echo KM_SKIP")
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
    cli.close()

    # The readback above asks the kernel what each scancode NOW maps to (EVIOCGKEYCODE), so
    # it proves the remap is live without anyone pressing a button. It used to be printed
    # and then ignored -- the script announced success even on REMAP_HITS 0.
    #
    # But zero hits only MEANS something when the remote is actually connected. The hwdb
    # rule matches on the device (evdev:input:b0005v2717p32B9*), so udev applies it the
    # moment the remote appears -- on a box whose remote is asleep or paired to Android
    # there is simply no input device to read a keycode back from, and the deployment is
    # perfectly correct. Measured on the dev stick: healthy box, remote away, REMAP_HITS 0.
    # Failing there would be a false alarm, so the verdict depends on `remote_present`.
    hits = remap_hits(out)
    if hits:
        print(f"  remap verified live: {hits} of {REMAP_SCANCODES} scancodes report their "
              f"new keycode")
    elif not remote_present:
        print("  the Xiaomi remote is not connected, so there is no input device to verify "
              "against.")
        print("  The hwdb rule is in place; udev applies it as soon as the remote connects.")
    else:
        sys.exit("the remap did NOT take: the Xiaomi remote IS connected, but no input "
                 "device reports any of the four remapped scancodes (REMAP_HITS 0).\n"
                 "The files are on the box, but the buttons will behave as before. Try "
                 "rebooting the box so the hwdb is applied at boot.")
    print("OK -- Xiaomi remote mapping deployed (Netflix->PM4K, Voice/Prime->TinyPPI, OK->Select).")


if __name__ == "__main__":
    main()
