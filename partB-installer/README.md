# Part B — CoreELEC internal dual-boot installer (Xiaomi TV Stick 2nd Gen `twilight`)

Turns the manual Part A procedure into a **near-zero-interaction installer** for
**stock, rooted `twilight` (Amlogic s7d / S905X5M) units only**: PC-built artifacts
flashed from rooted Android over `adb`. No USB stick, no CoreELEC-from-USB, no
on-device `parted`/`mkfs`.

> **Validated end-to-end on a fresh stock unit** (install → CoreELEC boot from eMMC →
> switcher round-trip → a real CoreELEC OS update survived via the self-heal hook). See
> **Testing status** below.

---

## What it does (mechanism, recap)

u-boot can't see GPT partitions we add, but it *can* read the existing named
partitions. So CoreELEC's kernel goes into the **inactive** `boot_<slot>`, its dtb
into the inactive `dtbo_<slot>`, SYSTEM/storage onto new `CE_FLASH`/`CE_STORAGE`
(GPT, found by LABEL via Linux). A u-boot env gate (`boot_ce`) routes: flag set →
CoreELEC from eMMC; else → Android. OTA disabled + CE slot marked unbootable so
Android A/B never clobbers it.

## Partition layout (carved from the 4176 MiB userdata span)

| Partition | Size | LBA range |
|---|---|---|
| `userdata` (Android) | 2376 MiB | 6713344 – 11579391 |
| `CE_FLASH` (FAT32) | 600 MiB | 11579392 – 12808191 |
| `CE_STORAGE` (ext4) | 1200 MiB | 12808192 – 15265791 |

Single source of truth: `build/layout.py` (the PC driver imports it for offsets).
On a larger-eMMC port, bump `CE_FLASH`/`CE_STORAGE` — see research.md §2.4.

---

## Directory

```
partB-installer/
  build/            PC-side artifact builders (run build_all.py)
    layout.py            geometry (single source of truth)
    build_gpt_layout.py  full 128-entry GPT + new layout, all CRCs (self-verifying)
    build_boota_dtboa.py boota.img (kernel) + dtboa.img (dtb, 128 KiB)
    build_ce_flash.sh    ce_flash.img  600 MiB FAT32, populated   (WSL)
    build_ce_storage.sh  ce_storage.img 1200 MiB empty ext4       (WSL)
    envtool.py           u-boot env parse/edit/crc (the env codec)
    build_env.py         env additions + gate; build_target_env(); reads refdata/
    ab_misc.py           A/B bootloader_control: mark CE slot unbootable
    build_all.py         orchestrates everything -> artifacts/
  refdata/          committed, identity-free build inputs (see refdata/README.md)
    stock_gpt_first2m/last2m.bin   stock twilight GPT reference (random GUIDs only)
    env_additions.json             the 9 generic boot keys
  artifacts/        generated blobs (gpt_*, boota, dtboa, ce_flash, ce_storage, ...)
  payload/          CoreELEC files you supply (NOT committed)
    flash/               kernel.img, SYSTEM(+md5), dtb.img, dovi.ko, cfgload, user-update.sh ...
    remote/              99-xiaomi-remote.hwdb, xiaomi.xml  (remote keymap, committed)
  installer/
    install.py           staged orchestrator (stage_magisk/stage0/1/2/2a/stage3/verify) -- the entry point
    flash_to_coreelec.py core engine: preflight, per-unit env/misc, PC-side backups,
                         STREAMED writes (nc-over-adb-forward tunnel), verify; --dry-run
    deploy_*.py          stage3 CoreELEC-side: toolbox addon, Kodi sources, remote keymap
    install_blockgms/blockota/toolbox_export.py   Magisk module installers
    restore_stock_gpt.py / restore_env_misc_factory.py   reverse (read pulled_backups/)
    finish_install.py    recovery for a partially-applied install
    reassert_env_gate.py boot-default + gate re-assert
    probe_android.py / validate_nondestructive.py   read-only recon / 21-check validation
    pull_coreelec_payload.py   (re)pull /flash + env from a booted CE unit (build-time)
  app/blockgms/ blockota/ toolbox_export/   Magisk modules (module.prop + service.sh)
  app/RebootToCoreELEC/  the trigger app (EnvFlip.kt flips boot_ce via root)
  addon/script.coreelec.toolbox/   CoreELEC Toolbox addon (BT sync / boot default / WiFi MAC)
```

---

## Pre-built bundle (no WSL, no build step)

The ready-to-ship bundle (`make_dist.py` → `dist/` + `partB-installer-dist.zip`,
~388 MiB, big images gzipped, **APK included**) is published as the repo's
**[Release](../../releases/latest)** asset. The end user needs only Python 3 + adb
(+ paramiko for stage 3) and runs `installer/install.py`; see `dist/INSTALL.md` or the
**[top-level README](../README.md)** for the full step-by-step. Self-containment is
verified by the bundle's own `validate_nondestructive.py` (uses `env_additions.json`,
no `payload/`).

Regenerate the bundle: `python make_dist.py`.

## Build from source

Build inputs split into **committed (identity-free)** and **you-supply**:
- committed: `refdata/` (stock GPT reference + `env_additions.json`) + all source.
- you supply: the CoreELEC OS payload in `payload/flash/` (GPLv2, not redistributed
  here — harvest it with `installer/pull_coreelec_payload.py` from a booted CE unit,
  or extract a CoreELEC Generic image) and a **WSL** env with `dosfstools` + `mtools`
  + `e2fsprogs` (for the two FS images). Everything else is pure-Python.

```
python build/build_all.py     # gpt + boota/dtboa + env_additions + ce_flash/ce_storage
python make_dist.py           # assemble the bundle
```

Produces in `artifacts/`: `gpt_primary.bin`, `gpt_backup.bin`, `boota.img`,
`dtboa.img`, `ce_flash.img`, `ce_storage.img`. Per-unit `env_target.bin` /
`misc_target.bin` are generated live at install time from each target's own env —
no per-device dump is ever baked into an artifact.

### Build the app (WSL)

`bash app/build_apk.sh` — installs JDK 17 + Android SDK 34 (first run) and runs
`./gradlew assembleDebug`, copying `RebootToCoreELEC.apk` into `artifacts/`.

## Install (on a stock rooted unit)

> `--serial` takes a **USB id or `ip:port`** (the scripts just pass it to `adb -s`; USB and
> wireless behave identically). Omit `--serial` to auto-pick when only one device is attached.

**Root first (if not already):** place the Magisk-patched `init_boot.img` as
`init_boot_patched.img` in the bundle root (next to `installer/`), then:
```
python installer/install.py stage_magisk --serial <ip:port>
```
Reboots into the fastboot bootloader, flashes `init_boot_a`, and reboots back to Android.
USB must be connected for the fastboot step. After this, `su` works and stage 0 proceeds.

```
# dry run first (no device writes; prepares per-unit blobs, prints the write plan)
python installer/flash_to_coreelec.py --serial <ip:port> --dry-run

# real install
python installer/flash_to_coreelec.py --serial <ip:port> --yes
```

Pre-flight refuses anything that isn't a stock, rooted, unlocked, unmodified
`twilight`. Then it pulls **PC-side backups** of every region it will touch
(`pulled_backups/`, before any write), and **streams** the new GPT + filesystems +
kernel/dtb + A/B + env into device `dd`s over a **TCP tunnel** (`adb forward` +
on-device `busybox nc -l | [gzip -dc |] dd of=… seek=…`) — writing by **raw
whole-disk offset** (super/userdata are mounted on live Android, so the new
partition nodes aren't usable until reboot). The tunnel is used because
`adb exec-out` doesn't forward stdin and `adb shell` mangles binary via a pty.
Because every write's input is streamed from the PC, nothing is read from the
partition being overwritten (no read-during-overwrite race), nothing is buffered
in device RAM, and the backups can't be destroyed by the install.
GPT is written first (so a later failure still lets Android reformat userdata and
boot); env is written last and re-read to verify. Finally it **arms the bootloader
control block** (`misc` offset 0: `boot-recovery` + `--wipe_data`) so the next reboot
enters recovery, reformats the now-smaller userdata to its new size and re-keys
encryption, then boots Android. (A bare superblock wipe is undone by a clean reboot's
cached-superblock writeback — Android flushes the original SB back over the zeros — so
the BCB recovery wipe is the deterministic trigger; same path `fastboot -w` / OTA use.)
Reboot → Android (default). Use the app → CoreELEC.

## Durable OTA blocking (Block-OTA Magisk module)

`pm disable` alone is insufficient here for two reasons: (1) the install's
first-boot userdata reformat erases `/data/system`, re-enabling the updater; and
(2) a single disable can be re-enabled. The fix is a tiny **Magisk module**
(`app/blockota/`) whose `service.sh` runs **every boot** and, for
`com.xiaomi.mitv.updateservice`: `pm disable-user` then `pm clear` (wipes any
downloaded/staged OTA + its scheduling state), plus
`settings put global ota_disable_automatic_update 1`.

This matters because `ro.product.ab_ota_partitions` includes `boot` + `dtbo`, so
an applied A/B OTA would write the **inactive slot = our CoreELEC slot** and flip
active, clobbering CoreELEC. Install it **after first boot**:
```
python installer/install_blockota.py --serial <ip:port>   # places /data/adb/modules/blockota_twilight
adb -s <ip:port> reboot
python installer/install_blockota.py --serial <ip:port> --verify
```
Remove the module (Magisk app) to restore normal OTA.

## App

`app/RebootToCoreELEC` (fork of jamal2367's). The "Reboot to CoreELEC" button now
calls `EnvFlip.bootCoreElec()`: reads the env via root, sets `boot_ce=1`,
recomputes the CRC32, writes it back, reboots. **No `fw_setenv`** (Android lacks
it). `EnvFlip.kt`'s env codec is **byte-identical to `build/envtool.py`** (verified).
A bad write is not bricking — u-boot falls back to its built-in default env (boots
Android) on a CRC mismatch.

---

## Env handling — identity-preserving (important)

The working unit's env has per-device identity baked in (`serial`, `assm_sn`,
`did_key` = MAC + key, `cpu_id`, `ethaddr`). We must **not** ship that to other
units. Instead the installer reads each **target's own** env and only:
- adds the generic boot-source helpers a fresh stock env lacks (`bootfromsd/usb/
  nand`, `cfgload*`), copied from the known-good env, and
- installs the slot-correct `boot_ce` gate.

Everything else — all identity — is left untouched; u-boot's `cmdline_keys`/
`keyman` repopulates per-device values from the target's `reserved` partition at
every boot. Verified: `validate_nondestructive.py` confirms identity survives.

---

## Testing status

Validated **non-destructively** against the working unit (`validate_nondestructive.py`,
21/21): identity device facts, env build + identity preservation, app codec ==
PC codec, A/B marking, GPT geometry + CRCs, artifact sizes. Pre-flight correctly
aborts the already-modified unit (both repo + dist driver).

**Fully validated end-to-end on a 2nd, fresh stock unit (v1.0.5).** The complete flow
works on a unit other than the dev unit:
- stage0–3 install; 8-region SHA-256 read-back verify passes.
- Android picks up the new GPT (userdata → 2376 MiB; CE_FLASH/CE_STORAGE by-name, bounded
  to their own nodes — no overlap), then the **BCB recovery wipe** reformats userdata to
  the new size; **stage 2 re-applies the env gate** the reset clears.
- **CoreELEC boots from internal eMMC**; the switcher round-trips (CoreELEC → Android).
- Remote keymap remaps; OTA blocks active (blockgms/blockota/toolbox_export modules).
- CoreELEC-default boot works.
- **A real CoreELEC OS nightly update was applied and the `user-update.sh` self-heal hook
  kept the dual-boot alive** (kernel/dtb re-synced + env gate restored).

Getting here surfaced four ordering bugs, each fixed (v1.0.1 deterministic userdata
reformat → v1.0.2 stage-2 re-gate → v1.0.3 no re-gate loop → v1.0.4 Kodi auto-config →
v1.0.7 `stage_magisk` pre-stage for Magisk root via fastboot).
Only a soft/hard factory-reset *from* the installed state is still untested (low stakes:
re-run stage 2 to re-gate).

## Reversibility

stage0 pulls per-region backups to `pulled_backups/` before any write (full 2 MiB
backup-GPT region included, so the alt header is captured). To undo a clean install:
```
python installer/restore_stock_gpt.py        --serial <ip:port> --yes   # GPT + userdata wipe
python installer/restore_env_misc_factory.py --serial <ip:port> --yes   # env + misc
```
Both prefer this unit's own `pulled_backups/` and fall back to the dev `device_backups/`
Phase-0 dumps. Then remove the Magisk modules (`blockgms_sysupdate`, `blockota_twilight`,
`toolbox_export`) to restore OTA/exports. Worst case: USB-burn factory (boot0
HW-write-protected → unbrickable that way).
```
