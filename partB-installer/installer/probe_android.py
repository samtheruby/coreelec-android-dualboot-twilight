#!/usr/bin/env python3
"""
Read-only Android recon for the Part B installer. Run with the stock/rooted
twilight unit booted into Android and adb reachable.

Answers the installer's open questions:
  - which tools exist on Android (fw_setenv/fw_printenv, mke2fs, mkfs.vfat,
    blockdev, sgdisk, parted, base64, sha256sum, toybox/busybox)
  - the /dev/block/by-name -> mmcblk0pN -> major:minor map (for mknod)
  - is /etc/fw_env.config present (does fw_setenv even know the env location)
  - the A/B bootloader_control bytes in misc @0x800 (+ crc method check)
  - /proc/partitions and userdata mount/encryption

Writes a report to partB-installer/payload/android_probe.txt
NOTHING is written to the device.

Usage: python probe_android.py [adb_serial]   (default = first network device)
"""
import subprocess, sys, os, zlib, struct

SERIAL = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.195:41243"
DEST = os.path.join(os.path.dirname(__file__), "..", "payload")
os.makedirs(DEST, exist_ok=True)


def sh(cmd, root=True):
    full = ["adb", "-s", SERIAL, "exec-out"]
    full.append(f"su -c {shq(cmd)}" if root else cmd)
    return subprocess.run(full, capture_output=True, text=True).stdout


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def shb(cmd):
    """run rooted, return raw bytes"""
    return subprocess.run(["adb", "-s", SERIAL, "exec-out", f"su -c {shq(cmd)}"],
                          capture_output=True).stdout


report = []
def out(s=""):
    print(s); report.append(s)


out("===== TOOL AVAILABILITY =====")
tools = ("fw_setenv fw_printenv mke2fs mkfs.ext4 mkfs.vfat mkfs.f2fs make_ext4fs "
         "tune2fs resize2fs e2fsck dd blockdev sgdisk parted blkid base64 "
         "sha256sum md5sum stat toybox busybox getprop magisk").split()
res = sh("for t in %s; do p=$(command -v $t 2>/dev/null); "
        "echo \"$t=${p:-MISSING}\"; done" % " ".join(tools))
out(res.strip())

out("\n===== /etc/fw_env.config on Android? =====")
out(sh("cat /etc/fw_env.config 2>/dev/null || echo 'NOT PRESENT'").strip())

out("\n===== by-name map (env/misc/boot/dtbo/super/userdata + all) =====")
out(sh(r"ls -l /dev/block/by-name/ 2>/dev/null | awk '{print $NF, $(NF-2)}'").strip())
out("\n--- resolve key parts to mmcblk0pN + major:minor ---")
out(sh(r'''for n in env misc boot_a boot_b dtbo_a dtbo_b userdata; do
  t=$(readlink -f /dev/block/by-name/$n 2>/dev/null);
  if [ -n "$t" ]; then mm=$(cat /sys/class/block/$(basename $t)/dev 2>/dev/null);
    echo "$n -> $t  ($mm)"; else echo "$n -> (none)"; fi; done''').strip())

out("\n===== /proc/partitions =====")
out(sh("cat /proc/partitions").strip())

out("\n===== GPT entry count (primary header @ LBA1 offset 0x50) =====")
out(sh(r"dd if=/dev/block/mmcblk0 bs=1 skip=80 count=4 2>/dev/null | od -An -tu4").strip())

out("\n===== misc A/B bootloader_control @0x800 (32 bytes) =====")
raw = shb("dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32 2>/dev/null")
if len(raw) == 32:
    out("hex: " + raw.hex())
    stored = struct.unpack_from("<I", raw, 28)[0]
    calc = zlib.crc32(raw[0:28]) & 0xffffffff
    out(f"slot_suffix={raw[0:4]!r} magic={raw[4:8]!r}")
    out(f"slot_a byte@12=0x{raw[12]:02x} (prio={raw[12]&0xf} tries={(raw[12]>>4)&7} ok={(raw[12]>>7)&1})")
    out(f"slot_b byte@14=0x{raw[14]:02x} (prio={raw[14]&0xf} tries={(raw[14]>>4)&7} ok={(raw[14]>>7)&1})")
    out(f"stored crc=0x{stored:08x} calc crc=0x{calc:08x} MATCH={stored==calc}")
else:
    out(f"unexpected misc read len={len(raw)}")

out("\n===== userdata mount / encryption =====")
out(sh("getprop ro.crypto.type; getprop ro.boot.slot_suffix; "
       "mount | grep -E '/data ' ; echo '---'; "
       "getprop ro.product.device; getprop ro.boot.verifiedbootstate").strip())

out("\n===== block write sanity (can root open mmcblk0 for write? dry, no write) =====")
out(sh("if dd if=/dev/block/mmcblk0 of=/dev/null bs=512 count=1 2>/dev/null; "
       "then echo 'root can READ raw mmcblk0'; else echo 'cannot read'; fi").strip())

out("\n===== OTA package present =====")
out(sh("pm list packages 2>/dev/null | grep -iE 'update|ota' | head").strip())

path = os.path.join(DEST, "android_probe.txt")
open(path, "w", encoding="utf-8", newline="\n").write("\n".join(report) + "\n")
print(f"\nsaved {path}")
