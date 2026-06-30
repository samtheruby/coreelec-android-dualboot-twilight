#!/usr/bin/env python3
"""
PC-side installer driver for the CoreELEC internal dual-boot (twilight only).

Writes are streamed from the PC into device `dd`s over a TCP tunnel:
    adb forward tcp:PORT -> device `busybox nc -l -p PORT | [gzip -dc |] dd of=... seek=...`
The PC connects to the tunnel and sends each artifact. This is the forensics-
standard no-staging method: the data never touches the userdata partition we
overwrite (no read-during-overwrite race), nothing is buffered in device RAM
(it streams), and the TCP socket gives a clean EOF (unlike `adb exec-out` stdin,
which is not forwarded, and `adb shell`, which mangles binary via a pty).

Reads (preflight, backups, verify) use `adb exec-out dd | base64` (device->PC).
Backups are pulled to the PC BEFORE any write, so the install can't destroy them.

Flow: preflight -> per-unit env/misc blobs -> PC-side backups -> streamed writes
(GPT first, env last, each verified) -> disable OTA.

  python flash_to_coreelec.py --serial <ip:port> --dry-run
  python flash_to_coreelec.py --serial <ip:port> --yes
"""
import argparse, base64, gzip, hashlib, os, socket, subprocess, sys, struct, time

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "..", "artifacts")
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool, build_env, ab_misc, layout as L  # noqa: E402

DISK = "/dev/block/mmcblk0"
GPT_BACKUP_LBA = 15_265_792
STOCK_NUM_ENTRIES = 32
STOCK_UD_LAST_LBA = 15_265_791
BIG = {"ce_flash.img", "ce_storage.img"}
NC = "/vendor/bin/busybox nc"
GUNZIP = "/vendor/bin/busybox gzip -dc"


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def secs():
    return {n: (a, b, c) for n, a, b, c in L.as_sectors()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (ip:port or USB id); omit to auto-pick the only device")
    ap.add_argument("--yes", action="store_true", help="perform real writes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--port", type=int, default=5599)
    ap.add_argument("--default", choices=["android", "coreelec"], default="android",
                    help="which OS a normal reboot boots (default: android; the app "
                         "enters CoreELEC). 'coreelec' = CE default, reboot-to-eMMC/nand -> Android.")
    args = ap.parse_args()
    import adb_serial
    args.serial = adb_serial.resolve(args.serial)
    dry = args.dry_run or not args.yes
    g = Ctx(args.serial, dry, args.port, args.default)

    print(f"=== CoreELEC dual-boot installer (serial={args.serial} "
          f"mode={'DRY-RUN' if dry else 'REAL WRITE'}) ===")
    require_artifacts()
    ce_slot = g.preflight()
    g.build_target_blobs(ce_slot)
    if dry:
        g.print_plan(ce_slot)
        print("\nDRY-RUN only. Re-run with --yes to install.")
        return
    g.backups_to_pc(ce_slot)
    g.reguard()
    try:
        g.write_all(ce_slot)
    finally:
        g.adb("forward", "--remove", f"tcp:{g.port}", capture_output=True)
    g.verify_writes(ce_slot)
    g.disable_ota()
    g.arm_factory_reset()
    print("\n=== install complete ===")
    print("  NEXT REBOOT -> recovery auto-reformats userdata to the new size (factory-reset-")
    print("  like), re-keys encryption, then boots Android. After that:")
    print("    normal reboot -> Android (default)")
    print("    'Reboot to CoreELEC' app (boot_ce=1) -> CoreELEC")


def require_artifacts():
    need = ["gpt_primary.bin", "gpt_backup.bin", "boota.img", "dtboa.img"]
    missing = [n for n in need if not os.path.exists(os.path.join(ART, n))]
    for n in BIG:
        if not (os.path.exists(os.path.join(ART, n)) or os.path.exists(os.path.join(ART, n + ".gz"))):
            missing.append(n + "[.gz]")
    if missing:
        sys.exit(f"missing artifacts: {missing} -- run build/build_all.py first")


class Ctx:
    def __init__(self, serial, dry, port, default="android"):
        self.serial = serial
        self.dry = dry
        self.port = port
        self.default = default  # "android" | "coreelec" -- which OS a normal reboot boots
        self.pipefail = False   # set in preflight if the device shell supports it

    # ---- adb (reads / commands; NOT writes) --------------------------------
    def adb(self, *a, **k):
        return subprocess.run(["adb", "-s", self.serial, *a], **k)

    def _exec_args(self, cmd):
        return ["adb", "-s", self.serial, "exec-out", f"su -c {shq(cmd)}"]

    def su(self, cmd):
        r = subprocess.run(self._exec_args(cmd), capture_output=True)
        return r.stdout.decode("utf-8", "replace"), r.returncode

    def su_bytes(self, cmd):
        return subprocess.run(self._exec_args(cmd), capture_output=True).stdout

    def getprop(self, p):
        return self.su(f"getprop {p}")[0].strip()

    # ---- streamed write over nc tunnel -------------------------------------
    def _fresh_port(self):
        """A new tcp port per transfer. busybox `nc -l` sets no SO_REUSEADDR, so
        re-listening on one port while its prior connection lingers in TIME_WAIT
        fails -- which broke sequential writes. A fresh port each call avoids it."""
        self._pseq = getattr(self, "_pseq", -1) + 1
        return self.port + self._pseq

    def nc_write(self, payload_path, devcmd, label, verify_timeout=900):
        """Stream payload_path from the PC into `nc -l | devcmd` on the device,
        over its own freshly-forwarded tcp port."""
        port = self._fresh_port()
        self.adb("forward", f"tcp:{port}", f"tcp:{port}", capture_output=True)
        try:
            # listener: [set -o pipefail;] nc -l -p PORT | <devcmd>. pipefail (when
            # supported) makes a gzip-decompress failure abort instead of being
            # masked by dd's rc=0.
            prefix = "set -o pipefail; " if self.pipefail else ""
            full = f"{prefix}{NC} -l -p {port} | {devcmd}"
            proc = subprocess.Popen(self._exec_args(full),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # wait until the device port is actually in LISTEN. `adb forward` accepts
            # at the PC end the instant we connect -- before nc has bound -- and any
            # bytes we send then are dropped by adbd's onward dial (dd then hangs on a
            # stream that never arrives). So gate on the device-side listen socket.
            hexp = format(port, "04X")
            for _ in range(100):
                if proc.poll() is not None:
                    break
                tcp, _ = self.su("cat /proc/net/tcp /proc/net/tcp6 2>/dev/null")
                if any(len(f) > 3 and f[1].upper().endswith(":" + hexp) and f[3] == "0A"
                       for f in (ln.split() for ln in tcp.splitlines())):
                    break
                time.sleep(0.2)
            # connect (retry until the listener is up)
            sock = None
            for _ in range(75):
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                    break
                except OSError:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.2)
            if sock is None:
                err = (proc.stderr.read().decode("utf-8", "replace").strip()
                       if proc.poll() is not None else "(listener up, no connect)")
                proc.kill()
                sys.exit(f"{label}: could not connect to nc tunnel on port {port} {err}")
            sent = 0
            with open(payload_path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent += len(chunk)
            sock.shutdown(socket.SHUT_WR)
            sock.close()
            try:
                rc = proc.wait(timeout=verify_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                sys.exit(f"{label}: device write timed out")
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            if rc != 0:
                sys.exit(f"{label}: device write failed (rc={rc}) {err}")
            print(f"  WROTE {label}  ({sent:,} B sent)")
        finally:
            self.adb("forward", "--remove", f"tcp:{port}", capture_output=True)

    def _img_payload(self, basename):
        """Return (path, is_gz) preferring the gz form for big images."""
        raw = os.path.join(ART, basename)
        gz = raw + ".gz"
        if basename in BIG and os.path.exists(gz):
            return gz, True
        if os.path.exists(raw):
            return raw, False
        if os.path.exists(gz):
            return gz, True
        sys.exit(f"missing artifact {basename}[.gz]")

    def write_offset(self, basename, seek, label):
        path, gz = self._img_payload(basename)
        sink = f"dd of={DISK} bs=512 seek={seek} conv=fsync"
        devcmd = f"{GUNZIP} | {sink}" if gz else sink
        self.nc_write(path, devcmd, label)

    def write_node(self, basename, name, bs=None):
        path, gz = self._img_payload(basename)
        sink = f"dd of=/dev/block/by-name/{name} conv=fsync" + (f" bs={bs}" if bs else "")
        devcmd = f"{GUNZIP} | {sink}" if gz else sink
        self.nc_write(path, devcmd, name)

    def push_dd(self, basename, dest, seek=0, label=None):
        """Reliable write of a small, NON-carve artifact (GPT, kernel, dtb, env):
        adb push to /data/local/tmp then on-device dd. These targets are OUTSIDE the
        userdata carve, so /data staging can't overlap the region being overwritten
        (no brick race), and push+dd lands where tiny nc transfers intermittently
        raced (the 17 KB GPT + 512 B misc were seen not persisting via nc)."""
        path = os.path.join(ART, basename)
        label = label or basename
        tmp = f"/data/local/tmp/_w_{basename}"
        r = self.adb("push", path, tmp, capture_output=True)
        if r.returncode != 0:
            sys.exit(f"{label}: push failed: {r.stderr.decode('utf-8', 'replace')}")
        out, rc = self.su(f"dd if={tmp} of={dest} bs=512 seek={seek} conv=fsync 2>&1; rm -f {tmp}")
        if rc != 0:
            sys.exit(f"{label}: dd failed: {out.strip()}")
        print(f"  WROTE {label} -> {dest} seek={seek}  (push+dd, {os.path.getsize(path):,} B)")

    def write_sector_b64(self, payload_path, devnode, seek_blocks, bs=512):
        """Write a SMALL payload to devnode at seek_blocks*bs via an on-device
        base64 pipe (no nc). A seek'd nc->dd write to the tiny misc partition did
        not persist on this SoC (the sector read back as factory), but an on-device
        `base64 -d | dd` does. Only for small blobs (the b64 rides the command line)."""
        data = open(payload_path, "rb").read()
        b64 = base64.b64encode(data).decode()
        out, rc = self.su(f"printf %s {b64} | base64 -d | "
                          f"dd of={devnode} bs={bs} seek={seek_blocks} conv=fsync 2>/dev/null && echo OK")
        if rc != 0 or "OK" not in out:
            sys.exit(f"{os.path.basename(payload_path)}: on-device write failed: {out.strip()}")
        print(f"  WROTE {os.path.basename(payload_path)} -> {devnode} seek={seek_blocks} ({len(data)} B, b64)")

    # ---- 1. preflight ------------------------------------------------------
    def preflight(self):
        print("\n-- preflight --")
        dev = self.getprop("ro.product.device")
        if dev != "twilight":
            sys.exit(f"device='{dev}' != twilight -- WRONG MODEL. Abort.")
        if "uid=0" not in self.su("id")[0]:
            sys.exit("su root not available")
        vbs = self.getprop("ro.boot.verifiedbootstate")
        print(f"  device=twilight root=ok verifiedbootstate={vbs}"
              + ("" if vbs == "orange" else "  (WARN: expected orange)"))
        if not self.su(f"[ -x {NC.split()[0]} ] && echo y")[0].strip() == "y":
            sys.exit(f"{NC.split()[0]} not present -- need busybox for nc/gzip")
        # fast-fail on a corrupt gz stream: in plain sh, `a | gzip -dc | dd` returns
        # dd's status (0) even if gzip choked, so a truncated decompress would slip
        # past the rc check (the post-write SHA-256 read-back still catches it). When
        # the shell supports `set -o pipefail`, enable it so such a failure aborts the
        # write immediately. Probed (not assumed) because a shell lacking the option
        # may abort on it.
        self.pipefail = self.su("set -o pipefail 2>/dev/null && echo Y")[0].strip() == "Y"
        print(f"  busybox nc ok; pipefail={'on' if self.pipefail else 'off (SHA read-back is the guard)'}")

        byname = self.su("ls /dev/block/by-name/ 2>/dev/null")[0]
        if "CE_FLASH" in byname or "CE_STORAGE" in byname:
            sys.exit("CE_FLASH/CE_STORAGE already exist -- already modified. Abort.")

        gpt = self.su_bytes(f"dd if={DISK} bs=512 count=34 2>/dev/null")
        if len(gpt) < 34 * 512 or gpt[512:520] != b"EFI PART":
            sys.exit("could not read GPT header")
        num = struct.unpack_from("<I", gpt, 512 + 80)[0]
        if num != STOCK_NUM_ENTRIES:
            sys.exit(f"GPT entries={num} != stock {STOCK_NUM_ENTRIES}. Abort.")
        pe = struct.unpack_from("<Q", gpt, 512 + 72)[0]
        arr = gpt[pe * 512:]
        ud_last = None
        for i in range(num):
            e = arr[i * 128:(i + 1) * 128]
            if len(e) < 128:
                break
            if e[56:128].decode("utf-16-le", "replace").split("\x00")[0] == "userdata":
                ud_last = struct.unpack_from("<Q", e, 40)[0]
        if ud_last != STOCK_UD_LAST_LBA:
            sys.exit(f"userdata last_lba={ud_last} != stock {STOCK_UD_LAST_LBA}. Abort.")
        print(f"  GPT stock: 32 entries, userdata ends {ud_last} (full size)")

        active = self.getprop("ro.boot.slot_suffix")
        ce_slot = {"_a": "_b", "_b": "_a"}.get(active)
        if not ce_slot:
            sys.exit(f"bad slot_suffix '{active}'")
        print(f"  active slot={active} -> CE slot={ce_slot}")
        return ce_slot

    # ---- 3. per-unit blobs -------------------------------------------------
    def build_target_blobs(self, ce_slot):
        print("\n-- per-unit blobs (identity-preserving) --")
        env_raw = self.su_bytes("dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null")
        if len(env_raw) < envtool.ENV_SIZE:
            sys.exit(f"short env read: {len(env_raw)}")
        if not envtool.crc_ok(env_raw)[0]:
            sys.exit("target env CRC invalid -- refusing")
        new_env = build_env.build_target_env(env_raw[:envtool.ENV_SIZE], ce_slot, self.default)
        kept = [k for k in build_env.IDENTITY_KEYS if k in envtool.parse(new_env)]
        print(f"  env: +{len(build_env.GENERIC_KEYS)} generic +gate({ce_slot}) default={self.default}; identity kept: {kept}")
        open(os.path.join(ART, "env_target.bin"), "wb").write(new_env)

        # The A/B control block sits at misc offset 0x800 == sector 4. Read that
        # whole 512-byte sector and patch only its first 32 bytes, then write the
        # whole aligned sector back: a `bs=1 seek=2048` pipe-write does NOT persist
        # (sub-block writes over the nc/dd pipe were lost), an aligned bs=512 does.
        sector = bytearray(self.su_bytes(
            "dd if=/dev/block/by-name/misc bs=512 skip=4 count=1 2>/dev/null")[:512])
        info = ab_misc.parse(bytes(sector[:32]))
        print(f"  misc A/B: a=0x{info['a_byte']:02x} b=0x{info['b_byte']:02x} crc_ok={info['crc_ok']}")
        sector[:32] = ab_misc.mark_unbootable(bytes(sector[:32]), ce_slot)
        open(os.path.join(ART, "misc_sector.bin"), "wb").write(bytes(sector))
        print(f"  misc_sector.bin (512 B): {ce_slot} -> unbootable")

    # ---- plan (dry-run) ----------------------------------------------------
    def print_plan(self, ce_slot):
        s = secs()
        print("\n-- write plan (streamed via nc; no writes in dry-run) --")
        for lbl, src, dst in [
            ("GPT-primary", "gpt_primary.bin", f"{DISK} seek=0"),
            ("GPT-backup", "gpt_backup.bin", f"{DISK} seek={GPT_BACKUP_LBA}"),
            ("CE_FLASH", "ce_flash.img[.gz]", f"{DISK} seek={s['CE_FLASH'][0]}"),
            ("CE_STORAGE", "ce_storage.img[.gz]", f"{DISK} seek={s['CE_STORAGE'][0]}"),
            ("userdata-sb wipe", "/dev/zero x8192", f"{DISK} seek={s['userdata'][0]}"),
            ("kernel", "boota.img", f"by-name/boot{ce_slot}"),
            ("dtb", "dtboa.img", f"by-name/dtbo{ce_slot}"),
            ("A/B misc", "misc_target.bin", "by-name/misc seek=2048"),
            ("env (LAST)", "env_target.bin", "by-name/env"),
            ("MPT wipe", "/dev/zero x8", "by-name/reserved seek=0 (blank Amlogic MPT)"),
        ]:
            print(f"   {lbl:<16} {src:<22} -> {dst}")
        print("   then: BCB <- boot-recovery + --wipe_data (next reboot reformats userdata)")
        print("   then: pm disable-user com.xiaomi.mitv.updateservice")

    # ---- 4. PC-side backups (before any write) -----------------------------
    def backups_to_pc(self, ce_slot):
        print("\n-- backups -> pulled_backups/ (before any write) --")
        dest = os.path.join(HERE, "..", "pulled_backups")
        os.makedirs(dest, exist_ok=True)

        def pull(name, cmd):
            data = self.su_bytes(cmd + " 2>/dev/null | base64")
            raw = base64.b64decode(b"".join(bytes(data).split()))
            open(os.path.join(dest, name), "wb").write(raw)
            print(f"  {name} ({len(raw)} B)")

        pull("gpt_primary_pre.bin", f"dd if={DISK} bs=512 count=34")
        # full 2 MiB backup-GPT region (array + alt header at the last sector) -- this is
        # exactly the span the install overwrites, so restore_stock_gpt.py can reverse it
        # cleanly from pulled_backups/ (a 34-sector grab would miss the alt header).
        pull("gpt_backup_pre.bin", f"dd if={DISK} bs=512 skip={GPT_BACKUP_LBA} count=4096")
        pull("env_pre.bin", "dd if=/dev/block/by-name/env bs=512 count=128")
        pull("misc_pre.bin", "dd if=/dev/block/by-name/misc bs=512 count=64")
        pull(f"boot{ce_slot}_pre.bin", f"dd if=/dev/block/by-name/boot{ce_slot}")
        pull(f"dtbo{ce_slot}_pre.bin", f"dd if=/dev/block/by-name/dtbo{ce_slot}")
        pull("reserved_pre.bin", "dd if=/dev/block/by-name/reserved")   # identity insurance
        pull("frp_pre.bin", "dd if=/dev/block/by-name/frp")
        if open(os.path.join(dest, "gpt_primary_pre.bin"), "rb").read()[512:520] != b"EFI PART":
            sys.exit("backup sanity failed (GPT) -- aborting before writes")
        print("  backups verified readable")

    # ---- 5. re-guard + streamed writes -------------------------------------
    def reguard(self):
        byname = self.su("ls /dev/block/by-name/ 2>/dev/null")[0]
        if self.getprop("ro.product.device") != "twilight" \
                or "CE_FLASH" in byname or "CE_STORAGE" in byname:
            sys.exit("re-guard failed (state changed) -- aborting before writes")

    def write_all(self, ce_slot, skip_gpt=False, skip_sbwipe=False):
        s = secs()
        print("\n-- writes (GPT/kernel/dtb/env: push+dd; CE images: nc; misc: b64) --")
        # GPT first (push+dd, reliable): commits new geometry so a later failure
        # still lets Android reformat the shrunk userdata and boot.
        if not skip_gpt:
            self.push_dd("gpt_primary.bin", DISK, 0, "GPT-primary")
            self.push_dd("gpt_backup.bin", DISK, GPT_BACKUP_LBA, "GPT-backup")
        self._verify_gpt()
        # All /data-staged (push+dd) + small writes go NOW, while userdata is still
        # healthy. The CE writes further down land in the carve by raw offset and can
        # disturb a live userdata fs (its SB is wiped right after), which could flip
        # /data read-only and break a later push. So kernel/dtb/misc/env first.
        self.push_dd("boota.img", f"/dev/block/by-name/boot{ce_slot}", 0, f"boot{ce_slot}")
        self.push_dd("dtboa.img", f"/dev/block/by-name/dtbo{ce_slot}", 0, f"dtbo{ce_slot}")
        # A/B misc: aligned 512-byte sector @ sector 4 (offset 0x800), on-device b64
        # (a seek'd nc->dd write to the small misc partition did not persist).
        self.write_sector_b64(os.path.join(ART, "misc_sector.bin"),
                              "/dev/block/by-name/misc", 4)
        self._verify_misc(ce_slot)
        # env (push+dd; a bad env just falls back to default -> Android still boots)
        self.push_dd("env_target.bin", "/dev/block/by-name/env", 0, "env")
        self._verify_env(ce_slot)
        # Blank the Amlogic proprietary partition table (MPT) so the CoreELEC kernel
        # falls back to the GPT and can see CE_FLASH/CE_STORAGE. Non-carve, idempotent.
        self.wipe_mpt()
        self._verify_mpt()
        # CoreELEC filesystems LAST: MUST nc-stream -- they land in the carve by raw
        # offset, so staging them on userdata would be the read-during-overwrite
        # brick race; too big to push anyway. After this, only the SB wipe + sync.
        self.write_offset("ce_flash.img", s["CE_FLASH"][0], "CE_FLASH")
        self.write_offset("ce_storage.img", s["CE_STORAGE"][0], "CE_STORAGE")
        # secondary measure: zero the userdata superblock. The PRIMARY, deterministic
        # reformat trigger is the BCB armed at the end of the install (arm_factory_reset);
        # this SB wipe by itself is undone by a clean reboot's cached-superblock writeback
        # (Android flushes the original SB back over the zeros on unmount, so no reformat).
        # skip_sbwipe=True for finish_install: userdata is ALREADY the correct size there,
        # and a wipe that DOES take triggers a recovery factory-reset which resets the env
        # to stock -- wiping the very gate finish_install just wrote (a re-gate loop).
        if skip_sbwipe:
            print("  (skip userdata SB wipe -- userdata already sized; preserves env gate)")
        else:
            print(f"  WIPE userdata superblock (4 MiB @ {s['userdata'][0]}s)")
            if self.su(f"dd if=/dev/zero of={DISK} bs=512 seek={s['userdata'][0]} "
                       f"count=8192 conv=fsync")[1] != 0:
                sys.exit("userdata wipe failed")
            self.su("sync")

    # ---- verification ------------------------------------------------------
    def _drop_caches(self):
        """Force subsequent reads to hit the eMMC, not boot-time cached pages."""
        self.su(f"sync; echo 3 > /proc/sys/vm/drop_caches; blockdev --flushbufs {DISK}")

    def _verify_gpt(self):
        self._drop_caches()
        gpt = self.su_bytes(f"dd if={DISK} bs=512 count=2 2>/dev/null")
        num = struct.unpack_from("<I", gpt, 512 + 80)[0]
        if num != 128:
            sys.exit(f"GPT verify failed: entries={num}")
        print("  verify GPT: 128 entries OK")

    def _verify_misc(self, ce_slot):
        self._drop_caches()
        m = self.su_bytes("dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32 2>/dev/null")[:32]
        info = ab_misc.parse(m)
        byte = info["a_byte"] if ce_slot == "_a" else info["b_byte"]
        if not info["crc_ok"] or byte != 0:
            sys.exit(f"misc verify failed (crc_ok={info['crc_ok']} byte=0x{byte:02x})")
        print(f"  verify misc: {ce_slot} unbootable, crc OK")

    def _verify_env(self, ce_slot):
        env = self.su_bytes("dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null")[:envtool.ENV_SIZE]
        if not envtool.crc_ok(env)[0]:
            sys.exit("env verify failed: CRC invalid")
        d = envtool.parse(env)
        if d.get("boot_ce") != "0" or f"imgread kernel boot{ce_slot}" not in d.get("bootcefromemmc", ""):
            sys.exit("env verify failed: gate/boot_ce wrong")
        print(f"  verify env: CRC OK, boot_ce=0, gate->boot{ce_slot}")

    # ---- Amlogic MPT (kernel-visible partition table) ----------------------
    def wipe_mpt(self):
        r"""Blank the Amlogic proprietary partition table ("MPT") at the start of the
        `reserved` partition so the CoreELEC (Amlogic vendor) kernel falls back to the
        full GPT and can see CE_FLASH/CE_STORAGE (mmcblk0p33/p34).

        Why this is needed: the Amlogic kernel, when a VALID MPT exists at 36 MiB
        (reserved offset 0), uses it and IGNORES the GPT ("skip mounting disk with MPT
        partition"). The stock MPT is capped at MAX_MMC_PART_NUM=32 entries listing only
        the Android partitions, so our GPT-added CE_FLASH is invisible -> CoreELEC hangs
        on the boot logo (can't mount boot=LABEL=CE_FLASH). A unit with NO MPT falls back
        to the GPT scan and boots fine. An applied A/B OTA re-populates the MPT (factory
        stock) -- exactly how a previously-working unit regressed -- so we blank it here.
        `reserved_pre.bin` (pulled before any write) backs up the original.

        Only the MPT struct (magic "MPT\0" + up to 32x40 B entries = 0x518 B) is touched:
        we zero the first 8 sectors (4 KiB). The `AMLNORMAL` block at reserved+0x4000 and
        all device identity further in `reserved` are untouched (verified on hardware).
        Idempotent: a no-op if no MPT magic is present.
        """
        magic = self.su_bytes("dd if=/dev/block/by-name/reserved bs=4 count=1 2>/dev/null")[:4]
        if magic != b"MPT\x00":
            print(f"  MPT: none present (magic={magic!r}) -- kernel already GPT-visible, skip")
            return
        out, rc = self.su("dd if=/dev/zero of=/dev/block/by-name/reserved bs=512 count=8 conv=fsync 2>&1")
        if rc != 0:
            sys.exit(f"MPT wipe failed: {out.strip()}")
        print("  WIPE Amlogic MPT (reserved[0:0x1000]) -> kernel falls back to GPT (CE_FLASH visible)")

    def _verify_mpt(self):
        self._drop_caches()
        magic = self.su_bytes("dd if=/dev/block/by-name/reserved bs=4 count=1 2>/dev/null")[:4]
        if magic == b"MPT\x00":
            sys.exit("MPT verify failed: 'MPT' magic still present in reserved -- "
                     "CoreELEC kernel would not see CE_FLASH")
        print("  verify MPT: blanked (kernel uses GPT)")

    # ---- end-to-end SHA-256 read-back verification -------------------------
    def _sha_file(self, path):
        """sha256 + byte length of a raw file."""
        h = hashlib.sha256(); n = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk); n += len(chunk)
        return h.hexdigest(), n

    def _sha_image_raw(self, basename):
        """sha256 + length of the RAW image, decompressing the .gz on the fly
        if only the gz form is shipped (so it matches what landed on disk)."""
        raw = os.path.join(ART, basename); gz = raw + ".gz"
        if os.path.exists(raw):
            return self._sha_file(raw)
        if not os.path.exists(gz):
            sys.exit(f"missing {basename}[.gz] for verify")
        h = hashlib.sha256(); n = 0
        with gzip.open(gz, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk); n += len(chunk)
        return h.hexdigest(), n

    def _sha_device(self, dd_if, skip_sectors, nbytes):
        """Read exactly nbytes starting skip_sectors*512 into dd_if; hash on-device."""
        count = (nbytes + 511) // 512
        cmd = (f"dd if={dd_if} bs=512 skip={skip_sectors} count={count} 2>/dev/null "
               f"| head -c {nbytes} | sha256sum")
        out, _ = self.su(cmd)
        out = out.strip()
        return out.split()[0] if out else ""

    def verify_writes(self, ce_slot):
        """SHA-256 every written region off the eMMC vs the PC-side source."""
        print("\n-- SHA-256 read-back verification (off eMMC) --")
        self._drop_caches()
        s = secs()
        # (label, local-hash fn, device dd source, skip in 512-sectors)
        plan = [
            ("GPT-primary", self._sha_file(os.path.join(ART, "gpt_primary.bin")), DISK, 0),
            ("GPT-backup",  self._sha_file(os.path.join(ART, "gpt_backup.bin")),  DISK, GPT_BACKUP_LBA),
            ("CE_FLASH",    self._sha_image_raw("ce_flash.img"),                  DISK, s["CE_FLASH"][0]),
            ("CE_STORAGE",  self._sha_image_raw("ce_storage.img"),                DISK, s["CE_STORAGE"][0]),
            ("kernel",      self._sha_file(os.path.join(ART, "boota.img")),       f"/dev/block/by-name/boot{ce_slot}", 0),
            ("dtb",         self._sha_file(os.path.join(ART, "dtboa.img")),       f"/dev/block/by-name/dtbo{ce_slot}", 0),
            ("env",         self._sha_file(os.path.join(ART, "env_target.bin")),  "/dev/block/by-name/env", 0),
            ("A/B misc",    self._sha_file(os.path.join(ART, "misc_sector.bin")), "/dev/block/by-name/misc", 4),  # sector 4 == 0x800
        ]
        allok = True
        for label, (lh, n), dd_if, skip in plan:
            dh = self._sha_device(dd_if, skip, n)
            ok = (lh == dh and dh != "")
            allok = allok and ok
            tail = "" if ok else f"  PC={lh[:16]} DEV={dh[:16] or '(empty)'}"
            print(f"  {'OK  ' if ok else 'FAIL'} {label:<11} {n:>12,} B  {dh[:16]}{tail}")
        if not allok:
            sys.exit("SHA-256 verification FAILED -- a written region does not match. "
                     "Do NOT trust this install; investigate before rebooting.")
        print("  all regions byte-identical to source (SHA-256).")

    # ---- 6. OTA ------------------------------------------------------------
    def disable_ota(self):
        print("\n-- disable vendor OTA (transient) --")
        out, _ = self.su("pm disable-user --user 0 com.xiaomi.mitv.updateservice")
        print("  " + (out.strip() or "(no output)"))
        print("  NOTE: this disable is erased by the first-boot userdata reformat.")
        print("  For DURABLE blocking, after first boot run:")
        print("    python install_blockota.py --serial <ip:port>   (installs the Block-OTA Magisk module)")

    # ---- 7. arm the userdata reformat (deterministic) ----------------------
    def arm_factory_reset(self):
        """Schedule a recovery-driven userdata reformat on the next boot via the BCB.

        Why not just the superblock wipe: a clean `adb reboot` unmounts /data and
        flushes the cached ORIGINAL superblock back over our zeros, so no reformat
        fires -- the f2fs stays at the OLD full-userdata size on the now-smaller
        partition (df shows ~4176 MiB on a 2376 MiB device; it mounts but will
        I/O-error once usage crosses the partition end). Deterministic fix: set the
        bootloader control block (BCB, at misc offset 0) command='boot-recovery' and
        recovery='recovery\\n--wipe_data\\n'. The next reboot enters recovery, which
        mkfs's userdata to the NEW partition size and re-keys metadata encryption,
        clears the BCB, then boots Android. Same canonical path `fastboot -w` / OTA
        uses. The A/B slot_metadata (misc sector 4 / offset 0x800) and everything at
        or past offset 2048 is untouched -- only the first 1 KiB (BCB) is rewritten.
        """
        print("\n-- arm userdata reformat (BCB -> recovery --wipe_data) --")
        # read-modify-write the first two 512 B sectors (BCB: command[0:32],
        # status[32:64], recovery[64:832]) so nothing else in misc's first 1 KiB is
        # disturbed. An aligned bs=512 b64 write persists where a bs=1 seek does not.
        sec = bytearray(self.su_bytes(
            "dd if=/dev/block/by-name/misc bs=512 count=2 2>/dev/null")[:1024])
        if len(sec) < 1024:
            sec += bytearray(1024 - len(sec))
        sec[0:32] = b"boot-recovery".ljust(32, b"\x00")   # command[32]
        sec[64:832] = bytearray(768)                      # clear recovery[768]
        rec = b"recovery\n--wipe_data\n"
        sec[64:64 + len(rec)] = rec
        path = os.path.join(ART, "_bcb_wipe.bin")
        open(path, "wb").write(bytes(sec))
        self.write_sector_b64(path, "/dev/block/by-name/misc", 0)
        chk = self.su_bytes("dd if=/dev/block/by-name/misc bs=512 count=1 2>/dev/null")[:13]
        if chk != b"boot-recovery":
            sys.exit("BCB arm failed: 'boot-recovery' not read back -- userdata will NOT reformat")
        print("  BCB set: next reboot -> recovery reformats userdata to the new size, then Android")


if __name__ == "__main__":
    main()
