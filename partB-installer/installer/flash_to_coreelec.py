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

  python flash_to_coreelec.py --serial <serial> --dry-run
  python flash_to_coreelec.py --serial <serial> --yes
"""
import argparse, base64, gzip, hashlib, os, socket, subprocess, sys, struct, time

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "..", "artifacts")
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import envtool, build_env, ab_misc, layout as L, devices, bundle  # noqa: E402

# Build stamp printed in every run header (flash + finish). A hardcoded constant on
# PURPOSE: it is compiled into the bytecode, so a stale __pycache__/*.pyc (the classic
# "I copied the new .py but Python kept the old compiled copy" trap) prints the OLD
# value -- proving which code actually executed, not just which source sits on disk.
# Bump on every installer change, and keep it in step with the release tag -- a stamp
# that lags the tag defeats its own purpose. history: r1=v1.2.3, r2=nc+USB write retry,
# r3=SHA verify/gate fix for regions >=4 GiB (busybox head -c overflow),
# v1.2.4=stage_magisk pre-root identity + bootloader flash gate,
# v1.2.5=quiesce /data + carve stability probe before the CE streams,
# v1.2.6=artifact SHA-256 gate + CE image size gate + per-device pulled_backups.
BUILD = "1.2.6 (artifact SHA gate + CE size gate + per-device backups)"

DISK = "/dev/block/mmcblk0"
BIG = {"ce_flash.img", "ce_storage.img"}
NC = "/vendor/bin/busybox nc"
GUNZIP = "/vendor/bin/busybox gzip -dc"


class _NcRetry(Exception):
    """A transient nc-transfer failure worth retrying (a stalled transfer or a
    listener that never accepted). A retry re-streams from byte 0 into a fresh
    port; the sink dd always re-seeks to the region start, so it's a clean full
    overwrite, never an append. A non-zero device rc (a real decompress/dd error)
    is NOT this -- it fails immediately rather than looping on a deterministic fault."""
# Geometry (GPT backup LBA, stock entry count, stock userdata last-LBA) and the
# per-device artifact directory now come from the IDENTIFIED device (self.device /
# self.artdir), not module constants -- the stick and box differ on all of them.

# Decompressed sha256 + length of each CE source image, keyed by the payload path we
# actually stream. Filled ONCE (by check_ce_image_sizes, before the first write) and
# reused by verify_writes and the idempotent resume gate. Expanding the box's 10 GiB
# ce_storage.img is minutes of CPU, so hashing it three times per run would be three
# times the wait for no new information -- the file cannot change mid-run.
_SRC_SHA = {}


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def secs(dev):
    return {n: (a, b, c) for n, a, b, c in L.as_sectors(dev)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb serial (USB device id); omit to auto-pick the only device")
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
    print(f"    build={BUILD}")
    ce_slot = g.preflight()          # identifies the device -> sets g.device / g.artdir
    require_artifacts(g.device)
    g.check_ce_image_sizes()         # the CE images fit their partitions (before ANY write)
    g.build_target_blobs(ce_slot)
    if dry:
        g.print_plan(ce_slot)
        print("\nDRY-RUN only. Re-run with --yes to install.")
        return
    g.backups_to_pc(ce_slot)
    g.reguard()
    # OTA disable runs BEFORE write_all: the quiesce inside write_all stops the
    # Android framework, and `pm` needs the framework. Transient either way (the
    # first-boot reformat erases it); stage2's Magisk module is the durable block.
    g.disable_ota()
    g.write_all(ce_slot)             # each nc port is forwarded + removed by _nc_write_once
    g.verify_writes(ce_slot)
    g.arm_factory_reset()
    print("\n=== install complete ===")
    print("  NEXT REBOOT -> recovery auto-reformats userdata to the new size (factory-reset-")
    print("  like), re-keys encryption, then boots Android. After that:")
    print("    normal reboot -> Android (default)")
    print("    'Reboot to CoreELEC' app (boot_ce=1) -> CoreELEC")


def artdir_for(dev):
    return os.path.join(ART, dev.slug)


def backup_dir_for(dev):
    """pulled_backups/<slug>/ -- this unit's pre-install dumps, namespaced by device.

    Flat pulled_backups/ meant installing the box after the stick silently overwrote the
    stick's ONLY pre-install backups (same filenames), leaving nothing to reverse the
    stick with. The restore scripts read the same per-device path (and still fall back to
    a legacy flat directory, so backups pulled before this change stay usable)."""
    return os.path.join(HERE, "..", "pulled_backups", dev.slug)


def require_artifacts(dev):
    """Every flashable is present, is the right size for THIS device, and -- when the
    bundle ships a manifest -- is byte-for-byte the file that was built.

    The size checks are the part that works in a source checkout (no manifest): the GPT
    blobs are pure geometry, so a stale or wrong-device gpt_backup.bin is exactly the
    kind of thing that would otherwise be dd'd over the backup GPT and only noticed by
    the read-back afterwards."""
    artdir = artdir_for(dev)
    need = ["gpt_primary.bin", "gpt_backup.bin", "boota.img", "dtboa.img"]
    missing = [n for n in need if not os.path.exists(os.path.join(artdir, n))]
    for n in BIG:
        if not (os.path.exists(os.path.join(artdir, n)) or os.path.exists(os.path.join(artdir, n + ".gz"))):
            missing.append(n + "[.gz]")
    if missing:
        sys.exit(f"missing artifacts for {dev.slug}: {missing} (looked in artifacts/{dev.slug}/) "
                 f"-- run: python build/build_all.py --device {dev.slug}")

    # The GPT blobs are dd'd at fixed LBAs, so their length IS their footprint on the
    # eMMC: gpt_primary covers LBA 0..33, gpt_backup covers gpt_backup_lba..end-of-disk.
    # A blob built for the other unit has the wrong length here and is refused before it
    # can run past the end of one of those regions.
    for name, want in (("gpt_primary.bin", 34 * devices.SECTOR),
                       ("gpt_backup.bin", dev.gpt_backup_span * devices.SECTOR)):
        got = os.path.getsize(os.path.join(artdir, name))
        if got != want:
            sys.exit(f"{dev.slug}/{name} is {got:,} B but the {dev.name} needs exactly "
                     f"{want:,} B -- wrong device's artifact, or a stale build. REFUSING "
                     f"(it is written to a fixed LBA). Rebuild: python build/build_all.py "
                     f"--device {dev.slug}")

    # vs SHA256SUMS.txt, when this is a dist bundle. The installer's own read-back only
    # proves the eMMC matches the FILE; this proves the file matches what we shipped.
    print(f"\n-- artifacts [{dev.slug}] --")
    paths = [os.path.join(artdir, n) for n in need]
    paths += [os.path.join(artdir, n + ext) for n in BIG for ext in ("", ".gz")]
    n_checked = bundle.verify(paths)
    print(f"  {n_checked} artifact(s) verified against SHA256SUMS.txt" if n_checked else
          "  (no SHA256SUMS.txt covering these artifacts -- source checkout; the manifest "
          "can only vouch for a dist bundle)")


class Ctx:
    def __init__(self, serial, dry, port, default="android"):
        self.serial = serial
        self.dry = dry
        self.port = port
        self.default = default  # "android" | "coreelec" -- which OS a normal reboot boots
        self.pipefail = False   # set in preflight if the device shell supports it
        self.device = None      # set in preflight (devices.identify)
        self.artdir = None      # artifacts/<device.slug>/ -- set in preflight
        # True once the carved GPT is on the eMMC. From that moment a failure leaves the
        # unit in a state a plain reboot must NOT be allowed to meet (see die()), so every
        # abort past this point carries the recovery instructions. finish_install sets it
        # up front: it only runs on a unit whose GPT is ALREADY carved.
        self.gpt_written = False

    # ---- adb (reads / commands; NOT writes) --------------------------------
    def adb(self, *a, **k):
        return subprocess.run(["adb", "-s", self.serial, *a], **k)

    # ---- aborting --------------------------------------------------------------
    def die(self, msg):
        """Abort, telling the user what state the unit is actually in.

        Before the GPT lands, nothing has been written and a plain exit is the whole
        story. AFTER it lands, userdata has been shrunk on disk but the reformat that
        resizes its filesystem is armed LAST (arm_factory_reset) -- so between those two
        points a reboot brings Android up on an f2fs that still believes it owns the old,
        larger span. It mounts, and then I/O-errors once usage crosses the new partition
        end. That is the one state a user must not walk into unwarned, and every abort
        past the GPT write can produce it.
        """
        if self.gpt_written:
            msg += (
                "\n\n  ---- IMPORTANT: DO NOT REBOOT THIS DEVICE YET ----\n"
                "  The carved GPT is already on the eMMC, but the userdata reformat is NOT\n"
                "  armed. Rebooting now brings Android up on an oversized filesystem that\n"
                "  will corrupt itself once it fills past the new partition end.\n"
                "\n"
                "  Finish the install (hash-gated: it re-writes only what actually failed,\n"
                "  and arms the reformat):\n"
                f"    python installer/finish_install.py --serial {self.serial} "
                f"--default {self.default} --yes\n"
                "\n"
                "  Or back the device out to stock instead:\n"
                f"    python installer/restore_stock_gpt.py --serial {self.serial} --yes\n")
        sys.exit(msg)

    def _no_dry(self, what):
        """A dry run must not reach a write. main() returns before write_all, so this can
        only fire on a wiring bug -- which is exactly when you want it to fire, on the PC,
        instead of on someone's eMMC."""
        if self.dry:
            raise AssertionError(f"BUG: {what} attempted during a DRY-RUN -- no device "
                                 f"write may be reached on this path")

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

    def nc_write(self, payload_path, devcmd, label, verify_timeout=900, attempts=3):
        """Stream payload_path into `nc -l | devcmd`, retrying a transient failure.
        Each attempt gets a fresh port and re-streams from byte 0 (a full overwrite,
        never an append -- the sink dd re-seeks to the region start every time). Only
        transient failures (stall / no-connect) retry; a device rc != 0 fails hard."""
        self._no_dry(f"nc stream of {label}")
        for attempt in range(1, attempts + 1):
            try:
                return self._nc_write_once(payload_path, devcmd, label, verify_timeout)
            except _NcRetry as e:
                if attempt == attempts:
                    self.die(f"{label}: {e} (gave up after {attempts} attempts)")
                print(f"  {label}: {e} -- retrying ({attempt + 1}/{attempts})")

    def _nc_write_once(self, payload_path, devcmd, label, verify_timeout=900):
        """One streamed-write attempt over its own freshly-forwarded tcp port.
        Raises _NcRetry on a transient failure; sys.exit on a hard (rc != 0) one."""
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
                raise _NcRetry(f"could not connect to nc tunnel on port {port} {err}")
            # create_connection(timeout=5) leaves a 5 s timeout on the socket, which
            # then governs every sendall below -- NOT just the connect. On a large carve
            # the device-side `gzip -dc | dd` backpressures the pipe far longer than 5 s
            # (an empty ext4 gz expands to a huge zero-run that dd writes at eMMC speed),
            # so a mid-stream sendall blocks past 5 s and raises a spurious TimeoutError.
            # This bit the box's 10 GiB CE_STORAGE but not the stick's 1.2 GiB. Clear it:
            # the transfer's real bound is proc.wait(verify_timeout) after SHUT_WR.
            sock.settimeout(None)
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
                raise _NcRetry("device write timed out")
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            if rc != 0:
                self.die(f"{label}: device write failed (rc={rc}) {err}")
            print(f"  WROTE {label}  ({sent:,} B sent)")
        finally:
            self.adb("forward", "--remove", f"tcp:{port}", capture_output=True)

    def _img_payload(self, basename):
        """Return (path, is_gz) for the image we WRITE, preferring the gz form for the
        big carve images (gunzip streams on-device).

        The gz is DERIVED from the raw, so a raw NEWER than its gz means the build was
        rerun and the gz is a leftover from the previous CoreELEC payload -- refuse
        rather than flash the old build. A shipped dist carries the gz only, so this
        only ever bites in the dev tree, which is exactly where the pair can drift."""
        raw = os.path.join(self.artdir, basename)
        gz = raw + ".gz"
        if os.path.exists(raw) and os.path.exists(gz) and \
                os.path.getmtime(gz) < os.path.getmtime(raw):
            sys.exit(f"{basename}.gz is older than {basename} -- stale artifact; rerun: "
                     f"python build/build_all.py --device {self.device.slug}")
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

    def _write_retry(self, attempt_fn, label, attempts=3, pause=1.0):
        """Run attempt_fn() -- a single idempotent-overwrite write -- up to `attempts`
        times, retrying a transient adb/dd failure. attempt_fn returns (ok, detail);
        on ok it returns immediately, on the final failure it sys.exits. Safe for every
        caller because each re-seeks/overwrites the SAME region from scratch, so a retry
        is a full rewrite, never a partial append. Unlike nc_write (which can tell a
        stall from a device fault) these USB push+dd / b64 writes have no reliable
        transient-vs-deterministic signal, so ANY failure retries; a deterministic
        fault simply exhausts the attempts and exits (a few seconds, not a loop)."""
        self._no_dry(label)
        for attempt in range(1, attempts + 1):
            ok, detail = attempt_fn()
            if ok:
                return
            if attempt == attempts:
                self.die(f"{label}: {detail} (gave up after {attempts} attempts)")
            print(f"  {label}: {detail} -- retrying ({attempt + 1}/{attempts})")
            time.sleep(pause)

    def push_dd(self, basename, dest, seek=0, label=None):
        """Reliable write of a small, NON-carve artifact (GPT, kernel, dtb, env):
        adb push to /data/local/tmp then on-device dd. These targets are OUTSIDE the
        userdata carve, so /data staging can't overlap the region being overwritten
        (no brick race), and push+dd lands where tiny nc transfers intermittently
        raced (the 17 KB GPT + 512 B misc were seen not persisting via nc)."""
        path = os.path.join(self.artdir, basename)
        label = label or basename
        tmp = f"/data/local/tmp/_w_{basename}"

        def attempt():
            r = self.adb("push", path, tmp, capture_output=True)
            if r.returncode != 0:
                return False, f"push failed: {r.stderr.decode('utf-8', 'replace').strip()}"
            # rm runs even on dd failure (`;`-joined), so a retry re-pushes clean.
            out, rc = self.su(f"dd if={tmp} of={dest} bs=512 seek={seek} conv=fsync 2>&1; rm -f {tmp}")
            if rc != 0:
                return False, f"dd failed: {out.strip()}"
            return True, None

        self._write_retry(attempt, label)
        print(f"  WROTE {label} -> {dest} seek={seek}  (push+dd, {os.path.getsize(path):,} B)")

    def write_sector_b64(self, payload_path, devnode, seek_blocks, bs=512):
        """Write a SMALL payload to devnode at seek_blocks*bs via an on-device
        base64 pipe (no nc). A seek'd nc->dd write to the tiny misc partition did
        not persist on this SoC (the sector read back as factory), but an on-device
        `base64 -d | dd` does. Only for small blobs (the b64 rides the command line)."""
        data = open(payload_path, "rb").read()
        b64 = base64.b64encode(data).decode()
        name = os.path.basename(payload_path)

        def attempt():
            out, rc = self.su(f"printf %s {b64} | base64 -d | "
                              f"dd of={devnode} bs={bs} seek={seek_blocks} conv=fsync 2>/dev/null && echo OK")
            if rc != 0 or "OK" not in out:
                return False, f"on-device write failed: {out.strip()}"
            return True, None

        self._write_retry(attempt, name)
        print(f"  WROTE {name} -> {devnode} seek={seek_blocks} ({len(data)} B, b64)")

    # ---- 1. preflight ------------------------------------------------------
    def preflight(self):
        print("\n-- preflight --")
        # Identify the physical unit by model + eMMC size (NOT the shared codename
        # `twilight`, which the stick and box both report). require_layout=True: a
        # geometry step must refuse any device whose carve layout isn't implemented.
        self.device = devices.identify(
            self.getprop,
            devices.sectors_reader(lambda cmd: self.su(cmd)[0]),
            require_layout=True, log=print)
        self.artdir = artdir_for(self.device)
        if "uid=0" not in self.su("id")[0]:
            sys.exit("su root not available")
        vbs = self.getprop("ro.boot.verifiedbootstate")
        print(f"  root=ok verifiedbootstate={vbs}"
              + ("" if vbs == "orange" else "  (WARN: expected orange)"))
        if self.su(f"[ -x {NC.split()[0]} ] && echo y")[0].strip() != "y":
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
        if num != self.device.stock_num_entries:
            sys.exit(f"GPT entries={num} != stock {self.device.stock_num_entries}. Abort.")
        pe = struct.unpack_from("<Q", gpt, 512 + 72)[0]
        arr = gpt[pe * 512:]
        ud_last = None
        for i in range(num):
            e = arr[i * 128:(i + 1) * 128]
            if len(e) < 128:
                break
            if e[56:128].decode("utf-16-le", "replace").split("\x00")[0] == "userdata":
                ud_last = struct.unpack_from("<Q", e, 40)[0]
        if ud_last != self.device.stock_ud_last_lba:
            sys.exit(f"userdata last_lba={ud_last} != stock {self.device.stock_ud_last_lba}. Abort.")
        print(f"  GPT stock: {num} entries, userdata ends {ud_last} (full size)")

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
        open(os.path.join(self.artdir, "env_target.bin"), "wb").write(new_env)

        # The A/B control block sits at misc offset 0x800 == sector 4. Read that
        # whole 512-byte sector and patch only its first 32 bytes, then write the
        # whole aligned sector back: a `bs=1 seek=2048` pipe-write does NOT persist
        # (sub-block writes over the nc/dd pipe were lost), an aligned bs=512 does.
        sector = bytearray(self.su_bytes(
            "dd if=/dev/block/by-name/misc bs=512 skip=4 count=1 2>/dev/null")[:512])
        info = ab_misc.parse(bytes(sector[:32]))
        print(f"  misc A/B: a=0x{info['a_byte']:02x} b=0x{info['b_byte']:02x} crc_ok={info['crc_ok']}")
        sector[:32] = ab_misc.mark_unbootable(bytes(sector[:32]), ce_slot)
        open(os.path.join(self.artdir, "misc_sector.bin"), "wb").write(bytes(sector))
        print(f"  misc_sector.bin (512 B): {ce_slot} -> unbootable")

    # ---- plan (dry-run) ----------------------------------------------------
    def print_plan(self, ce_slot):
        s = secs(self.device)
        print(f"\n-- write plan for [{self.device.slug}] (streamed via nc; no writes in dry-run) --")
        for lbl, src, dst in [
            ("GPT-primary", "gpt_primary.bin", f"{DISK} seek=0"),
            ("GPT-backup", "gpt_backup.bin", f"{DISK} seek={self.device.gpt_backup_lba}"),
            ("CE_FLASH", "ce_flash.img[.gz]", f"{DISK} seek={s['CE_FLASH'][0]}"),
            ("CE_STORAGE", "ce_storage.img[.gz]", f"{DISK} seek={s['CE_STORAGE'][0]}"),
            ("userdata-sb wipe", "/dev/zero x8192", f"{DISK} seek={s['userdata'][0]}"),
            ("kernel", "boota.img", f"by-name/boot{ce_slot}"),
            ("dtb", "dtboa.img", f"by-name/dtbo{ce_slot}"),
            ("A/B misc", "misc_sector.bin", "by-name/misc seek=2048 B (sector 4)"),
            ("env (LAST)", "env_target.bin", "by-name/env"),
            ("MPT wipe", "/dev/zero x8", "by-name/reserved seek=0 (blank Amlogic MPT)"),
        ]:
            print(f"   {lbl:<16} {src:<22} -> {dst}")
        print("   then: BCB <- boot-recovery + --wipe_data (next reboot reformats userdata)")
        print("   then: pm disable-user com.xiaomi.mitv.updateservice")

    # ---- 4. PC-side backups (before any write) -----------------------------
    def _part_bytes(self, name):
        """Byte size of a by-name partition (sizes the whole-partition pulls)."""
        out, _ = self.su(f"blockdev --getsize64 /dev/block/by-name/{name}")
        out = out.strip()
        if not out.isdigit() or int(out) == 0:
            sys.exit(f"cannot size partition {name!r} for backup ({out or '(no output)'}) "
                     "-- aborting before writes")
        return int(out)

    def backups_to_pc(self, ce_slot):
        """Pull the pre-install backups the restore scripts depend on.

        Every pull is verified end-to-end: the SHA-256 of the bytes that landed
        on the PC must equal an independent on-device hash of the same region
        (verify_writes' check, in reverse). A dd failure or a truncated/corrupt
        adb transfer therefore aborts HERE, before the first write -- not at
        restore time when the stock data is already gone."""
        dest = backup_dir_for(self.device)
        print(f"\n-- backups -> pulled_backups/{self.device.slug}/ "
              f"(before any write; SHA-256 verified) --")
        os.makedirs(dest, exist_ok=True)

        def pull(name, dd_if, skip, nbytes):
            sectors = nbytes // 512          # every backup region is sector-aligned
            data = self.su_bytes(f"dd if={dd_if} bs=512 skip={skip} count={sectors} "
                                 "2>/dev/null | base64")
            raw = base64.b64decode(b"".join(bytes(data).split()))
            pc = hashlib.sha256(raw).hexdigest()
            dev = self._sha_device(dd_if, skip, nbytes)
            if len(raw) != nbytes or not dev or pc != dev:
                sys.exit(f"backup pull FAILED for {name}: got {len(raw):,}/{nbytes:,} B, "
                         f"PC={pc[:16]} DEV={dev[:16] or '(empty)'} -- aborting before writes")
            open(os.path.join(dest, name), "wb").write(raw)
            print(f"  {name} ({len(raw):,} B) sha256 ok")

        pull("gpt_primary_pre.bin", DISK, 0, 34 * 512)
        # the whole backup-GPT region (array + alt header at the last sector) -- exactly
        # the span the install overwrites, so restore_stock_gpt.py can reverse it cleanly
        # from pulled_backups/. Span is device-specific: stick 4096 (2 MiB), box 33 sectors.
        pull("gpt_backup_pre.bin", DISK,
             self.device.gpt_backup_lba, self.device.gpt_backup_span * 512)
        pull("env_pre.bin", "/dev/block/by-name/env", 0, 128 * 512)
        pull("misc_pre.bin", "/dev/block/by-name/misc", 0, 64 * 512)
        # whole partitions; "reserved" is the identity insurance (cpu_id, ethaddr)
        for part in (f"boot{ce_slot}", f"dtbo{ce_slot}", "reserved", "frp"):
            pull(f"{part}_pre.bin", f"/dev/block/by-name/{part}", 0, self._part_bytes(part))
        if open(os.path.join(dest, "gpt_primary_pre.bin"), "rb").read()[512:520] != b"EFI PART":
            sys.exit("backup sanity failed (GPT) -- aborting before writes")
        print("  all backups verified (SHA-256 vs on-device)")

    # ---- 5. re-guard + streamed writes -------------------------------------
    def reguard(self):
        # Re-confirm the SAME identified unit is still attached (model + eMMC size)
        # and still stock, immediately before the destructive writes -- guards against
        # a device swap between preflight and write.
        again = devices.identify(self.getprop,
                                 devices.sectors_reader(lambda cmd: self.su(cmd)[0]),
                                 require_layout=True)
        byname = self.su("ls /dev/block/by-name/ 2>/dev/null")[0]
        if again.slug != self.device.slug or "CE_FLASH" in byname or "CE_STORAGE" in byname:
            sys.exit("re-guard failed (device or state changed) -- aborting before writes")

    # ---- quiesce /data (the carve's live writer) ---------------------------
    def _kernel_userdata_sectors(self):
        """Sector count of userdata AS THE RUNNING KERNEL maps it (None if
        unreadable). After our GPT write the ON-DISK table is the carve, but the
        kernel keeps its boot-time table for any in-use partition -- so this
        EXCEEDING the carved size is the tell that a live old-geometry /data
        (f2fs) still spans the CE regions."""
        node, _ = self.su("readlink /dev/block/by-name/userdata")
        node = node.strip().rsplit("/", 1)[-1]
        if not node:
            return None
        out = self.su(f"cat /sys/class/block/{node}/size")[0].split()
        return int(out[0]) if out and out[0].isdigit() else None

    def quiesce_data(self, s):
        """Stop whatever can write into the carve while we stream the CE images.
        Returns True iff a quiesce was needed (old-geometry /data was live).

        The mounted /data is the PRE-carve f2fs: the kernel still maps userdata
        at its boot-time (stock) size, and f2fs is log-structured -- every write
        allocates fresh blocks at write pointers (active_logs=6 of them) spread
        across that whole stale span, i.e. INSIDE CE_FLASH/CE_STORAGE. Its
        background GC migrates blocks precisely when the box looks idle, so an
        untouched box keeps scribbling. In the field this corrupted a CE_FLASH
        that had been streamed correctly (SHA FAIL on read-back; a bad chunk
        changed hash between two reads of an idle disk).

        `stop` halts the framework -- the userspace write source. adbd and the
        Magisk su daemon are init services and survive (do NOT umount /data:
        Magisk lives under /data/adb and losing root mid-install is worse).
        background_gc=off stops f2fs's own idle-time migration; the syncs flush
        what is already dirty. The UI dies until reboot -- the install ends in
        a reboot anyway. Skipped when the kernel's userdata is already carve-
        sized (post-reformat finish_install re-run): f2fs then physically
        cannot reach the CE regions, and killing the UI would be pure cost."""
        kern = self._kernel_userdata_sectors()
        if kern is not None and kern == s["userdata"][2]:
            print("  quiesce: skipped -- kernel userdata already carve-sized "
                  "(f2fs cannot reach the CE regions)")
            return False
        why = ("size unreadable -- assuming live" if kern is None
               else f"{kern:,}s > carved {s['userdata'][2]:,}s")
        print(f"  QUIESCE /data ({why}): stop framework + f2fs GC off "
              "(UI dies until the post-install reboot)")
        self.su("stop")
        self.su("sync")
        self.su("mount -o remount,background_gc=off /data")
        dline = next((ln for ln in self.su("cat /proc/mounts")[0].splitlines()
                      if " /data " in ln), "")
        if dline and "background_gc=off" not in dline:
            print("  WARN: background_gc=off did not take on /data -- framework stop "
                  "+ the stability probe are the remaining guards")
        self.su("sync")
        self._drop_caches()
        return True

    def _assert_carve_quiet(self, s, windows=6, win_sectors=8192):
        """Abort if something is still writing inside the carve: hash `windows`
        4 MiB samples spread across CE_FLASH..CE_STORAGE twice, caches dropped in
        between, and require both passes identical. Cheap (tens of MiB, seconds)
        and read-only. A pass does NOT prove the whole span is untouched (a
        writer can land between samples) -- the post-write SHA verify remains the
        real gate. This exists to fail BEFORE ~20 minutes of streaming, and to
        catch it again on the resume path before the gate trusts a hash."""
        start, end = s["CE_FLASH"][0], s["CE_STORAGE"][1]
        span = end - start + 1 - win_sectors
        skips = [start + (span * i) // (windows - 1) for i in range(windows)]

        def sample():
            self._drop_caches()
            out, _ = self.su("; ".join(
                f"dd if={DISK} bs=512 skip={k} count={win_sectors} 2>/dev/null | sha256sum"
                for k in skips))
            return [ln.split()[0] for ln in out.strip().splitlines() if ln.strip()]

        first, second = sample(), sample()
        if len(first) != windows or first != second:
            self.die("carve region is still being written (a sampled window changed "
                     "between two reads of an idle disk) -- /data is NOT quiet, so a "
                     "CE stream would be corrupted after it lands. NOT streaming. "
                     "Investigate with installer/diag_ce_flash.py before retrying.")
        print(f"  carve quiet: {windows} sampled windows stable across two reads")

    def _already_written(self, src_sha, dd_if, skip):
        """True iff the on-eMMC region [skip*512 : +n] already hashes to the source
        blob. Reads off the eMMC (drop_caches first), so it reflects what actually
        persisted, not a cached page. Used by write_all(idempotent=True) to skip a
        region a prior (partial) run already wrote correctly."""
        want, n = src_sha
        if not want:
            return False
        self._drop_caches()
        return self._sha_device(dd_if, skip, n) == want

    def write_all(self, ce_slot, skip_gpt=False, skip_sbwipe=False, idempotent=False):
        self._no_dry("write_all")
        s = secs(self.device)
        A = self.artdir
        print("\n-- writes (GPT/kernel/dtb/env: push+dd; CE images: nc; misc: b64) --")

        def ensure(label, src_sha_fn, dd_if, skip, write_fn):
            # idempotent resume (finish_install): if the on-eMMC region already hashes
            # to the source, a prior run wrote it correctly -- skip the (re)write so a
            # resume only redoes what actually failed (e.g. rewrite CE_STORAGE, skip the
            # already-good CE_FLASH). src_sha_fn is a THUNK so a fresh install never pays
            # the source-hash cost (hashing the 10 GiB CE_STORAGE source is not free).
            if idempotent and self._already_written(src_sha_fn(), dd_if, skip):
                print(f"  SKIP {label} (eMMC already matches source)")
                return
            write_fn()

        # GPT first (push+dd, reliable): commits new geometry so a later failure
        # still lets Android reformat the shrunk userdata and boot.
        if not skip_gpt:
            ensure("GPT-primary", lambda: self._sha_file(os.path.join(A, "gpt_primary.bin")),
                   DISK, 0, lambda: self.push_dd("gpt_primary.bin", DISK, 0, "GPT-primary"))
            # From here on userdata is SHRUNK on disk while its filesystem still spans the
            # old size, and only arm_factory_reset() fixes that. Every abort from now on
            # must tell the user not to reboot into it -- see die().
            self.gpt_written = True
            ensure("GPT-backup", lambda: self._sha_file(os.path.join(A, "gpt_backup.bin")),
                   DISK, self.device.gpt_backup_lba,
                   lambda: self.push_dd("gpt_backup.bin", DISK, self.device.gpt_backup_lba, "GPT-backup"))
        self._verify_gpt()
        # All /data-staged (push+dd) + small writes go NOW, while userdata is still
        # healthy. The CE writes further down land in the carve by raw offset and can
        # disturb a live userdata fs (its SB is wiped right after), which could flip
        # /data read-only and break a later push. So kernel/dtb/misc/env first.
        ensure(f"boot{ce_slot}", lambda: self._sha_file(os.path.join(A, "boota.img")),
               f"/dev/block/by-name/boot{ce_slot}", 0,
               lambda: self.push_dd("boota.img", f"/dev/block/by-name/boot{ce_slot}", 0, f"boot{ce_slot}"))
        ensure(f"dtbo{ce_slot}", lambda: self._sha_file(os.path.join(A, "dtboa.img")),
               f"/dev/block/by-name/dtbo{ce_slot}", 0,
               lambda: self.push_dd("dtboa.img", f"/dev/block/by-name/dtbo{ce_slot}", 0, f"dtbo{ce_slot}"))
        # A/B misc: aligned 512-byte sector @ sector 4 (offset 0x800), on-device b64
        # (a seek'd nc->dd write to the small misc partition did not persist).
        ensure("A/B misc", lambda: self._sha_file(os.path.join(A, "misc_sector.bin")),
               "/dev/block/by-name/misc", 4,
               lambda: self.write_sector_b64(os.path.join(A, "misc_sector.bin"),
                                             "/dev/block/by-name/misc", 4))
        self._verify_misc(ce_slot)
        # env (push+dd; a bad env just falls back to default -> Android still boots)
        ensure("env", lambda: self._sha_file(os.path.join(A, "env_target.bin")),
               "/dev/block/by-name/env", 0,
               lambda: self.push_dd("env_target.bin", "/dev/block/by-name/env", 0, "env"))
        self._verify_env(ce_slot)
        # Blank the Amlogic proprietary partition table (MPT) so the CoreELEC kernel
        # falls back to the GPT and can see CE_FLASH/CE_STORAGE. Non-carve, idempotent.
        self.wipe_mpt()
        self._verify_mpt()
        # CoreELEC filesystems LAST: MUST nc-stream -- they land in the carve by raw
        # offset, so staging them on userdata would be the read-during-overwrite
        # brick race; too big to push anyway. After this, only the SB wipe + sync.
        # But FIRST kill the carve's live writer: until the reformat reboot, the
        # mounted f2fs still spans these LBAs and will overwrite a landed stream
        # (quiesce also protects the idempotent gate's 10 GiB hash reads below).
        quiesced = self.quiesce_data(s)
        self._assert_carve_quiet(s)
        ensure("CE_FLASH", lambda: self._sha_image_raw("ce_flash.img"), DISK, s["CE_FLASH"][0],
               lambda: self.write_offset("ce_flash.img", s["CE_FLASH"][0], "CE_FLASH"))
        ensure("CE_STORAGE", lambda: self._sha_image_raw("ce_storage.img"), DISK, s["CE_STORAGE"][0],
               lambda: self.write_offset("ce_storage.img", s["CE_STORAGE"][0], "CE_STORAGE"))
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

            def wipe_sb():
                rc = self.su(f"dd if=/dev/zero of={DISK} bs=512 seek={s['userdata'][0]} "
                             f"count=8192 conv=fsync")[1]
                return (rc == 0), (None if rc == 0 else "userdata wipe failed")

            self._write_retry(wipe_sb, "userdata SB wipe")
            self.su("sync")
        return quiesced

    # ---- verification ------------------------------------------------------
    def _drop_caches(self):
        """Force subsequent reads to hit the eMMC, not boot-time cached pages."""
        self.su(f"sync; echo 3 > /proc/sys/vm/drop_caches; blockdev --flushbufs {DISK}")

    def _verify_gpt(self):
        self._drop_caches()
        gpt = self.su_bytes(f"dd if={DISK} bs=512 count=2 2>/dev/null")
        num = struct.unpack_from("<I", gpt, 512 + 80)[0]
        if num != devices.CARVED_NUM_ENTRIES:
            self.die(f"GPT verify failed: entries={num} (expected "
                     f"{devices.CARVED_NUM_ENTRIES})")
        print(f"  verify GPT: {num} entries OK")

    def _verify_misc(self, ce_slot):
        self._drop_caches()
        m = self.su_bytes("dd if=/dev/block/by-name/misc bs=1 skip=2048 count=32 2>/dev/null")[:32]
        info = ab_misc.parse(m)
        byte = info["a_byte"] if ce_slot == "_a" else info["b_byte"]
        if not info["crc_ok"] or byte != 0:
            self.die(f"misc verify failed (crc_ok={info['crc_ok']} byte=0x{byte:02x})")
        print(f"  verify misc: {ce_slot} unbootable, crc OK")

    def _verify_env(self, ce_slot):
        env = self.su_bytes("dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null")[:envtool.ENV_SIZE]
        if not envtool.crc_ok(env)[0]:
            self.die("env verify failed: CRC invalid")
        d = envtool.parse(env)
        if d.get("boot_ce") != "0" or f"imgread kernel boot{ce_slot}" not in d.get("bootcefromemmc", ""):
            self.die("env verify failed: gate/boot_ce wrong")
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
        def attempt():
            out, rc = self.su("dd if=/dev/zero of=/dev/block/by-name/reserved bs=512 count=8 conv=fsync 2>&1")
            return (rc == 0), (None if rc == 0 else f"MPT wipe failed: {out.strip()}")

        self._write_retry(attempt, "MPT wipe")
        print("  WIPE Amlogic MPT (reserved[0:0x1000]) -> kernel falls back to GPT (CE_FLASH visible)")

    def _verify_mpt(self):
        self._drop_caches()
        magic = self.su_bytes("dd if=/dev/block/by-name/reserved bs=4 count=1 2>/dev/null")[:4]
        if magic == b"MPT\x00":
            self.die("MPT verify failed: 'MPT' magic still present in reserved -- "
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
        """sha256 + length of the image AS FLASHED (decompressing the .gz on the fly when
        that is the form we streamed), so it matches what landed on disk. Memoized in
        _SRC_SHA by payload path -- check_ce_image_sizes, the idempotent resume gate and
        verify_writes all want the same number, and expanding 10 GiB is not free.

        Resolves through _img_payload deliberately: this used to prefer the RAW while
        write_offset() preferred the GZ, so a stale gz next to a fresh raw wrote the OLD
        image and verified it against the NEW hash."""
        path, gz = self._img_payload(basename)
        if path in _SRC_SHA:
            return _SRC_SHA[path]
        if gz:
            h = hashlib.sha256(); n = 0
            with gzip.open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk); n += len(chunk)
            res = (h.hexdigest(), n)
        else:
            res = self._sha_file(path)
        _SRC_SHA[path] = res
        return res

    def check_ce_image_sizes(self):
        """Refuse a CE image that does not fit its carved partition -- BEFORE any write.

        write_offset streams into `dd of=/dev/block/mmcblk0 seek=<start>` with no count,
        because a count= on the device side is not safe here: busybox dd counts a short
        read from the gzip pipe as a whole block, so bounding it there would silently
        TRUNCATE a perfectly good image. The bound therefore has to be checked on the PC,
        against the length the image actually decompresses to. Without it an oversized
        ce_storage.img runs straight off the end of CE_STORAGE -- which on the stick is
        the sector immediately before the backup GPT.

        The hashes computed here are cached, so verify_writes reuses them and this costs
        no extra CPU over the run; it only moves the work to before the first write, which
        is the entire point of a preflight."""
        print("\n-- CE image size gate (must fit the carve; hashes cached for verify) --")
        s = secs(self.device)
        for basename, part in (("ce_flash.img", "CE_FLASH"), ("ce_storage.img", "CE_STORAGE")):
            path, _ = self._img_payload(basename)
            print(f"  {os.path.basename(path)}: expanding to hash+size ...", end="", flush=True)
            sha, n = self._sha_image_raw(basename)
            cap = s[part][2] * devices.SECTOR          # partition sector count -> bytes
            print(f" {n:,} B  sha256={sha[:16]}")
            if n > cap:
                sys.exit(f"\n{basename} decompresses to {n:,} B but {part} is only "
                         f"{cap:,} B on the {self.device.name}.\n"
                         f"Streaming it would write {n - cap:,} B PAST the end of {part}, "
                         f"over whatever follows it on the eMMC. REFUSING -- nothing has "
                         f"been written.\nRebuild the artifacts for this unit: "
                         f"python build/build_all.py --device {self.device.slug}")
            if n < cap:
                # Legal (the filesystem simply does not span the whole partition) but it
                # is never what a correct build produces -- both CE images are sized from
                # devices.py -- so say it out loud rather than let it pass in silence.
                print(f"    NOTE: {n:,} B image in a {cap:,} B partition "
                      f"({cap - n:,} B of the partition left untouched)")

    def _sha_device(self, dd_if, skip_sectors, nbytes):
        """SHA-256 exactly nbytes starting at skip_sectors*512 of dd_if, hashed on-device.

        Whole 512-byte sectors are read straight with bs=512; only a sub-sector tail
        (nbytes % 512, always < 512) is trimmed with `head -c`. The old code did
        `dd ... | head -c {nbytes}`, but busybox `head -c` takes a 32-bit byte count,
        so a region >= 4 GiB (the box's 10 GiB CE_STORAGE) overflowed it: head errored
        ("head: ..."), sha256sum hashed nothing, and the DEV hash came back as the error
        string -- a SPURIOUS verify FAIL, and (via _already_written) an idempotent gate
        that could never see an already-good CE_STORAGE, so it re-streamed every run.
        Never hand head a value >= 512 now; the stick's <2 GiB carve never hit this."""
        full, rem = divmod(nbytes, 512)          # whole sectors + sub-sector tail (0..511)
        read = f"dd if={dd_if} bs=512 skip={skip_sectors} count={full} 2>/dev/null"
        if rem:
            read += (f"; dd if={dd_if} bs=512 skip={skip_sectors + full} count=1 2>/dev/null "
                     f"| head -c {rem}")
        out, _ = self.su(f"{{ {read}; }} | sha256sum")
        out = out.strip()
        return out.split()[0] if out else ""

    def verify_writes(self, ce_slot):
        """SHA-256 every written region off the eMMC vs the PC-side source."""
        print("\n-- SHA-256 read-back verification (off eMMC) --")
        self._drop_caches()
        s = secs(self.device)
        A = self.artdir
        # (label, local-hash fn, device dd source, skip in 512-sectors)
        plan = [
            ("GPT-primary", self._sha_file(os.path.join(A, "gpt_primary.bin")), DISK, 0),
            ("GPT-backup",  self._sha_file(os.path.join(A, "gpt_backup.bin")),  DISK, self.device.gpt_backup_lba),
            ("CE_FLASH",    self._sha_image_raw("ce_flash.img"),                DISK, s["CE_FLASH"][0]),
            ("CE_STORAGE",  self._sha_image_raw("ce_storage.img"),              DISK, s["CE_STORAGE"][0]),
            ("kernel",      self._sha_file(os.path.join(A, "boota.img")),       f"/dev/block/by-name/boot{ce_slot}", 0),
            ("dtb",         self._sha_file(os.path.join(A, "dtboa.img")),       f"/dev/block/by-name/dtbo{ce_slot}", 0),
            ("env",         self._sha_file(os.path.join(A, "env_target.bin")),  "/dev/block/by-name/env", 0),
            ("A/B misc",    self._sha_file(os.path.join(A, "misc_sector.bin")), "/dev/block/by-name/misc", 4),  # sector 4 == 0x800
        ]
        allok = True
        for label, (lh, n), dd_if, skip in plan:
            dh = self._sha_device(dd_if, skip, n)
            ok = (lh == dh and dh != "")
            allok = allok and ok
            tail = "" if ok else f"  PC={lh[:16]} DEV={dh[:16] or '(empty)'}"
            print(f"  {'OK  ' if ok else 'FAIL'} {label:<11} {n:>12,} B  {dh[:16]}{tail}")
        if not allok:
            self.die("SHA-256 verification FAILED -- a written region does not match its "
                     "source. Do NOT trust this install.")
        print("  all regions byte-identical to source (SHA-256).")

    # ---- 6. OTA ------------------------------------------------------------
    def disable_ota(self):
        print("\n-- disable vendor OTA (transient) --")
        out, _ = self.su("pm disable-user --user 0 com.xiaomi.mitv.updateservice")
        print("  " + (out.strip() or "(no output)"))
        print("  NOTE: this disable is erased by the first-boot userdata reformat.")
        print("  For DURABLE blocking, after first boot run:")
        print("    python install_blockota.py --serial <serial>   (installs the Block-OTA Magisk module)")

    # ---- 7. arm the userdata reformat (deterministic) ----------------------
    def arm_factory_reset(self):
        """Schedule a recovery-driven userdata reformat on the next boot via the BCB.

        Why not just the superblock wipe: a clean `adb reboot` unmounts /data and
        flushes the cached ORIGINAL superblock back over our zeros, so no reformat
        fires -- the f2fs stays at the OLD full-userdata size on the now-smaller
        partition (df shows the old full carve size on the smaller userdata; it
        mounts but will I/O-error once usage crosses the partition end). Deterministic fix: set the
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
        path = os.path.join(self.artdir, "_bcb_wipe.bin")
        open(path, "wb").write(bytes(sec))
        self.write_sector_b64(path, "/dev/block/by-name/misc", 0)
        chk = self.su_bytes("dd if=/dev/block/by-name/misc bs=512 count=1 2>/dev/null")[:13]
        if chk != b"boot-recovery":
            self.die("BCB arm failed: 'boot-recovery' not read back -- userdata will NOT "
                     "reformat on the next boot")
        print("  BCB set: next reboot -> recovery reformats userdata to the new size, then Android")


if __name__ == "__main__":
    main()
