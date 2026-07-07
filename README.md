This Repo offers a method to boot **CoreELEC from internal eMMC** alongside the stock **Google TV** on a
Xiaomi 4k 2nd Gen or a Xiaomi TV Box S 3rd Gen.

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

Follow these steps in order. Run every command from a terminal opened inside the folder you unzipped.

Two placeholders appear in the commands:
- `<ip:port>` is your device's address for the Android steps, for example `192.168.1.50:5555`.
- `<coreelec-ip>` is your device's address once it is running CoreELEC.

When only one device is connected you can leave out `--serial <ip:port>`; the tool finds it on its own.

**1. Get ready (do this once)**
- On your device: open Settings, turn on **Developer options**, then turn on **USB debugging** and **OEM unlocking**.
- On your PC: install **Python 3** and **Android platform-tools** (this gives you the `adb` and `fastboot` commands). Then run `pip install paramiko`.
- Download the release for your device from the [latest Release](../../releases/latest) and unzip it.

**2. Connect your device to the PC**
Plug in by USB and approve the "Allow USB debugging" prompt on the TV, or connect over Wi-Fi with `adb connect <ip:port>`. Then check the PC sees it:
```
adb devices
```
Your device should show up in the list. Steps 3 and 4 need a USB cable, so use USB if you can.

**3. Unlock the bootloader (this erases everything on the device)**
With the device connected by USB, run:
```
python installer/install.py stage_unlock --serial <ip:port> --yes
```
The device restarts into a setup mode and shows a **Mi logo**. If the TV asks you to confirm, use the remote (or the volume and power buttons) to approve. This step checks first; if your device is already unlocked it just reboots and moves on. When it unlocks, the device wipes itself and restarts into first-time setup.

After it restarts, set the device up again:
1. Finish first-time setup (you can skip signing in to Google).
2. Turn **USB debugging** and **OEM unlocking** back on.
3. Reconnect (plug in by USB again, or run `adb connect <ip:port>`).

**4. Give the device root access (needs USB)**
```
python installer/install.py stage_magisk --serial <ip:port>
```
This installs the Magisk app and flashes the init_boot.img that grants root. It checks that the file matches your device's exact software version and stops if it does not. The device restarts into setup mode, flashes the file, and restarts into Android. Once the device reboots open the **Magisk app** to allow it to finish its setup. To check root worked:
```
adb shell su -c id
```
Magisk will ask if you want to grant root access, allow it and adb should return `uid=0`.

**5. Back up the device (safe, changes nothing)**
```
python installer/install.py stage0 --serial <ip:port>
```
This saves a full backup to a `pulled_backups` folder and checks the device is ready. Do not skip it; the undo steps later need this backup.

**6. Install CoreELEC (this erases your apps and data on the device)**
First do a test run that only prints the plan and changes nothing:
```
python installer/install.py stage1 --serial <ip:port>
```
If it finishes with OK, run the real install and restart:
```
python installer/install.py stage1 --serial <ip:port> --yes
adb -s <ip:port> reboot
```
On restart the device resizes its storage once and boots back into the Android first-time setup.

**7. Set the device up again, then reconnect**
Go through the initial setup again, turn **USB debugging** back on, and reconnect with `adb connect <ip:port>` if using wireless debugging. The address may have changed.

**8. Put the Magisk app back**
Step 6 removed it. Re-install it:
```
python installer/install.py stage1b --serial <ip:port>
```
The tool pauses and asks you to open the **Magisk app** on the device, finish its setup, and allow the root-access request. Once root is confirmed, press Enter in the terminal.

**9. Install the switcher app and modules**
```
python installer/install.py stage2 --serial <ip:port>
```
This installs the **Reboot to CoreELEC** app and the pieces that let you switch between the two systems and keep updates from breaking CoreELEC. Do not open CoreELEC before this step; the switch will not work yet.

**10. Block Xiaomi system updates**
```
python installer/install.py stage2a --serial <ip:port>
```
Next Reboot again to apply the changes:
```
adb -s <ip:port> reboot
```

**11. Start CoreELEC**
On the device, open the **Reboot to CoreELEC** app and choose to restart into CoreELEC.

**12. Finish CoreELEC setup**
Once the device is running CoreELEC, make sure to enable **SSH** in CoreELEC's settings if it was not enabled during initial setup and note your IP address (`<coreelec-ip>`). Then from the PC:
```
python installer/install.py stage3 --host <coreelec-ip>
```
This adds the Toolbox add-on, the PM4K and TinyPPI download sources, and (on Xiaomi) the remote-button mapping.

**Done.** A normal restart goes to **Android**. Open **Reboot to CoreELEC** to switch to **CoreELEC**.
To change which one starts by default, see [How To Use](#how-to-use). To undo everything, see
[Reverse / restore](#reverse--restore).

---

## How To Use

**Day to day**
- **Switch to CoreELEC:** open the **Reboot to CoreELEC** app on the device and choose CoreELEC.
- **Switch back to Android:** just restart the device. A normal restart always goes to Android.

**Make CoreELEC start by default instead**
Out of the box a normal restart goes to Android. To make the device boot straight into CoreELEC, run from the PC, use the CoreELEC toolbox app in CoreELEC to switch the default.

You can also set this while installing by adding `--default coreelec` to the Step 6 command.

**If switching stops working after a CoreELEC update**
CoreELEC updates repair the dual-boot setup on their own. If a switch ever stops working right after one, run from the PC:
```
python installer/reassert_env_gate.py --serial <ip:port> --boot-ce 1
```

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
