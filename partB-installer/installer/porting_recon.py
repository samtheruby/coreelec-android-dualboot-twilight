#!/usr/bin/env python3
"""
Porting recon for the CoreELEC internal dual-boot — feasibility data collector.

READ-ONLY. Nothing is written to the device. It only reads props, the GPT, the
u-boot env, misc, and the head of reserved, then pulls small backup blobs to the
PC. Root (`su`) is required for the raw block reads (get root first: unlock +
Magisk-patched init_boot — that is the ONLY destructive prereq).

Run on the target booted into Android with adb reachable:

    python porting_recon.py --serial <serial>

Produces (in ./recon_out/):
    recon_report.txt   human-readable findings
    recon_data.json    machine-readable facts (feeds a future layout.py)
    env.bin misc.bin reserved_head.bin gpt_primary.bin gpt_backup.bin
    part_geometry.json start/size (sectors) of every by-name partition

Send the whole recon_out/ folder back. It answers the 4 porting questions
(research.md §3.1): u-boot GPT-scan vs injection, A/B?, dtb/SoC, env format.
"""
import argparse, base64, json, os, struct, subprocess, sys, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adb_serial import resolve

OUT = os.path.join(os.getcwd(), "recon_out")
ENV_SIZE = 0x10000              # twilight env area; we verify this holds here


# ---- adb plumbing -----------------------------------------------------------
def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def sh(serial, cmd, root=True):
    """Rooted shell command -> stdout text."""
    inner = f"su -c {shq(cmd)}" if root else cmd
    r = subprocess.run(["adb", "-s", serial, "exec-out", inner],
                       capture_output=True, text=True)
    return r.stdout


def shb(serial, cmd):
    """Rooted command, binary-safe: device base64-encodes, we decode. exec-out does
    not mangle, and base64 survives any pty quirks."""
    r = subprocess.run(["adb", "-s", serial, "exec-out", f"su -c {shq(cmd)}"],
                       capture_output=True)
    txt = r.stdout
    try:
        return base64.b64decode(txt)
    except Exception:
        return b""


def dd_region(serial, byname_or_dev, count_bytes=None, skip_bytes=0):
    """Read a block region as raw bytes via dd|base64 (root)."""
    src = byname_or_dev if byname_or_dev.startswith("/dev/") \
        else f"/dev/block/by-name/{byname_or_dev}"
    parts = [f"dd if={src} bs=1M"]
    if skip_bytes:
        parts.append(f"skip={skip_bytes} iflag=skip_bytes")
    if count_bytes:
        parts.append(f"count={count_bytes} iflag=count_bytes")
    cmd = " ".join(parts) + " 2>/dev/null | base64"
    return shb(serial, cmd)


# ---- report accumulators ----------------------------------------------------
report = []
facts = {}


def out(s=""):
    print(s)
    report.append(s)


def section(title):
    out("\n===== " + title + " =====")


# ---- 1. identity / root -----------------------------------------------------
def recon_identity(serial):
    section("IDENTITY / ROOT / OTA")
    props = ["ro.product.device", "ro.product.model", "ro.board.platform",
             "ro.build.version.release", "ro.boot.slot_suffix", "ro.crypto.type",
             "ro.boot.verifiedbootstate", "ro.product.ab_ota_partitions"]
    got = {}
    for p in props:
        v = sh(serial, f"getprop {p}").strip()
        got[p] = v
        out(f"  {p} = {v}")
    uid = sh(serial, "id").strip()
    out(f"  su -c id = {uid}")
    got["root"] = "uid=0" in uid
    slot = got.get("ro.boot.slot_suffix", "")
    got["is_ab"] = bool(slot)
    out(f"  --> A/B device: {got['is_ab']} (slot_suffix='{slot}')")
    out(f"  --> unlocked:   {got.get('ro.boot.verifiedbootstate') == 'orange'}")
    facts["identity"] = got
    if not got["root"]:
        out("\n  !! NO ROOT. Raw block reads below will be empty. Get root first "
            "(unlock + Magisk init_boot), then re-run.")
    return got


# ---- 2. partition table / by-name geometry ----------------------------------
def recon_partitions(serial):
    section("BY-NAME MAP + /proc/partitions")
    out(sh(serial, r'ls -l /dev/block/by-name/ 2>/dev/null | '
                   r'''awk '{print $NF, $(NF-2)}' ''').strip())
    out("\n--- /proc/partitions ---")
    out(sh(serial, "cat /proc/partitions").strip())

    section("PARTITION GEOMETRY (start/size sectors)")
    names = sh(serial, "ls /dev/block/by-name/ 2>/dev/null").split()
    geom = {}
    for n in names:
        node = sh(serial, f"readlink -f /dev/block/by-name/{n} 2>/dev/null").strip()
        if not node:
            continue
        base = os.path.basename(node)
        st = sh(serial, f"cat /sys/class/block/{base}/start 2>/dev/null").strip()
        sz = sh(serial, f"cat /sys/class/block/{base}/size 2>/dev/null").strip()
        if st and sz:
            geom[n] = {"node": node, "start": int(st), "size": int(sz),
                       "mib": int(sz) // 2048}
    for n in ("reserved", "env", "misc", "super", "boot_a", "boot_b",
              "dtbo_a", "dtbo_b", "userdata"):
        if n in geom:
            g = geom[n]
            out(f"  {n:<10} start={g['start']:>10} size={g['size']:>10} "
                f"({g['mib']} MiB)  {g['node']}")
    total = sh(serial, "cat /sys/class/block/mmcblk0/size 2>/dev/null").strip()
    if total:
        out(f"  mmcblk0    total={total} sectors ({int(total)//2048} MiB / "
            f"{int(total)/2097152:.2f} GiB)")
        facts["total_sectors"] = int(total)
    # carve region = userdata span
    if "userdata" in geom:
        u = geom["userdata"]
        out(f"\n  --> CARVE REGION (userdata span): start_mib={u['start']//2048} "
            f"size_mib={u['mib']}  (this replaces layout.py CARVE_*)")
        facts["carve"] = {"start_mib": u["start"] // 2048, "size_mib": u["mib"],
                          "start_lba": u["start"], "count": u["size"]}
    facts["geometry"] = geom
    json.dump(geom, open(os.path.join(OUT, "part_geometry.json"), "w"), indent=2)
    return geom


# ---- 3. GPT header: entry count + 32->128 expandability ----------------------
def recon_gpt(serial):
    section("GPT HEADER (32->128 expansion feasibility)")
    # primary header lives at LBA1
    hdr = dd_region(serial, "/dev/block/mmcblk0", count_bytes=512, skip_bytes=512)
    if len(hdr) < 512 or hdr[0:8] != b"EFI PART":
        out(f"  GPT header not found / not readable (len={len(hdr)}). Need root.")
        return
    first_usable = struct.unpack_from("<Q", hdr, 0x28)[0]
    last_usable = struct.unpack_from("<Q", hdr, 0x30)[0]
    entries_lba = struct.unpack_from("<Q", hdr, 0x48)[0]
    num_entries = struct.unpack_from("<I", hdr, 0x50)[0]
    entry_size = struct.unpack_from("<I", hdr, 0x54)[0]
    # room reserved for the entry array = sectors between entries_lba and first_usable
    array_sectors = first_usable - entries_lba
    array_bytes = array_sectors * 512
    max_entries = array_bytes // entry_size if entry_size else 0
    out(f"  num_partition_entries = {num_entries}")
    out(f"  size_of_partition_entry = {entry_size}")
    out(f"  entries_lba={entries_lba} first_usable_lba={first_usable} "
        f"last_usable_lba={last_usable}")
    out(f"  reserved entry-array room = {array_bytes} B = {max_entries} entries")
    expandable = max_entries >= 128
    out(f"  --> declares {num_entries}, room for {max_entries} "
        f"-> non-destructive expand to 128: {expandable}")
    facts["gpt"] = {"num_entries": num_entries, "entry_size": entry_size,
                    "first_usable_lba": first_usable, "last_usable_lba": last_usable,
                    "entries_lba": entries_lba, "max_entries": max_entries,
                    "expandable_to_128": expandable}
    # pull primary (first 34 sectors) + backup (last 33 sectors) for baseline
    prim = dd_region(serial, "/dev/block/mmcblk0", count_bytes=34 * 512)
    save("gpt_primary.bin", prim)
    if facts.get("total_sectors"):
        tail_skip = (facts["total_sectors"] - 33) * 512
        back = dd_region(serial, "/dev/block/mmcblk0", count_bytes=33 * 512,
                         skip_bytes=tail_skip)
        save("gpt_backup.bin", back)


# ---- 4. u-boot env: format + cfgload-vs-injection signal ---------------------
def recon_env(serial):
    section("U-BOOT ENV (format + GPT-scan-vs-injection)")
    # pull generously (0x20000) so we detect a redundant/larger env too
    raw = dd_region(serial, "env", count_bytes=0x20000)
    if not raw:
        out("  env unreadable (need root).")
        return
    save("env.bin", raw)
    out(f"  pulled env bytes = {len(raw)}")

    # try the twilight format: [crc32 LE][key=val\0...][pad], crc over [4:ENV_SIZE]
    def try_crc(size, crc_at=0):
        if len(raw) < size:
            return None
        stored = struct.unpack_from("<I", raw, crc_at)[0]
        calc = zlib.crc32(raw[crc_at + 4:size]) & 0xffffffff
        return stored == calc, stored, calc

    fmt = None
    for size in (0x10000, 0x20000):
        r = try_crc(size)
        if r and r[0]:
            fmt = ("non-redundant", size, 0)
            out(f"  CRC MATCH as NON-redundant, size={hex(size)} "
                f"(stored={r[1]:#010x})")
            break
    if not fmt:
        # redundant env: [crc32][flag byte][body]; crc over body
        for size in (0x10000, 0x20000):
            if len(raw) < size:
                continue
            stored = struct.unpack_from("<I", raw, 0)[0]
            calc = zlib.crc32(raw[5:size]) & 0xffffffff
            if stored == calc:
                fmt = ("redundant", size, 5)
                out(f"  CRC MATCH as REDUNDANT (flag byte), size={hex(size)}")
                break
    if not fmt:
        out("  !! CRC did not match twilight format at 0x10000/0x20000, "
            "redundant or not. Env format DIFFERS -> envtool.py must be adapted.")
        fmt = ("unknown", 0, 0)
    facts["env_format"] = {"kind": fmt[0], "size": fmt[1], "body_off": fmt[2]}

    # parse key=val pairs (best-effort, from body start)
    body_off = fmt[2] + 4 if fmt[2] else 4
    body = raw[body_off:fmt[1] or ENV_SIZE]
    kv = {}
    for chunk in body.split(b"\x00"):
        if not chunk:
            break
        k, _, v = chunk.partition(b"=")
        try:
            kv[k.decode("latin1")] = v.decode("latin1")
        except Exception:
            pass
    out(f"  parsed {len(kv)} env vars")
    keys_of_interest = ["bootcmd", "storeboot", "cfgloademmc", "bootfromemmc",
                        "cfgloadsd", "bootfromnand", "recovery_from_flash",
                        "loadaddr", "dtb_mem_addr", "loadaddr_kernel"]
    for k in keys_of_interest:
        if k in kv:
            out(f"    {k} = {kv[k][:300]}")
    has_cfgloademmc = "cfgloademmc" in kv
    out(f"\n  --> cfgloademmc present: {has_cfgloademmc}")
    out("      present + scans eMMC FAT -> possibly the simpler CFGLOAD method")
    out("      absent/non-scanning     -> NAMED-PARTITION INJECTION method (twilight)")
    facts["env_vars_of_interest"] = {k: kv.get(k) for k in keys_of_interest}
    facts["has_cfgloademmc"] = has_cfgloademmc


# ---- 5. Amlogic MPT probe (logo-hang trap) ----------------------------------
def recon_mpt(serial):
    section("AMLOGIC MPT (reserved head)")
    head = dd_region(serial, "reserved", count_bytes=0x5000)
    if not head:
        out("  reserved unreadable (need root).")
        return
    save("reserved_head.bin", head)
    magic = head[0:4]
    present = magic == b"MPT\x00"
    out(f"  reserved[0:4] = {magic!r}  MPT present: {present}")
    if present:
        part_num = struct.unpack_from("<I", head, 0x10)[0]
        out(f"  MPT part_num = {part_num} (installer wipe_mpt must run; confirm "
            f"offset 0x2400000 matches)")
    else:
        aml = head[0x4000:0x4009]
        out(f"  reserved[0x4000:] = {aml!r} (AMLNORMAL expected; no MPT -> GPT scan works)")
    facts["mpt_present"] = present


# ---- 6. misc A/B struct + tools --------------------------------------------
def recon_misc_tools(serial):
    section("MISC A/B bootloader_control @0x800")
    raw = dd_region(serial, "misc", count_bytes=0x1000)
    if raw and len(raw) >= 0x820:
        save("misc.bin", raw)
        blk = raw[0x800:0x820]
        stored = struct.unpack_from("<I", blk, 28)[0]
        calc = zlib.crc32(blk[0:28]) & 0xffffffff
        out(f"  hex@0x800 = {blk.hex()}")
        out(f"  slot_suffix={blk[0:4]!r} magic={blk[4:8]!r}")
        out(f"  slot_a byte@12=0x{blk[12]:02x} slot_b byte@14=0x{blk[14]:02x}")
        out(f"  stored crc=0x{stored:08x} calc=0x{calc:08x} MATCH={stored==calc}")
        facts["misc_bcab"] = {"magic": blk[4:8].decode("latin1", "replace"),
                              "crc_match": stored == calc}
    else:
        out(f"  misc read too short (len={len(raw)}); need root.")

    section("ON-DEVICE TOOLS")
    tools = ("fw_setenv fw_printenv mkfs.vfat mke2fs mkfs.ext4 mkfs.f2fs sgdisk "
             "parted blockdev blkid base64 sha256sum toybox busybox dd nc "
             "resize2fs e2fsck").split()
    res = sh(serial, "for t in %s; do p=$(command -v $t 2>/dev/null); "
             "echo \"$t=${p:-MISSING}\"; done" % " ".join(tools))
    out(res.strip())
    facts["tools"] = dict(
        ln.split("=", 1) for ln in res.strip().splitlines() if "=" in ln)


# ---- save helper ------------------------------------------------------------
def save(name, data):
    if not data:
        out(f"  (skip {name}: no data)")
        return
    p = os.path.join(OUT, name)
    open(p, "wb").write(data)
    out(f"  saved {name} ({len(data)} bytes)")


# ---- main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="CoreELEC dual-boot porting recon (read-only)")
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick")
    a = ap.parse_args()
    serial = resolve(a.serial)
    os.makedirs(OUT, exist_ok=True)
    out(f"# porting recon  serial={serial}")

    ident = recon_identity(serial)
    recon_partitions(serial)
    if ident.get("root"):
        recon_gpt(serial)
        recon_env(serial)
        recon_mpt(serial)
        recon_misc_tools(serial)
    else:
        out("\n(skipping root-only reads — no uid=0)")

    # verdict hints (data only; humans decide)
    section("VERDICT HINTS")
    out(f"  A/B device .............. {facts.get('identity', {}).get('is_ab')}")
    out(f"  GPT expandable to 128 ... {facts.get('gpt', {}).get('expandable_to_128')}")
    out(f"  cfgloademmc present ..... {facts.get('has_cfgloademmc')}")
    out(f"  env format .............. {facts.get('env_format', {}).get('kind')}")
    out(f"  MPT present ............. {facts.get('mpt_present')}")
    out(f"  carve (userdata) MiB .... {facts.get('carve', {}).get('size_mib')}")

    rep_path = os.path.join(OUT, "recon_report.txt")
    open(rep_path, "w", encoding="utf-8", newline="\n").write("\n".join(report) + "\n")
    json.dump(facts, open(os.path.join(OUT, "recon_data.json"), "w"), indent=2)
    print(f"\nsaved {rep_path}")
    print(f"saved {os.path.join(OUT, 'recon_data.json')}")
    print(f"\n>> send the whole {OUT}/ folder back <<")


if __name__ == "__main__":
    main()
