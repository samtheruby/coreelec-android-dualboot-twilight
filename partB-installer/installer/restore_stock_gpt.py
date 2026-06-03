#!/usr/bin/env python3
"""
Restore the STOCK 32-entry GPT to a twilight unit whose carve region was already
modified (old manual layout, or a previous install). This reverts userdata to its
full span and removes the CE_FLASH / CE_STORAGE entries, so flash_to_coreelec.py's
preflight passes and a clean canonical 2676/850/650 install can be (re)applied.

Only the GPT is touched (primary @ LBA 0..33 and backup @ 15265792..). No file
system or partition data is moved; the GPT region is not mounted, so writing it
while userdata is mounted is safe (no overlap / no read-during-overwrite). The
running kernel keeps the old table cached until you REBOOT -- that's expected.

Source of truth, in priority order:
  1. pulled_backups/gpt_primary_pre.bin (34 sectors) + gpt_backup_pre.bin (2 MiB) --
     THIS unit's own pre-install GPT, saved by stage0. Use this to reverse a clean
     install on the same device.
  2. device_backups/disk_first2M.bin + disk_last2M.bin (2 MiB each) -- the dev Phase-0
     reference dumps (fallback). Both are verified 32-entry with userdata at the full carve.

  python restore_stock_gpt.py --serial <ip:port>          # recon only (no writes)
  python restore_stock_gpt.py --serial <ip:port> --yes     # restore, then reboot manually
"""
import argparse, base64, os, struct, subprocess, sys, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PB = os.path.join(HERE, "..", "pulled_backups")            # this unit's stage0 dumps
BK = os.path.join(HERE, "..", "..", "device_backups")      # dev Phase-0 dumps (fallback)
DEST = os.path.join(HERE, "..", "pulled_backups_prerestore")
DISK = "/dev/block/mmcblk0"
TOTAL_SECTORS = 15_269_888
GPT_BACKUP_LBA = 15_265_792
STOCK_UD_FIRST_LBA = 6_713_344          # stock userdata start (== carve start)
STOCK_UD_LAST_LBA = 15_265_791
SB_WIPE_SECTORS = 8192                  # 4 MiB: clobber fs superblock(s) -> force reformat

# Prefer THIS unit's pre-install GPT (stage0) over the dev reference dumps.
if os.path.exists(os.path.join(PB, "gpt_primary_pre.bin")) and \
   os.path.exists(os.path.join(PB, "gpt_backup_pre.bin")):
    PRIMARY = os.path.join(PB, "gpt_primary_pre.bin")
    BACKUP = os.path.join(PB, "gpt_backup_pre.bin")
    SRC_TAG = "pulled_backups (this unit's pre-install GPT)"
else:
    PRIMARY = os.path.join(BK, "disk_first2M.bin")
    BACKUP = os.path.join(BK, "disk_last2M.bin")
    SRC_TAG = "device_backups (Phase-0 reference dumps)"


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


class Dev:
    def __init__(self, serial):
        self.serial = serial

    def su(self, cmd):
        r = subprocess.run(["adb", "-s", self.serial, "exec-out", f"su -c {shq(cmd)}"],
                           capture_output=True)
        return r.stdout.decode("utf-8", "replace"), r.returncode

    def su_bytes(self, cmd):
        return subprocess.run(["adb", "-s", self.serial, "exec-out", f"su -c {shq(cmd)}"],
                              capture_output=True).stdout

    def pull_raw(self, cmd):
        data = self.su_bytes(cmd + " 2>/dev/null | base64")
        return base64.b64decode(b"".join(bytes(data).split()))

    def getprop(self, p):
        return self.su(f"getprop {p}")[0].strip()

    def push(self, local, remote):
        r = subprocess.run(["adb", "-s", self.serial, "push", local, remote],
                           capture_output=True)
        if r.returncode != 0:
            sys.exit("push failed: " + r.stderr.decode("utf-8", "replace"))


def parse_gpt(buf):
    """buf starts at LBA0. Return (num_entries, [(name,first,last)...])."""
    assert buf[512:520] == b"EFI PART", "no EFI PART header"
    num = struct.unpack_from("<I", buf, 512 + 80)[0]
    pe = struct.unpack_from("<Q", buf, 512 + 72)[0]
    arr = buf[pe * 512:]
    parts = []
    for i in range(num):
        e = arr[i * 128:(i + 1) * 128]
        if len(e) < 128:
            break
        nm = e[56:128].decode("utf-16-le", "replace").split("\x00")[0]
        fl = struct.unpack_from("<Q", e, 32)[0]
        ll = struct.unpack_from("<Q", e, 40)[0]
        if nm:
            parts.append((nm, fl, ll))
    return num, parts


def hdr_crc_ok(buf, off):
    h = bytearray(buf[off:off + 92])
    stored = struct.unpack_from("<I", h, 16)[0]
    struct.pack_into("<I", h, 16, 0)
    return (zlib.crc32(h) & 0xffffffff) == stored


def verify_stock_files():
    for p in (PRIMARY, BACKUP):
        if not os.path.exists(p):
            sys.exit(f"missing stock backup: {p}")
    f = open(PRIMARY, "rb").read()
    b = open(BACKUP, "rb").read()
    # primary: a 34-sector GPT grab (pulled_backups) or a 2 MiB disk dump (device_backups)
    if len(f) not in (34 * 512, 2 * 1024 * 1024):
        sys.exit(f"primary source is {len(f)} B (want 17408 or 2 MiB) -- refusing")
    # backup: the full 2 MiB region (entry array + alt header at the last sector)
    if len(b) != 2 * 1024 * 1024:
        sys.exit(f"backup source is {len(b)} B (want 2 MiB) -- refusing")
    num, parts = parse_gpt(f)
    if num != 32:
        sys.exit(f"stock primary has {num} entries (expected 32) -- refusing")
    if not hdr_crc_ok(f, 512):
        sys.exit("stock primary header CRC bad -- refusing")
    ud = next((ll for nm, fl, ll in parts if nm == "userdata"), None)
    if ud != STOCK_UD_LAST_LBA:
        sys.exit(f"stock userdata last_lba={ud} != {STOCK_UD_LAST_LBA} -- refusing")
    if b.rfind(b"EFI PART") < 0:
        sys.exit("stock backup has no EFI PART header -- refusing")
    print(f"  stock source [{SRC_TAG}] OK: primary {len(f)} B (32 entries, userdata->{ud}), "
          f"backup {len(b)} B (alt header present)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--yes", action="store_true", help="perform the GPT writes")
    ap.add_argument("--no-sbwipe", action="store_true",
                    help="skip the userdata superblock wipe -- safe ONLY when userdata "
                         "already starts at the stock LBA with a valid fs (then Android "
                         "boots it as-is, no factory reset). If unsure, omit this.")
    a = ap.parse_args()
    import adb_serial
    a.serial = adb_serial.resolve(a.serial)
    d = Dev(a.serial)

    print(f"=== restore stock GPT (serial={a.serial} "
          f"mode={'WRITE' if a.yes else 'RECON'}) ===")

    # ---- stock source ----
    print("\n-- stock backup source --")
    verify_stock_files()

    # ---- device identity + size guard ----
    print("\n-- device --")
    dev = d.getprop("ro.product.device")
    if dev != "twilight":
        sys.exit(f"device='{dev}' != twilight -- WRONG MODEL. Abort.")
    if "uid=0" not in d.su("id")[0]:
        sys.exit("su root not available")
    sz = d.su("cat /sys/class/block/mmcblk0/size")[0].strip()
    if sz != str(TOTAL_SECTORS):
        sys.exit(f"mmcblk0 size={sz} != {TOTAL_SECTORS} -- wrong disk geometry. Abort.")
    print(f"  device=twilight root=ok mmcblk0={sz} sectors (match)")

    # ---- read current GPT ----
    print("\n-- current on-device GPT --")
    cur = d.pull_raw(f"dd if={DISK} bs=512 count=34")
    if cur[512:520] != b"EFI PART":
        sys.exit("could not read current GPT")
    num, parts = parse_gpt(cur)
    ce = [nm for nm, _, _ in parts if nm in ("CE_FLASH", "CE_STORAGE")]
    ud = next((ll for nm, _, ll in parts if nm == "userdata"), None)
    print(f"  entries={num}  CE partitions={ce or 'none'}  userdata last_lba={ud}")
    for nm, fl, ll in parts:
        if nm in ("userdata", "CE_FLASH", "CE_STORAGE"):
            print(f"    {nm:<12} {fl:>10}..{ll:<10}  ({(ll - fl + 1) // 2048} MiB)")

    is_stock = (num == 32 and not ce and ud == STOCK_UD_LAST_LBA)
    if is_stock:
        print("\nAlready stock -- nothing to restore. (Run flash_to_coreelec.py directly.)")
        return

    # ---- insurance: pull current (modified) GPT to PC before overwriting ----
    print("\n-- pre-restore backup -> pulled_backups_prerestore/ --")
    os.makedirs(DEST, exist_ok=True)
    open(os.path.join(DEST, "gpt_primary_modified.bin"), "wb").write(cur)
    bkp = d.pull_raw(f"dd if={DISK} bs=512 skip={GPT_BACKUP_LBA} count=34")
    open(os.path.join(DEST, "gpt_backup_modified.bin"), "wb").write(bkp)
    print(f"  saved gpt_primary_modified.bin ({len(cur)} B), "
          f"gpt_backup_modified.bin ({len(bkp)} B)")

    if not a.yes:
        print("\n-- write plan (RECON only; no writes performed) --")
        print(f"   push {os.path.basename(PRIMARY)} -> /data/local/tmp ; "
              f"dd of={DISK} bs=512 count=34 seek=0 conv=fsync     (primary GPT)")
        print(f"   push {os.path.basename(BACKUP)}  -> /data/local/tmp ; "
              f"dd of={DISK} bs=512 seek={GPT_BACKUP_LBA} conv=fsync   (backup GPT, 2 MiB)")
        print(f"   dd if=/dev/zero of={DISK} bs=512 seek={STOCK_UD_FIRST_LBA} "
              f"count={SB_WIPE_SECTORS} conv=fsync   (wipe userdata superblock -> clean reformat)")
        print("   then: sync ; adb reboot")
        print("\nRECON only. Re-run with --yes to write.")
        return

    # ---- WRITE stock GPT ----
    print("\n-- writing stock GPT --")
    d.push(PRIMARY, "/data/local/tmp/_restore_gpt_p.bin")
    d.push(BACKUP, "/data/local/tmp/_restore_gpt_b.bin")
    print("  pushed stock dumps to /data/local/tmp")

    wipe = "" if a.no_sbwipe else (
        f"dd if=/dev/zero of={DISK} bs=512 seek={STOCK_UD_FIRST_LBA} count={SB_WIPE_SECTORS} conv=fsync && ")
    print("  userdata SB wipe: " + ("SKIPPED (--no-sbwipe)" if a.no_sbwipe else "yes (forces reformat)"))
    out, rc = d.su(
        f"dd if=/data/local/tmp/_restore_gpt_p.bin of={DISK} bs=512 count=34 seek=0 conv=fsync && "
        f"dd if=/data/local/tmp/_restore_gpt_b.bin of={DISK} bs=512 seek={GPT_BACKUP_LBA} conv=fsync && "
        f"{wipe}"
        f"sync && "
        f"rm -f /data/local/tmp/_restore_gpt_p.bin /data/local/tmp/_restore_gpt_b.bin && echo WROTE")
    print("  " + out.strip().replace("\n", "\n  "))
    if rc != 0 or "WROTE" not in out:
        sys.exit("GPT write failed")

    # ---- verify the raw write landed (kernel still has old table until reboot) ----
    chk = d.pull_raw(f"dd if={DISK} bs=512 count=34")
    n2, p2 = parse_gpt(chk)
    ce2 = [nm for nm, _, _ in p2 if nm in ("CE_FLASH", "CE_STORAGE")]
    ud2 = next((ll for nm, _, ll in p2 if nm == "userdata"), None)
    print(f"\n-- verify (raw re-read of mmcblk0) --")
    print(f"  entries={n2}  CE partitions={ce2 or 'none'}  userdata last_lba={ud2}")
    if not (n2 == 32 and not ce2 and ud2 == STOCK_UD_LAST_LBA):
        sys.exit("verify FAILED -- on-disk GPT is not stock. DO NOT REBOOT; investigate.")
    print("  on-disk GPT is stock. Now reboot so the kernel re-reads it:")
    print(f"    adb -s {a.serial} reboot")
    print("  After reboot, confirm with: "
          f"python flash_to_coreelec.py --serial {a.serial} --dry-run")


if __name__ == "__main__":
    main()
