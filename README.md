CoreELEC ⇄ Android internal dual-boot for the Xiaomi 4k 2nd Gen and Xiaomi TV Box S 3rd Gen

Method to Boot **CoreELEC from internal eMMC** alongside the stock **Android (Google TV)** OS on a
Xiaomi 4k 2nd Gen and Xiaomi TV Box S 3rd Gen **No partition is deleted, per-device identity is preserved, and it is fully reversible.**

> **Hardware scope:** Xiaomi 4k 2nd Gen and Xiaomi TV Box S 3rd Gen codename `MiTV-AFMU1` and `MiTV-AFMU0`, For porting to other devices see [`research.md`](research.md).

---

## ⚠️ Read first
- **READ THE ENTIRE README BEFORE ATTEMPTING**
- **THERE IS NO WARRANTY AND I AM NOT RESPONSIBLE FOR ANY DAMAGE DONE TO YOUR DEVICE**
- **Multiple Stages are destructive to Android user data. Your apps/logins on the stick are erased and the stick will need to be setup from scratch**
- **This is unsupported by CoreELEC.** Do not file CoreELEC bug reports for a device set up this way. If you have bugs open a report here!

---

## What you need

**PC (Windows/Linux/macOS):**
- **Python 3** (3.8+)
- **Android platform-tools** in the `PATH`
- **paramiko** for the CoreELEC-side stage: `pip install paramiko`
- **The prepared installer bundle** — download the release for the device you have from the
  [latest Release](../../releases/latest)

**On The Device:**
- Developer options on → **USB debugging** and **OEM Unlocking** enabled.
- A Unlocked and Rooted Bootloader, see below

---

## Step-by-step install (start here)

Follow these in order. `<ip:port>` is your stick's ADB address (e.g. `192.168.1.50:5555`);
`<coreelec-ip>` is its IP once booted into CoreELEC. Run every command from inside the
unzipped bundle folder.

**1. Get ready**
- On the stick: enable **Developer options → USB debugging and OEM Unlocking**
- On the PC: install **Python 3** and **adb**, then run `pip install paramiko`.
- Download `partB-installer-dist.zip` from the [latest Release](../../releases/latest) and unzip it.

**2. Connect** — USB (plug in + authorize) or wireless:
```
adb connect <ip:port>  # wireless only; for USB just plug in and authorize the prompt
adb devices            # confirm your stick is listed (USB id or ip:port)
```
> In every command below you can drop `--serial <…>` when only one device is attached (it
> auto-detects), or pass the **USB id** instead of `ip:port`. USB is faster/steadier for stage 1.

**3. Unlock the bootloader — ⚠️ THIS WIPES THE STICK (skip if already unlocked)**
Skip if `fastboot getvar unlocked` already shows `unlocked: yes`. Most sticks were never
unlocked, and a locked bootloader makes the next step (fastboot flash) fail.

With **USB connected**, run:
```
python installer/install.py stage_unlock --serial <ip:port> --yes
```
Reboots into the bootloader (a **Mi logo** splash appears on the stick), checks the lock
state with `fastboot getvar unlocked`, and — if locked — runs `fastboot flashing unlock`
+ `fastboot flashing unlock_critical`. If the stick's screen asks you to confirm, use the
remote/volume+power keys to approve. `getvar unlocked` then returns `yes` and it reboots.
> Unlocking **factory-resets the stick**. Afterwards walk through Android first-time setup
> from scratch (you can skip Google sign-in), re-enable **USB + Network debugging**, and
> `adb connect <ip:port>` again before continuing.

**4. Root the stick with Magisk (skip if already rooted)**
Skip if `adb shell su -c id` already returns `uid=0`.

With **USB connected** (required for the fastboot flash step), run:
```
python installer/install.py stage_magisk --serial <ip:port>
```
The script installs the bundled Magisk APK, identifies the unit and picks its patched
`init_boot`, **verifies that image matches the unit's exact firmware build** (aborts rather than
risk a bootloop on a mismatch), then flashes the **active slot's** `init_boot` and reboots to
Android. If root is not immediately confirmed, open the Magisk app to complete first-time setup,
then verify: `adb shell su -c id` → `uid=0`.

**5. Back up + preflight (safe, no changes)**
```
python installer/install.py stage0 --serial <ip:port>
```
Saves a full backup of every region to `pulled_backups/` and refuses to go on unless the stick is a clean, stock, rooted `twilight`. **Don't skip this** — it's what `restore` uses later.

**6. Install — ⚠️ THIS WIPES ANDROID USER DATA**
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

**7. Re-setup Android, then reconnect**
- Let the recovery wipe + Android first-time setup finish (it reboots itself once). Walk through setup, re-enable USB/Network debugging.
- `adb connect <ip:port>` again (the address may change).

**8. Re-install Magisk APK (stage 1's factory reset wiped it)**
```
python installer/install.py stage1b --serial <ip:port>
```
The factory reset wipes `/data` including the Magisk APK and its root-grant database. `init_boot_a` is still patched so no fastboot is needed — this just re-installs the APK. When prompted, open the **Magisk app** on the stick, complete first-time setup, and **approve the root-access dialog** for ADB shell. The script waits up to 120 s for `uid=0` confirmation, then prints the stage 2 command.

**9. Apps + modules**
```
python installer/install.py stage2 --serial <ip:port>
```
Re-applies the u-boot boot gate (stage 1's factory reset clears it), then installs the **Reboot to CoreELEC** app, the OS-update self-heal files, and the modules that keep updates from clobbering CoreELEC. **Don't try CoreELEC before stage 2** — without this step the switcher can't enter it. Reboot after this stage with `adb -s <ip:port> reboot` only if you are not running stage2a.

**10. (Optional) Block the Xiaomi updater too**
```
python installer/install.py stage2a --serial <ip:port>
adb -s <ip:port> reboot
```

**11. Boot into CoreELEC**
- After the reboot, open the **Reboot to CoreELEC** app on the stick and reboot into CoreELEC.

**12. Finish CoreELEC setup**
```
python installer/install.py stage3 --host <coreelec-ip>
```
Adds the Toolbox addon, the PM4K + TinyPPI download sources, and (on Xiaomi) the remote-button keymap.

**Done.** Normal reboot → **Android**. Open **Reboot to CoreELEC** → **CoreELEC**.
To flip which one is the default, see [Using it](#using-it). To undo everything, see
[Reverse / restore](#reverse--restore).

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
restore OTA/exports.

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

It needs only Python 3 + adb (+ paramiko for stage 3)

---

## Credits

This work builds on research and tools from several people:

- **[dangerouslaser](https://github.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc)** — Ugoos AM9
  Pro CoreELEC-on-eMMC research, the Amlogic USB-DNL burn/restore tooling, and the
  `cfgload` + `mount-storage.sh` boot method that informed our partition + boot analysis.
- **[gilgameshinter](https://github.com/gilgameshinter/)** — Provided the needed files and tests to add support for the Xiaomi TV Box S 3rd Gen
- **[U3knOwn](https://github.com/jamal2362)** — the **Reboot to CoreELEC** app and **TinyPPI**, plus the
  `repository.jamal2362` CoreELEC add-on repo.
- **Pro-me3us** — research on the Fire TV Cube dual-boot, the reference model for the
  boot-gate / kernel-injection approach.
- **[Pannal](https://github.com/pannal/CoreELEC)** - For his amazing work on custom coreelec 
  and Don't Panic Repo with PM4K.

The CoreELEC project and its contributors made the underlying OS and Amlogic device support
possible. This project is an unofficial, unsupported community effort and is not affiliated with or
endorsed by any of the above.
