# magisk/ — Magisk APK and device-specific patched init_boot images

This directory contains everything `stage_magisk` needs to root the device automatically:

- **`Magisk-vXX.X.apk`** — the Magisk manager app, installed automatically by `stage_magisk`
- **a pre-patched init_boot image per supported device** — filename is looked up in the
  device registry (`build/devices.py`), NOT derived from the codename.

Supported devices and their images:

| Device | model (`ro.product.model`) | patched init_boot |
|---|---|---|
| Xiaomi TV Stick 4K 2nd Gen | `MiTV-AFMU1` | `magisk/twilight-init_boot-patched.img` |
| Xiaomi TV Box S 3rd Gen | `MiTV-AFMU0` | `magisk/xiaomi_tv_box_s_3rd_gen_init_boot.img` |

## Why the registry (and not the codename)

Both devices report the **same** `ro.product.device` = `twilight`, so a codename-derived
filename would hand the box the stick's rooted `init_boot` (and vice-versa) — a brick.
`stage_magisk` instead calls `devices.identify()`, which pins the unit by **model**
(`ro.product.model`) cross-checked against the **eMMC sector count**, then picks that
device's image from the table above. Add a new device once, in `build/devices.py`, and
`stage_magisk` picks up its image automatically.

## Normal usage

Just run (USB must be connected for the fastboot flash step):
```
python installer/install.py stage_magisk
```
The script installs the Magisk APK, identifies the unit, flashes that unit's pre-patched
`init_boot` into the **active slot** via fastboot, and reboots back to Android.

## Firmware-match guard

A Magisk-patched `init_boot` only boots the **exact stock build** it was patched from —
flashing it onto a unit on a different build can bootloop. Each image records that build in
its ramdisk `build.prop` (`ro.bootimage.build.fingerprint`), so before flashing `stage_magisk`
reads it back out of the image (`devices.boot_fingerprint_from_img`) and compares it to the
unit's live `ro.bootimage.build.fingerprint`. On a mismatch it **aborts without flashing** and
prints both. There is no pin to maintain — the image is self-describing.

The images shipped here are patched from **HyperOS `V816.0.7.0`** (Android 14): stick
`V816.0.7.0.UZFAATK`, box `V816.0.7.0.UZFAABX`. Check a unit with
`adb shell getprop ro.bootimage.build.fingerprint`.

If your unit is on a different build, either update it to `V816.0.7.0`, or patch your own
`init_boot` (below) and pass `--magisk-img <path>`, which skips the guard.

## Creating a patched image for a new device

If the pre-patched image for your device is not included (or you want to patch against a newer firmware):

1. Register the device in `build/devices.py` (model, eMMC sector count, `magisk_img` filename).
2. Install the [Magisk app](https://github.com/topjohnwu/Magisk) on the device.
3. Get `init_boot.img` (from your OTA package, or push via `adb push init_boot.img /sdcard/`).
4. In Magisk: **Install → Select and patch a file**, pick `init_boot.img`.
5. Pull and rename the result to the `magisk_img` filename you registered:
   ```
   adb pull /sdcard/Download/magisk_patched-*.img magisk/<registered-name>.img
   ```
6. Run: `python installer/install.py stage_magisk`

The `*.img` files in this directory are gitignored (device/build-specific binaries).
