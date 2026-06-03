#!/usr/bin/env python3
"""
Run this with the WORKING CoreELEC unit booted + reachable over SSH.
Gathers, in one shot, everything Part B needs from the known-good unit:

  1. /etc/fw_env.config        -> the TRUE u-boot env location (device/offset/size).
                                  We discovered p2 is NOT the live env, so this is
                                  the single most important unknown to resolve.
  2. fw_printenv (full)        -> the live env WITH the working boot_ce gate.
  3. raw dump of the env area  -> from the device/offset in fw_env.config (env_live.bin),
                                  so env.bin can be built/dd'd to the right place.
  4. /flash payload            -> kernel.img, SYSTEM(+md5), dtb.img, cfgload,
                                  config.ini, recovery.img, device_trees/, dovi.ko,
                                  user-update.sh  (baked into ce_flash.img later).
  5. raw boot_a(p21)+dtbo_a(p12) -> what u-boot actually loads (authoritative
                                  boota/dtboa sources; compared against /flash).
  6. sha256 of everything.

Usage:  python pull_coreelec_payload.py [HOST]   (default 192.168.1.196)
Saves into  partB-installer/payload/
"""
import os, sys, hashlib, posixpath, stat
import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.196"
USER, PASS = "root", "coreelec"
DEST = os.path.join(os.path.dirname(__file__), "..", "payload")
os.makedirs(DEST, exist_ok=True)

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PASS, timeout=20,
            look_for_keys=False, allow_agent=False)


def run(cmd, timeout=120):
    i, o, e = cli.exec_command(cmd, timeout=timeout)
    return o.read().decode("replace"), e.read().decode("replace"), o.channel.recv_exit_status()


def save_text(name, txt):
    p = os.path.join(DEST, name)
    open(p, "w", encoding="utf-8", newline="\n").write(txt)
    print(f"  saved {name} ({len(txt)} B)")


# ---- 1. fw_env.config (the key unknown) -------------------------------------
print("== 1. /etc/fw_env.config ==")
out, err, _ = run("cat /etc/fw_env.config 2>/dev/null; echo '---'; "
                  "cat /etc/fw_env.config 2>/dev/null | grep -v '^#' | grep -v '^[[:space:]]*$'")
print(out)
save_text("fw_env.config.txt", out)

# ---- 2. live env (with gate) ------------------------------------------------
print("== 2. fw_printenv (live) ==")
out, err, _ = run("fw_printenv 2>/dev/null | sort")
save_text("fw_printenv_live.txt", out)
for k in ("bootcmd", "bootcefromemmc", "boot_ce", "cfgloademmc",
          "bootfromusb", "active_slot", "boot_part"):
    for line in out.splitlines():
        if line.startswith(k + "="):
            print("   " + line[:160])

# ---- 3. raw dump of the true env area ---------------------------------------
print("== 3. raw env dump (per fw_env.config) ==")
# parse first non-comment data line: <device> <offset> <size> [esize] [#sectors]
dev = off = size = None
for line in open(os.path.join(DEST, "fw_env.config.txt")).read().splitlines():
    s = line.strip()
    if not s or s.startswith("#") or s == "---":
        continue
    parts = s.split()
    if len(parts) >= 3 and parts[0].startswith("/dev"):
        dev = parts[0]; off = parts[1]; size = parts[2]; break
if dev:
    print(f"   env at dev={dev} off={off} size={size}")
    # dump exactly <size> bytes from <offset>
    cmd = (f"off=$(({off})); sz=$(({size})); "
           f"dd if={dev} bs=1 skip=$off count=$sz 2>/dev/null | base64")
    out, err, rc = run(cmd, timeout=180)
    raw = __import__("base64").b64decode(out)
    open(os.path.join(DEST, "env_live.bin"), "wb").write(raw)
    print(f"   saved env_live.bin ({len(raw)} B)  sha256={hashlib.sha256(raw).hexdigest()[:16]}")
else:
    print("   !! could not parse fw_env.config -- inspect fw_env.config.txt manually")

# ---- 4. /flash payload ------------------------------------------------------
print("== 4. /flash payload ==")
sftp = cli.open_sftp()
flashdir = os.path.join(DEST, "flash"); os.makedirs(flashdir, exist_ok=True)


def pull_tree(remote, local):
    for entry in sftp.listdir_attr(remote):
        rp = posixpath.join(remote, entry.filename)
        lp = os.path.join(local, entry.filename)
        if stat.S_ISDIR(entry.st_mode):
            os.makedirs(lp, exist_ok=True); pull_tree(rp, lp)
        else:
            sftp.get(rp, lp)
            print(f"   {rp}  ({entry.st_size} B)")


pull_tree("/flash", flashdir)

# ---- 5. raw boot_a + dtbo_a -------------------------------------------------
print("== 5. boot_a(p21) + dtbo_a(p12) raw ==")
for name, devpath in (("boot_a_live.bin", "/dev/mmcblk0p21"),
                      ("dtbo_a_live.bin", "/dev/mmcblk0p12")):
    out, err, rc = run(f"dd if={devpath} bs=1M 2>/dev/null | base64", timeout=240)
    raw = __import__("base64").b64decode(out)
    open(os.path.join(DEST, name), "wb").write(raw)
    print(f"   {name} ({len(raw)} B) sha256={hashlib.sha256(raw).hexdigest()[:16]}")

# ---- 6. sha256 manifest -----------------------------------------------------
print("== 6. sha256 manifest ==")
lines = []
for root, _, files in os.walk(DEST):
    for f in sorted(files):
        if f == "SHA256SUMS.txt":
            continue
        fp = os.path.join(root, f)
        h = hashlib.sha256(open(fp, "rb").read()).hexdigest()
        rel = os.path.relpath(fp, DEST).replace("\\", "/")
        lines.append(f"{h}  {rel}")
open(os.path.join(DEST, "SHA256SUMS.txt"), "w", newline="\n").write("\n".join(lines) + "\n")
print("\n".join("   " + l for l in lines))
sftp.close(); cli.close()
print("\nDONE. Payload in partB-installer/payload/")
