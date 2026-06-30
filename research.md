# CoreELEC internal dual-boot — full research & portability notes

Everything learned building an **internal-eMMC CoreELEC ⇄ Android dual-boot** for the
**Xiaomi TV Stick 4K 2nd Gen** (`twilight`, Amlogic **s7d / S905X5M**), written so the work can be
carried to other Amlogic devices (e.g. **Ugoos AM9 Pro** S905X5/S6, **Xiaomi TV Box** variants).

This document has three parts:
1. **The mechanism** — how the dual-boot actually works, and the key insight that made it possible.
2. **Component deep-dive** — every piece, the hard-won findings, and the bug fixes behind each.
3. **Portability** — what is reusable as-is, what must change per device, and the risks.

For the user-facing install steps see [`README.md`](README.md). The original chronological build
log is [`CoreELEC-internal-dualboot-twilight.md`](CoreELEC-internal-dualboot-twilight.md). The
related **Ugoos AM9 Pro** single-boot installer + Amlogic USB-burn tooling lives in
[`ugoos-am9-pro-coreelec-emmc/`](ugoos-am9-pro-coreelec-emmc/README.md).

---

## 1. The mechanism

### 1.1 The problem

The normal way CoreELEC boots from internal eMMC (`ceemmc` / the `cfgload` method) relies on
u-boot scanning eMMC partitions for a FAT filesystem containing a `cfgload` script. **On the
Xiaomi stick's s7d u-boot this does not work**, because:

> **u-boot addresses eMMC partitions by NAME from its own (encrypted, non-editable) Amlogic
> partition table. It cannot see GPT partitions we add.**

So a freshly-carved `CE_FLASH` GPT partition is **invisible to u-boot** — `cfgload`-scan boot
fails, which is exactly why `ceemmc` lists s7d internal install as "in development."

### 1.2 The key insight

u-boot **can** read the **existing named partitions** (`boot_a/b`, `dtbo_a/b`) via `imgread` /
`store read`. The device is A/B; only one slot is active (Android's). So:

> **Put the CoreELEC kernel into the *inactive* `boot_<slot>` and the CoreELEC dtb into the
> *inactive* `dtbo_<slot>`** — partitions u-boot already knows by name — **and gate which one
> boots with a u-boot env flag.** Linux (which reads the full GPT fine) then mounts CoreELEC's
> `SYSTEM` / storage from the GPT-added `CE_FLASH` / `CE_STORAGE` by LABEL.

So the partition *number* of CE_FLASH/CE_STORAGE is irrelevant — they are found by LABEL from
Linux; only `boot_<slot>`/`dtbo_<slot>` must be partitions u-boot can name.

### 1.3 The boot chain

```
power on
  └─ u-boot runs `bootcmd`:
       run bootfromsd ; run bootfromusb              ← SD/USB recovery still works
       if boot_ce == 1:  run bootcefromemmc          ← our gate
       run storeboot                                 ← Android (default)

  bootcefromemmc:
       setenv bootargs ... boot=LABEL=CE_FLASH disk=LABEL=CE_STORAGE
       store  read <dtb_addr> dtbo_<slot>            ← CoreELEC dtb (we wrote it here)
       imgread kernel boot_<slot> <load_addr>        ← CoreELEC kernel (we wrote it here)
       bootm <load_addr>                             ← boot CoreELEC
            └─ Linux mounts SYSTEM from CE_FLASH, /storage from CE_STORAGE (by LABEL, via GPT)
```

`boot_ce` is a one-shot flag (the gate clears it + `saveenv` before booting CE, so a CE crash
falls back to Android next boot). The Android-side **"Reboot to CoreELEC"** app sets `boot_ce=1`
by writing the env partition directly (no `fw_setenv` needed).

**Default direction is configurable.** For *CoreELEC-default*, `bootcmd` runs `bootcefromemmc`
unconditionally and routes Android via `bootfromnand` — see §2.10. A bad CoreELEC boot always
falls through to `storeboot` (Android), so the gate is fail-safe.

### 1.4 Why this is different from the Ugoos AM9 Pro approach

The Ugoos AM9 Pro (`ugoos-am9-pro-coreelec-emmc/`) does a **single-boot** CoreELEC-on-eMMC using
the *standard* `cfgload` mechanism — because **its u-boot's `cfgloademmc` CAN scan all eMMC
partitions by content** and finds `CE_FLASH` at p28. It then patches `cfgload` to
`disk=LABEL=CE_STORAGE` and installs `/flash/mount-storage.sh` + `nofsck` as the durable rescue
layer (CoreELEC's updater overwrites `cfgload` each update).

So the **first question on any new device is: can u-boot see/boot a GPT-added FAT partition?**
- **Yes** (Ugoos-class) → use the `cfgload` + `mount-storage.sh` method (single-boot), or add an
  env/cfgload gate for dual-boot.
- **No** (Xiaomi s7d-class) → you must use the **named-partition injection** method described here.

---

## 2. Component deep-dive

### 2.1 Device facts (verified, twilight)
- `ro.product.device=twilight`, `model=MiTV-AFMU1`, `ro.board.platform=s7d`, Android 14.
- eMMC `mmcblk0` = 7.28 GiB (15,269,888 × 512 B). **GPT** with header declaring only **32 entry
  slots** (the reserved entry array is the spec-min 16 KiB = room for 128).
- Bootloader **unlocked** (`verifiedbootstate=orange`, AVB effectively off), rooted (Magisk).
- `userdata` is **FBE-encrypted** (`ro.crypto.type=file`) — CoreELEC cannot read Android user data.
- CoreELEC build: **CE-22 (Piers nightly)**, kernel 5.15.196.
- **Model-correct dtb = `s7d_s905x5m_xiaomi_3rd_gen`** (the generic `s7d_s905x5m_2g` boots but
  misconfigures the GPU and throws many dmesg errors).

### 2.2 Stock partition map (the only free space)
The `userdata` span (sectors 6,713,344 → 15,265,791 = **3278–7454 MiB, 4176 MiB total**) is the
only carve region. Do-not-touch: `reserved` p1 (identity), `env` p2, `bootloader_a/b` p7/8,
`tee` p9, `boot0`/`boot1` (HW write-protected), `super` (Android system), active `boot_<slot>`/
`dtbo_<slot>`.

### 2.3 GPT 32 → 128 entry expansion (non-destructive)
The header declared 32 entries but the array reserves the spec-min 16 KiB (= 128 entries), so the
table can be expanded **without moving `first_usable_lba`** and without disturbing any existing
partition. Build a 128-entry GPT from the backup (keep all 32 originals byte-identical, define the
3 carve partitions, recompute primary+backup entry-array CRC32 and both header CRC32s), then `dd`
the primary (first 34 sectors) + backup (tail) to `mmcblk0`. `build/layout.py` is the single
source of offsets; the editor is in the build step.

### 2.3b The Amlogic MPT override — why Linux must be *forced* to read the GPT
§1.2 assumes "Linux reads the full GPT fine." That is only true when **no valid Amlogic MPT is
present.** The Amlogic vendor kernel (which CoreELEC runs) keeps a **proprietary partition table
("MPT")** at the start of the `reserved` partition (eMMC offset **36 MiB = 0x2400000**). When a
valid MPT exists, the kernel **uses it and ignores the GPT** — dmesg: `mmcblk0: skip mounting disk
with MPT partition`. The MPT is hard-capped at **`MAX_MMC_PART_NUM = 32`** entries (table size
0x518) and lists only the stock Android partitions, so our GPT-added **`CE_FLASH` is invisible to
the kernel** → no `mmcblk0p33` node → `boot=LABEL=CE_FLASH` can't mount → **CoreELEC hangs on the
MI boot logo** (no framebuffer hand-off). u-boot can still `imgread` the CE kernel from `boot_<slot>`
(the gate fires), so the symptom is a logo hang *after* the gate, not a fall-through to Android.

Format: `"MPT\0"` + version `01.00.00` + `part_num`(u32@0x10) + `checksum`(u32@0x14) + `part_num`×40 B
entries (`char name[16]; u64 size; u64 offset; u32 mask_flags; pad`). The checksum is an Amlogic **bug**
— `part_num × wordsum(entry[0])` only (the calc loop never advances past entry 0), so it validates the
count and partition #0 but nothing else.

**Why some units boot internally and others don't (identical install):** a unit with a **blank** MPT
region falls back to the GPT scan (all 34 partitions) and boots; a unit with a **populated** MPT does
not. An applied **A/B OTA re-populates the MPT** to factory-stock-32 (the same OTA also clobbers the
inactive `boot_<slot>`/`dtbo_<slot>` — see §2.11), which is exactly how a previously-working stick
regressed to a logo hang. Confirmed by comparing a working twin (byte-identical GPT, same gate):
its `reserved[0:0x4000]` was **all zeros (no MPT)**, `AMLNORMAL` at 0x4000; the broken unit had a
valid 32-entry MPT there.

**Fix (`flash_to_coreelec.py::wipe_mpt`, runs in the flash step):** zero `reserved[0:0x1000]` (the MPT
struct is 0x518; `AMLNORMAL`@0x4000 and all identity further in `reserved` are untouched) so the kernel
falls back to the GPT and sees `CE_FLASH`. Idempotent (no-op if no MPT magic); `reserved_pre.bin` backs
up the original. *Not* a kernel patch: the override is correct-by-design Amlogic behavior (the BootROM
loads from 0x200 where a GPT header would sit, so Amlogic uses its own table), and blanking the MPT is
the platform-correct way to make Linux use the GPT. Recurrence is prevented at the source by `blockgms`/
`blockota` (§2.11); the installer wipe + the pre-write `reserved` backup make a fresh install correct
and reversible.

### 2.4 Partition carve + size rationale
Final layout (sums to 4176): **userdata 2376 / CE_FLASH 600 / CE_STORAGE 1200 MiB**.
- `CE_FLASH` (FAT32) holds SYSTEM (~347 M) + kernel + recovery + dtb + dovi (~381 M used).
- `CE_STORAGE` (ext4, built with `-m 0` no root-reserve) holds CoreELEC `/storage`.
- **Why 1200 for CE_STORAGE:** a CoreELEC OS update downloads the ~397 M `.tar` to `/storage` then
  extracts the *whole* tar to `/storage/.update/.tmp/` **while the tar still exists** → a
  **~770–790 M transient peak**. 650 M was too small (the original bug that drove the resize).
  Verified by extracting `/init` from the CE `kernel.img` (zstd ramdisk) with
  `ce_inspect_initramfs.py`: `update_file()` does `rm -f /flash/SYSTEM` then `dd` in place
  (1×SYSTEM, not 2×), so CE_FLASH need not be huge.

#### Scaling to larger eMMC (16 GB+ devices)
The twilight sizes (600 / 1200) are squeezed by its tiny **4176 MiB** carve (it only has ~7.28 GiB
eMMC and a small stock `userdata`). On a device with **16 GB+ eMMC** there is plenty of free space,
so the CoreELEC partitions should be enlarged — a 1200 MiB `/storage` is uncomfortable for a daily
driver. Recommended sizing there:

| Partition | twilight (8 GB) | **16 GB+ recommended** | Why |
|---|---|---|---|
| `CE_FLASH` (FAT32) | 600 MiB | **1024 MiB (1 GiB)** | `SYSTEM` ~347 M + kernel/recovery/dtb/dovi ≈ 381 M used today; 1 GiB lets `SYSTEM` ~double across CE major releases — set-and-forget. |
| `CE_STORAGE` (ext4) | 1200 MiB | **2 GiB floor, 4 GiB recommended** | CE-update transient peak ~790 M + addons/skin (a Plex/PM4K build ≈ 200–400 M) + texture/thumbnail cache (0.5–2 GB for a real library) + logs/backups. |

Rules of thumb:
- **CE_FLASH never needs to be large** — it's `SYSTEM` + boot files, replaced in place on update
  (1×, not 2×). 1 GiB is generous; even 512 MiB works (the Ugoos single-boot installer uses 512).
- **CE_STORAGE is where you spend the surplus.** It absorbs the update peak, all Kodi data, and the
  texture cache, which grows with library size. 2 GiB is the practical minimum for a usable daily
  driver; 4 GiB is comfortable; on devices with abundant free eMMC (e.g. the Ugoos AM9P's ~54 GB
  userdata) give CE_STORAGE the bulk of the surplus — its installer allocates ~53.9 GB.
- **How to change it:** `build/layout.py` `SIZES_MIB` is the single knob — the three sizes must sum
  to that device's carve region (`CARVE_TOTAL_MIB`). On a bigger device, re-derive the carve from
  its own stock GPT (the `userdata` span), then split it `userdata` (Android's share) + `CE_FLASH`
  1024 + `CE_STORAGE` (everything left). The CE images are rebuilt from those sizes
  (`build_ce_flash.sh` / `build_ce_storage.sh` read `layout.py`).

### 2.5 The u-boot env (codec, identity, CRC) — `build/envtool.py`
- Live env = `mmcblk0p2` (by-name `env`), **offset 0, size 0x10000, NON-redundant**, format
  `[crc32 LE][key=val\0 …\0\0][0x00 pad]`, crc32 over bytes `[4:0x10000]`.
- The factory p2 is a minimal stub; the full env materializes on first `saveenv`.
- **The env carries per-device IDENTITY** (serial, assm_sn, did_key = MAC+key, cpu_id, ethaddr).
  So the installer edits **each target's own env** (adds only the boot helpers + gate); it never
  ships a foreign env. `keyman` repopulates identity each boot, so even a stripped env recovers.
- A bad env write is **not** a brick: u-boot falls back to its built-in default → boots Android.
- The Android-side env editor and the CoreELEC Toolbox's `envcodec.py` were both verified
  byte-identical to `envtool.serialize`.

### 2.6 A/B `misc` "mark CE slot unbootable"
No `bootctl` on device, so the AOSP 32-byte `bootloader_control` struct in `misc` @ offset `0x800`
(`BCAB` magic, slot priority bytes, crc32 @ `0x81c`) is edited directly: set the **inactive (CE)**
slot priority byte to `0x00` and recompute crc32. This stops Android's A/B logic from
auto-booting our CE kernel. Our gate uses raw `imgread boot_<slot>` which ignores the flag, so
CoreELEC still boots.

### 2.7 Write methods (the brick-risk redesign) — `installer/flash_to_coreelec.py`
Android lacks `fw_setenv`, `mkfs.vfat`, `parted`; on a live system `super`/`userdata` are mounted
so the partition table can't be re-read — hence **raw whole-disk offset writes to `mmcblk0`**.
The transport matters:
- **push + dd** (`adb push` → on-device `dd conv=fsync`) for **GPT, kernel(boot_<slot>),
  dtb(dtbo_<slot>), env** — all *outside* the carve, so `/data` staging can't overlap them.
  Reliable; the standard partition-flash method.
- **`base64 -d | dd`** on-device for **misc** (512 B aligned sector @ `0x800`, read-modify-write).
- **nc-over-adb-forward stream** for the **two big CE images only** (they land in the carve by raw
  offset and must *not* stage on userdata — that would be a read-during-overwrite brick race).
- `write_all` is ordered: all `/data`-staged + small writes first (while userdata is healthy), CE
  images + the userdata superblock-wipe last. Every region is SHA-256 read-back verified
  (`drop_caches` before each read to defeat stale page cache).

Findings that forced this design (all real first-run bugs):
- nc reused one port → busybox nc has no `SO_REUSEADDR` → 2nd sequential write failed. → fresh
  port per write.
- `adb forward` accepts at the PC end before the device `nc` binds → bytes dropped → dd hangs. →
  poll `/proc/net/tcp{,6}` for LISTEN before sending.
- tiny nc transfers (17 K GPT, 512 B misc) silently didn't persist → switched those to push+dd /
  base64.
- region reads served stale page cache → `drop_caches` + `blockdev --flushbufs` before verify.
- `adb exec-out` does **not** forward stdin (hangs/0 bytes); `adb shell` mangles binary via pty.

### 2.8 CoreELEC dtb selection (CRITICAL, device-specific)
The dtb must match the SoC **and** the CoreELEC build. `dtboa.img` = the CoreELEC `dtb.img`
(`xiaomi_3rd_gen` FDT) zero-padded to the partition size and written to the inactive `dtbo_<slot>`.
Wrong dtb = boots with wrong GPU/clocks or not at all. `HW H.265 decode + Dolby-Vision FEL` were
verified working (vdec IRQs increment, hevcf clock active) — the `Get pwrc-vdec-2/...failed` dmesg
lines are benign.

### 2.9 CoreELEC OS update self-heal — `payload/flash/user-update.sh` (v3)
A CE update breaks the dual-boot in **two distinct ways**, both fixed by a hook CoreELEC runs in
its initramfs (`sh /flash/user-update.sh`, busybox-only — **no Python, no fw_setenv** there):
1. **Kernel/dtb go stale:** the update rewrites `/flash/kernel.img`+`dtb.img` but u-boot boots from
   `boot_<slot>`/`dtbo_<slot>`. The hook `dd`s them back. (v1 failed to find the slot because the
   minimal initramfs has no `fw_printenv`; v2 reads the gate straight from the env partition bytes:
   `dd env | tr '\0' '\n' | grep 'imgread kernel boot_'`.)
2. **Gate stripped:** the update resets `bootcmd` to stock (drops the `boot_ce` gate). Since
   `fw_setenv` isn't available in the initramfs, the hook `dd`s a precomputed **`/flash/env_dualboot.bin`**
   (a full gated env with `boot_ce=1`, valid CRC; identity repopulated by keyman) back over the env
   partition → gate restored + auto-enters the freshly-updated CE.

`installer/deploy_flash_recovery.py` writes `env_dualboot.bin` + `ce_slot.conf` + the hook to
`/flash` (post-first-boot, when CE_FLASH is mountable). **Confirmed on hardware**: a real CE update
self-healed with no manual step.

### 2.10 Boot default (Android or CoreELEC) — `envtool.gate_vars(ce_slot, default)`
- *android-default* `bootcmd`: `... if boot_ce==1 { clear; run bootcefromemmc } ; run storeboot`.
- *coreelec-default* `bootcmd`: `if bootfromnand==1 { clear; storeboot(Android) } else {
  bootfromsd/usb; run bootcefromemmc }; run storeboot`. So a normal reboot = CoreELEC; a CE crash
  falls through to Android (safe).
- **Android trigger reuses CoreELEC's existing "reboot to eMMC/nand"** = `/usr/sbin/rebootfromnand`
  which just does `fw_setenv bootfromnand 1` — our `bootcmd` routes that to Android. No custom
  script on the CE side. `env_dualboot.bin` is built with the chosen default so CE updates keep it.

### 2.11 OTA blocking
- **Google TV system OTA is GMS, not the Xiaomi app.** Settings → "Check for updates" resolves to
  `com.google.android.gms` (`.update.SystemUpdatePanoActivity` → `update_engine`). GMS can't be
  pm-disabled (breaks Play/accounts/cast), so disable only the **`.update.SystemUpdate*`
  components** via `pm disable <pkg>/<component>` (persistent in `package-restrictions.xml`,
  survives reboots). → **`blockgms`** module (`app/blockgms` + `install_blockgms.py`). The threat is
  real: `ro.product.ab_ota_partitions` includes `boot`+`dtbo`, so an applied A/B OTA writes the
  inactive slot (= our CE slot) and flips active → clobbers CoreELEC.
- **Xiaomi updater** (`com.xiaomi.mitv.updateservice`) → optional **`blockota`** module.
- **Finding:** the *persistent* `pm disable-user` is the durable mechanism. A Magisk boot-time
  `service.sh` re-assert **fails** on this Amlogic TV box (`pm`/`settings`/`cmd` from the Magisk
  boot context return `Failed transaction` even after `boot_completed`) — but it's redundant, the
  persistent disable holds across reboots.
- **Caveat (web-confirmed):** disabling system-app components can bootloop if GMS later does a Play
  system update → Magisk safe-mode (3 failed boots) or `--revert` recovers.

### 2.12 The "Reboot to CoreELEC" app
Android app that flips `boot_ce` by writing the env partition directly (root; no `fw_setenv`). Its
env codec is byte-identical to `envtool`. Single button; built debug-signed.

### 2.13 CoreELEC Toolbox addon (`script.coreelec.toolbox`, generic)
A Kodi `WindowXMLDialog` addon with three features, all generic to any CoreELEC box:
- **Sync Bluetooth remotes from Android** — see §2.14.
- **Set default boot OS** — edits the env gate + refreshes `env_dualboot.bin` (so the choice
  survives CE updates). Reuses `envcodec.py` (verified == `envtool`).
- **Fix WiFi MAC** — writes `MacOverride`/`MacAddr` into `wifi.cfg`. Needed only on devices that
  ship a wrong/shared default MAC; on most boxes the chip's efuse MAC is correct.

### 2.14 Bluetooth remote sync (FBE-encrypted Android → CoreELEC)
Android `userdata` is FBE-encrypted, so CoreELEC can't read the BT pairings directly. Solution:
- **Android side** (`toolbox_export` Magisk module): each boot, copy the *decrypted*
  `/data/misc/bluedroid/bt_config.conf` + WiFi/BT MAC to `/flash` (plain FAT32, shared with CE).
- **CoreELEC side** (addon `bt_sync.py`): parse `bt_config.conf`, keep only HID input remotes
  (service `00001812` or HID descriptor — skip earbuds), convert the BLE keys (LE_KEY_PENC →
  LTK/Rand/EDIV, LE_KEY_PID → IRK) to a BlueZ `info` file under
  `/storage/.cache/bluetooth/<adapter>/<mac>/`, then `systemctl restart bluetooth`.
- The CE and Android **adapter MACs match** (same chip efuse), so no adapter-MAC override is needed
  on this device.

### 2.15 Remote keymap (two-layer remap) — `payload/remote/`
The remote's special buttons emit HID Consumer-page scancodes the kernel maps to keycodes Kodi
can't bind (Netflix/Voice arrive as identical `<unicode>` 0xfffe). Two layers:
1. **`99-xiaomi-remote.hwdb`** (udev hwdb, matches `evdev:input:b0005v2717p32B9*`) remaps the
   scancodes to keys Kodi *does* name: OK `0xc0041`→ENTER, Netflix `0xc008e`→RED, Voice
   `0xc00cf`→GREEN, PrimeVideo `0xc00b0`→YELLOW. `systemd-hwdb update` re-applies on every device
   add → survives the remote reconnecting.
2. **`xiaomi.xml`** Kodi keymap binds RED→`RunAddon(script.plexmod)`, GREEN/YELLOW→
   `RunAddon(script.tinyppi)`; OK→ENTER is Select by default.

Both files live in `/storage` → survive CE updates. Capture trick: input devices are grabbed by
libinput, so `systemctl stop kodi` to read raw evdev; `MSC_SCAN` (type 4 code 4) gives the HID
scancode before each `EV_KEY`; Kodi key-names came from debug-log `HandleKey:` lines.

### 2.16 Kodi download sources — `deploy_kodi_sources.py`
Adds `PM4K` (`https://pm4k.eu/`) and `jamal2362` (`https://ce-repo.github.io/repository.jamal2362/`)
to `sources.xml` under `<files>`. **Kodi has no live "add source" API** and rewrites `sources.xml`
from memory on shutdown, so the script must **stop kodi → edit → start kodi** (web-confirmed as the
only safe method).

### 2.17 Installer software architecture
`installer/install.py` orchestrates two contexts — **Android** (`--serial`, adb: stage0/1/2/2a,
verify) and **CoreELEC** (`--host`, ssh: stage3). Each stage is standalone + idempotent. The whole
chain was validated piecemeal on the working unit (incl. a full destructive reinstall with all 8
regions SHA-verified, plus a CE-update self-heal). `make_dist.py` produces a self-contained
~370 MiB bundle (images gzipped) needing only Python 3 + adb (+ paramiko for stage 3).

---

## 3. Portability

### 3.1 The decision tree for a new device
1. **Can u-boot boot a GPT-added FAT partition (`cfgloademmc` scan)?**
   - **Yes** → use the simpler `cfgload` + `mount-storage.sh` method (Ugoos pattern). For
     dual-boot, add a gate (env flag or conditional `cfgload`).
   - **No** → use the **named-partition injection** method (this project): CE kernel→inactive
     `boot_<slot>`, CE dtb→inactive `dtbo_<slot>`, env `boot_ce` gate.
2. **Is the device A/B?** If not, there's no inactive `boot`/`dtbo` to borrow → you need a
   different home for the CE kernel/dtb (a dedicated partition u-boot can name, or the cfgload path).
3. **Get the correct CoreELEC dtb for the exact SoC + build.** This is non-negotiable and unique
   per device.
4. **Confirm the env format** (size, redundant vs non-redundant, CRC range) before writing it.

### 3.2 Reuse matrix

| Component | Reusable as-is? | What changes per device |
|---|---|---|
| GPT 32→128 expansion (`layout.py` + editor) | Logic ✓ | Offsets/sizes; skip if device already has 128 entries |
| Carve from userdata | Concept ✓ | Offsets + sizes (Ugoos has ~54 GB → huge CE_STORAGE) |
| **Named-partition injection** (kernel→boot_X, dtb→dtbo_X) | Only on u-boot-can't-scan + A/B devices | Slot resolution; **skip entirely** on cfgload-capable boards |
| env codec (`envtool.py`) | Amlogic u-boot ✓ | Verify size/CRC/redundant-vs-not per device |
| env gate (`boot_ce` + bootcmd) | Concept ✓ | `bootcefromemmc` slot refs; the exact bootcmd strings |
| A/B `misc` unbootable | A/B devices ✓ | Skip on non-A/B devices |
| Write methods (push+dd / nc / base64) | ✓ generic adb | none |
| **CoreELEC dtb** | ✗ | **Device + build specific — always replace** |
| Dolby-Vision `dovi.ko` (felfix) | ✓ | Unlocked kernel module works on most kernels |
| CE-update hook (`user-update.sh`) | Concept ✓ | Resync targets (boot_X/dtbo_X here; cfgload/mount-storage on Ugoos-class) |
| **`blockgms`** (GMS OTA block) | ✓ **generic Google TV** | none |
| `blockota` (Xiaomi updater) | ✗ | Vendor updater package name |
| Reboot-to-CoreELEC app | ✓ (with env-gate) | Tied to the env mechanism |
| **CoreELEC Toolbox addon** | ✓ **generic CoreELEC** | none (features self-detect) |
| `toolbox_export` module | ✓ where CE_FLASH + FBE exist | none |
| BT remote sync | Concept ✓ | Adapter-MAC override if CE/Android MACs differ |
| **Kodi sources** | ✓ **generic** | none |
| Remote keymap (hwdb + Kodi keymap) | Method ✓ | Remote vendor/product + scancodes + addon IDs |

**Bottom line:** the **CoreELEC-side extras** (Toolbox addon, Kodi sources, GMS OTA block, the
remap *method*) port to almost anything. The **boot mechanism** (injection vs cfgload), the
**dtb**, and all **offsets** are device-specific.

### 3.3 Notes for the two named targets

**Ugoos AM9 Pro (S905X5 / S6).** Already has a working *single-boot* eMMC installer in
`ugoos-am9-pro-coreelec-emmc/` using the `cfgload` + `mount-storage.sh` method (its u-boot *can*
scan eMMC). To add Android dual-boot there: keep `super` + a bootable Android path and add a gate
(env `boot_ce`, or a conditional `cfgload`) rather than the injection trick — injection is
unnecessary on that board. Reuse from this project: the Toolbox addon, Kodi sources, GMS OTA block,
and the remap method. That repo also provides the **universal restore path** (native Amlogic
USB-DNL burner, `burn/aml-dnl-burn.py`) usable to restore *any* of these Amlogic devices to stock
without Windows.

**Xiaomi TV Box (other s7d/Amlogic variants).** Closest to this project. Likely needs the
**injection** method if its u-boot behaves like the stick's. Must re-derive: the exact partition
map/offsets, the correct dtb, the env format, and the A/B layout. The OTA block, Toolbox addon, and
Kodi sources reuse directly. The remote keymap needs the new remote's vendor/product + scancodes.

### 3.4 Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Stage 1 wipes Android user data | expected | One-time; the OS itself is intact |
| Wrong dtb → CoreELEC mis-boots | medium | Validate the dtb via USB/SD boot **before** internal install |
| Wrong env format assumption → broken gate | low-brick | u-boot falls back to Android; USB-burn restores. Verify env size/CRC first |
| A/B OTA clobbers the CE slot | high if unblocked | `blockgms` (+ `blockota`); A/B `misc` CE-slot-unbootable |
| CE update strips the gate / stales kernel | handled | `user-update.sh` v3 + `env_dualboot.bin` (confirmed self-healing) |
| GMS Play-system-update bootloop from disabled components | low | Magisk safe-mode (3 failed boots) / `install_blockgms.py --revert` |
| Running stage 1 on the wrong model | high | Pre-flight refuses non-`twilight` / non-stock units |
| Device where u-boot can neither scan **nor** inject | blocking | No eMMC-boot path; fall back to SD/USB CoreELEC |
| Per-device identity loss | none here | `reserved`/identity partitions never written; env identity repopulated by keyman |

**Universal safety net:** Amlogic `boot0`/`boot1` are hardware write-protected → the device can
always enter USB burn mode and be restored to stock with the Amlogic USB Burning Tool (or the
native `aml-dnl-burn.py`). No software install can permanently brick these devices.

---

## 4. Source map

| Area | Files |
|---|---|
| Build (per-device env, layout) | `partB-installer/build/{envtool,build_env,ab_misc,layout}.py` |
| Android-phase installer | `partB-installer/installer/{install,flash_to_coreelec,deploy_flash_recovery,install_blockgms,install_blockota,install_toolbox_export,reassert_env_gate,restore_*}.py` |
| CoreELEC-phase installer | `partB-installer/installer/{deploy_toolbox_addon,deploy_kodi_sources,deploy_remote_keymap}.py` |
| CE-update hook | `partB-installer/payload/flash/user-update.sh` |
| Magisk modules | `partB-installer/app/{blockgms,blockota,toolbox_export}/` |
| CoreELEC Toolbox addon | `partB-installer/addon/script.coreelec.toolbox/` |
| Remote mapping | `partB-installer/payload/remote/{99-xiaomi-remote.hwdb,xiaomi.xml}` |
| CoreELEC SSH/diag helpers | `ce_*.py` (repo root), `ce_inspect_initramfs.py` |
| Amlogic USB-burn + restore (Ugoos) | `ugoos-am9-pro-coreelec-emmc/{burn,lib,img-tools}/` |
