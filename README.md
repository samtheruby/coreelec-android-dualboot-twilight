# POC CoreELEC ⇄ Android internal dual-boot — Xiaomi TV Stick 4K (2nd Gen, `twilight`)

POC to Boot **CoreELEC from internal eMMC** alongside the stock **Android (Google TV)** OS on a
Xiaomi TV Stick 4K 2nd Gen — no USB stick, no SD card, both OSes on the internal 8 GB eMMC.
Android stays the default; CoreELEC is one tap away (or make CoreELEC the default instead).
**No partition is deleted, per-device identity is preserved, and it is fully reversible.**

> **Status: validated end-to-end on a fresh stock unit** — install → CoreELEC boots from
> eMMC → switcher round-trips both ways → a real CoreELEC OS update survived via the
> self-heal hook. (Soft/hard factory-reset from the installed state is the only untested path.)

> **Hardware scope:** Xiaomi TV Stick 4K 2nd Gen — codename `twilight` / `adastra`,
> model `MiTV-AFMU1`, SoC **Amlogic s7d (S905X5M)**, 2 GB RAM, ~7.28 GiB eMMC, Android 14.
> **Stage 1 is model-locked to `twilight`** (GPT offsets, dtb, partition sizes are specific to
> it). Pre-flight refuses anything else. The CoreELEC-side extras (Toolbox addon, Kodi sources)
> are generic and run on any CoreELEC box. For porting to other devices see
> [`research.md`](research.md).

---

## ⚠️ Read first
- **READ THE ENTIRE README BEFORE ATTEMPTING**
- **Stage 1 is destructive to Android user data.** It shrinks `userdata` and carves CoreELEC
  partitions out of the freed space, then arms a recovery wipe; the next boot enters recovery and
  reformats `userdata` to its new size (a one-time factory-reset-like wipe). Your apps/logins on
  the stick are erased. The OS itself is intact.
- **THERE IS NO WARRANTY AND I AM NOT RESPONSIBLE FOR ANY DAMAGE DONE TO YOUR DEVICE**
- **Not brickable from software.** Amlogic `boot0`/`boot1` are hardware write-protected, so the
  device can always be restored via the Amlogic USB Burning Tool over USB. A bad install means a
  restore cycle, not a dead device.
- **Reversible.** Every region is backed up to the PC before any write; `restore_stock_gpt.py` +
  `restore_env_misc_factory.py` put it back. Worst case: USB-burn the factory image.
- **This is unsupported by CoreELEC.** Do not file CoreELEC bug reports for a device set up this way. If you have bugs open a report here!

---

## What you need

**PC (Windows/Linux/macOS):**
- **Python 3** (3.8+)
- **adb** (Android platform-tools) on `PATH`
- **paramiko** for the CoreELEC-side stage: `pip install paramiko`
- **The prepared installer bundle** — download `partB-installer-dist.zip` from the
  [latest Release](../../releases/latest) and unzip it. It is fully self-contained
  (all images, apps, and modules prebuilt); run `installer/install.py` straight out of
  the unzipped folder. **No WSL, no build step, no CoreELEC download required.**
  (To assemble the bundle yourself, see [Build from source](#build-from-source).)

**The stick:**
- Developer options on → **USB debugging** + **Network debugging** (ADB)
- Bootloader unlocked (ships unlockable; required for Magisk)
- **Root** (Magisk) — `su` must work before stage 0. If not yet rooted, the
  `stage_magisk` step (step 3 below) handles it via fastboot.

Connect by **USB** (plug in, authorize the prompt) or **wireless** (`adb connect <stick-ip>:<port>`),
then confirm `adb devices` shows it. Every `--serial` below accepts a **USB id or `ip:port`** — or
omit `--serial` to auto-pick when only one device is attached.

---

## Step-by-step install (start here)

Follow these in order. `<ip:port>` is your stick's ADB address (e.g. `192.168.1.50:5555`);
`<coreelec-ip>` is its IP once booted into CoreELEC. Run every command from inside the
unzipped bundle folder (the one that contains the `installer/` folder).

**1. Get ready**
- On the stick: enable **Developer options → USB debugging + Network debugging**
- On the PC: install **Python 3** and **adb**, then `pip install paramiko`.
- Download `partB-installer-dist.zip` from the [latest Release](../../releases/latest) and unzip it.

**2. Connect** — USB (plug in + authorize) or wireless:
```
adb connect <ip:port>  # wireless only; for USB just plug in and authorize the prompt
adb devices            # confirm your stick is listed (USB id or ip:port)
```
> In every command below you can drop `--serial <…>` when only one device is attached (it
> auto-detects), or pass the **USB id** instead of `ip:port`. USB is faster/steadier for stage 1.

**3. Root the stick with Magisk (skip if already rooted)**
Skip if `adb shell su -c id` already returns `uid=0`.

With **USB connected** (required for the fastboot flash step), run:
```
python installer/install.py stage_magisk --serial <ip:port>
```
The script installs the bundled Magisk APK, reboots into the bootloader, flashes the
pre-patched `init_boot_a`, and reboots back to Android. If root is not immediately confirmed,
open the Magisk app to complete first-time setup, then verify: `adb shell su -c id` → `uid=0`.

**4. Back up + preflight (safe, no changes)**
```
python installer/install.py stage0 --serial <ip:port>
```
Saves a full backup of every region to `pulled_backups/` and refuses to go on unless the stick is a clean, stock, rooted `twilight`. **Don't skip this** — it's what `restore` uses later.

**5. Install — ⚠️ THIS WIPES ANDROID USER DATA**
Start with a dry run -
```
python installer/install.py stage1 --serial <ip:port>
```
Once it returns with OK you can do the destructive install
```
python installer/install.py stage1 --serial <ip:port> --yes
adb -s <ip:port> reboot
```
> This step writes the new partition layout + CoreELEC. Every region is SHA-256 verified, then a recovery wipe is armed. On `reboot` the stick enters **recovery**, reformats its (now smaller) storage one time, and boots Android. (Leave off `--yes` to do a dry run that only prints the plan.)

**6. Re-setup Android, then reconnect**
- Let the recovery wipe + Android first-time setup finish (it reboots itself once). Walk through setup, re-enable USB/Network debugging.
- `adb connect <ip:port>` again (the address may change).

**7. Re-install Magisk APK (stage 1's factory reset wiped it)**
```
python installer/install.py stage1b --serial <ip:port>
```
The factory reset wipes `/data` including the Magisk APK and its root-grant database. `init_boot_a` is still patched so no fastboot is needed — this just re-installs the APK. When prompted, open the **Magisk app** on the stick, complete first-time setup, and **approve the root-access dialog** for ADB shell. The script waits up to 120 s for `uid=0` confirmation, then prints the stage 2 command.

**8. Apps + modules**
```
python installer/install.py stage2 --serial <ip:port>
```
Re-applies the u-boot boot gate (stage 1's factory reset clears it), then installs the **Reboot to CoreELEC** app, the OS-update self-heal files, and the modules that keep updates from clobbering CoreELEC. **Don't try CoreELEC before stage 2** — without this step the switcher can't enter it. Reboot after this stage with `adb -s <ip:port> reboot` only if you are not running stage2a.

**9. (Optional) Block the Xiaomi updater too**
```
python installer/install.py stage2a --serial <ip:port>
adb -s <ip:port> reboot
```

**10. Boot into CoreELEC**
- After the reboot, open the **Reboot to CoreELEC** app on the stick and reboot into CoreELEC.

**11. Finish CoreELEC setup**
```
python installer/install.py stage3 --host <coreelec-ip>
```
Adds the Toolbox addon, the PM4K + TinyPPI download sources, and (on Xiaomi) the remote-button keymap.

**Done.** Normal reboot → **Android**. Open **Reboot to CoreELEC** → **CoreELEC**.
To flip which one is the default, see [Using it](#using-it). To undo everything, see
[Reverse / restore](#reverse--restore).

---

## Install — overview

The install has **two phases**, distinguished by how the PC talks to the stick:

| phase | device booted in | transport | stages |
|---|---|---|---|
| **Pre-install** | Android (Google TV) | `adb` + USB fastboot | stage_magisk |
| **Android** | Android (Google TV) | `adb` / `--serial <ip:port>` | stage0, stage1, stage1b, stage2, stage2a, verify |
| **CoreELEC** | CoreELEC (after first CE boot) | `ssh` / `--host <ip>` | stage3 |

Run everything through `partB-installer/installer/install.py`. Each stage is also runnable
standalone.

---

### Stage_magisk — flash Magisk-patched init_boot via fastboot (pre-stage 0)
```
python installer/install.py stage_magisk --serial <ip:port>
```
Reboots the stick into the fastboot bootloader, flashes `init_boot_a` with the Magisk-patched image,
then reboots back to Android. **Requires USB for fastboot** (WiFi ADB alone isn't enough).

The image is auto-located by device name: `magisk/{device}-init_boot-patched.img` (e.g.
`magisk/twilight-init_boot-patched.img`). Falls back to `artifacts/` and the bundle root for
compatibility. Override with `--magisk-img <path>`. Use `--fastboot-serial <serial>` if multiple
fastboot devices are connected.

Only needed once per unit. If root is already active, skip it.

### Stage 0 — preflight + backups (read-only)
```
python installer/install.py stage0 --serial <ip:port>
```
Confirms the unit is a stock, rooted `twilight` with the expected 32-entry GPT, and pulls
per-region backups (env, misc, reserved, boot_*, dtbo_*, GPT) to the PC. **Refuses to proceed on
a non-stock / already-modified unit.**

### Stage 1 — core install (DESTRUCTIVE)
```
python installer/install.py stage1 --serial <ip:port> --yes      # omit --yes for a dry-run
adb -s <ip:port> reboot
```
Writes the 128-entry GPT (userdata 2376 / CE_FLASH 600 / CE_STORAGE 1200 MiB), the CoreELEC
boot/storage images, the CoreELEC kernel into the inactive `boot_<slot>` and dtb into
`dtbo_<slot>`, patches `misc` (A/B) and the u-boot `env` (boot gate). **Every one of the 8
regions is SHA-256 verified after writing.** Finally it arms the bootloader control block
(`misc`: `boot-recovery` + `--wipe_data`), so the reboot enters **recovery**, reformats the
now-smaller userdata to its new size, re-keys encryption, and boots Android → first-time setup
once. (A plain superblock wipe is undone by a clean reboot's cached-superblock writeback; the BCB
recovery wipe is the deterministic path.) Reconnect adb afterward.

### Stage 1b — re-install Magisk APK (after the stage 1 factory reset)
```
python installer/install.py stage1b --serial <ip:port>
```
The stage 1 factory reset wipes `/data`, removing the Magisk APK and its root-grant database.
`init_boot_a` is still patched so no fastboot is needed — only the APK needs re-installing.
Open the **Magisk app** on the stick when prompted, complete first-time setup, and approve the
ADB root grant. The script waits up to 120 s for `uid=0` before printing the stage 2 command.

### Stage 2 — apps + modules (Android side)
```
python installer/install.py stage2 --serial <ip:port>
adb -s <ip:port> reboot
```
- **(Re)asserts the u-boot boot gate** first. Stage 1's reboot factory-resets userdata via recovery,
  which on this SoC also resets the u-boot env to stock and drops the gate; stage 2 rebuilds the full
  gate (generic boot helpers + `boot_ce`) on the post-reset env. (Pass `--default coreelec` here too
  if you used it in stage 1.) Without this step CoreELEC is unreachable.
- **Reboot to CoreELEC** app — taps into CoreELEC from Android.
- `/flash` **update-recovery files** (`env_dualboot.bin`, `ce_slot.conf`, `user-update.sh`) so the
  dual-boot **survives CoreELEC OS updates**.
- **`toolbox_export`** Magisk module — each Android boot, copies the decrypted Bluetooth pairings
  + WiFi/BT MAC to `/flash`, so the CoreELEC Toolbox addon can sync your BT remote into CoreELEC.
- **`blockgms`** — disables only the Google Play Services *system-update* components, so Settings →
  "Check for updates" can't push an A/B OTA that would clobber CoreELEC (Play/accounts/casting
  untouched).

### Stage 2a — Xiaomi auto-update block (optional)
```
python installer/install.py stage2a --serial <ip:port>
```
Disables the Xiaomi updater (`com.xiaomi.mitv.updateservice`). Optional belt-and-suspenders on top
of blockgms.

### Boot into CoreELEC, then Stage 3 — CoreELEC side
Reboot into CoreELEC (open the **Reboot to CoreELEC** app), enable SSH (Settings → CoreELEC →
Services → SSH), note its IP, then:
```
python installer/install.py stage3 --host <coreelec-ip>
```
- **CoreELEC Toolbox addon** *(generic)* — Sync Bluetooth remotes from Android · Set default boot
  OS · Fix WiFi MAC.
- **Kodi sources** *(generic)* — adds `PM4K` (`https://pm4k.eu/`) and `jamal2362`
  (`https://ce-repo.github.io/repository.jamal2362/`) under File Manager, so you can *Add-ons →
  Install from zip file* → install **PM4K** (`script.plexmod`) and **TinyPPI** (`script.tinyppi`).
- **Xiaomi remote keymap** *(Xiaomi, auto-detected)* — remaps the remote's special buttons:
  **OK → Select**, **Netflix → PM4K**, **Voice & PrimeVideo → TinyPPI**. Skipped automatically on
  non-Xiaomi units; force with `--xiaomi`, skip with `--no-keymap`.

All stage-3 files live in `/storage`, so they survive CoreELEC OS updates.

### verify (read-only)
```
python installer/install.py verify --serial <ip:port>
```
Layout / env / CoreELEC-readiness check.

---

## Using it

- **Normal reboot → Android** (default). Open **Reboot to CoreELEC** → CoreELEC. A normal reboot
  returns to Android.
- **Make CoreELEC the default** instead: add `--default coreelec` to stage1, or on an installed
  unit run `python installer/reassert_env_gate.py --serial <ip:port> --default coreelec`. Then a
  normal power-on boots CoreELEC, and CoreELEC's built-in **"reboot to eMMC/nand"** boots Android.
  If CoreELEC ever fails to boot, u-boot falls through to Android automatically — so
  CoreELEC-default is safe. Flip back with `--default android`.
- **CoreELEC OS updates self-heal.** A CE update rewrites `/flash` and resets the u-boot boot gate;
  the `/flash/user-update.sh` hook (runs in the CE initramfs) re-syncs the kernel/dtb and restores
  the gate automatically. If the switcher ever stops working post-update:
  `python installer/reassert_env_gate.py --serial <ip:port> --boot-ce 1`.

---

## Reverse / restore

From the Android side, with the stage-0 backups present:
```
python installer/restore_stock_gpt.py --serial <ip:port> --yes          # stock GPT + userdata wipe
python installer/restore_env_misc_factory.py --serial <ip:port> --yes   # env + misc back to factory
```
Then remove the Magisk modules (`blockgms_sysupdate`, `blockota_twilight`, `toolbox_export`) to
restore OTA/exports. Worst case: **USB-burn the factory image** (`boot0` is HW write-protected, so
the device can always enter USB burn mode).

---

## Bundle contents

The prepared bundle (`partB-installer-dist.zip`, the Release asset) unzips to:

```
partB-installer/
  build/        envtool.py build_env.py ab_misc.py layout.py    (per-device env + layout logic)
  installer/    install.py (orchestrator) + per-stage scripts
  artifacts/    gpt_primary/backup, boota.img, dtboa.img, env_additions.json,
                ce_flash.img.gz, ce_storage.img.gz, RebootToCoreELEC.apk,
                script.coreelec.toolbox-*.zip
  magisk/       {device}-init_boot-patched.img                  (place your Magisk-patched image here)
  blockgms/ blockota/ toolbox_export/   (Magisk modules)
  flash/        user-update.sh                              (CoreELEC OS-update self-heal hook)
  payload/remote/   99-xiaomi-remote.hwdb  xiaomi.xml       (remote button mapping)
  INSTALL.md    SHA256SUMS.txt                              (generated into the bundle)
```

It needs only Python 3 + adb (+ paramiko for stage 3) — no WSL, no build step.

---

## Credits

This work builds on research and tools from several people:

- **[dangerouslaser](https://github.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc)** — Ugoos AM9
  Pro CoreELEC-on-eMMC research, the Amlogic USB-DNL burn/restore tooling, and the
  `cfgload` + `mount-storage.sh` boot method that informed our partition + boot analysis.
- **[U3knOwn](https://github.com/jamal2362)** — the **Reboot to CoreELEC** app and **TinyPPI**, plus the
  `repository.jamal2362` CoreELEC add-on repo.
- **Pro-me3us** — research on the Fire TV Cube dual-boot, the reference model for the
  boot-gate / kernel-injection approach.
- **[Pannal](https://github.com/pannal/CoreELEC)** - For his amazing work on custom coreelec 
  and Don't Panic Repo with PM4K.

The CoreELEC project and its contributors made the underlying OS and Amlogic device support
possible. This project is an unofficial, unsupported community effort and is not affiliated with or
endorsed by any of the above.
