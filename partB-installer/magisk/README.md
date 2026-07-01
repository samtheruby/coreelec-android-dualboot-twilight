# magisk/ — Magisk APK and device-specific patched init_boot images

This directory contains everything `stage_magisk` needs to root the device automatically:

- **`Magisk-vXX.X.apk`** — the Magisk manager app, installed automatically by `stage_magisk`
- **`{device}-init_boot-patched.img`** — pre-patched init_boot image for each supported device

For the Xiaomi TV Stick 4K 2nd Gen (`twilight`), the pre-patched image is:
```
magisk/twilight-init_boot-patched.img
```

`stage_magisk` auto-detects the connected device's name via `adb getprop ro.product.device`
and picks the matching image automatically. This naming scheme lets a single bundle folder
support multiple device types without ambiguity.

## Normal usage

Just run (USB must be connected for the fastboot flash step):
```
python installer/install.py stage_magisk --serial <ip:port>
```
The script installs the Magisk APK, flashes the pre-patched `init_boot` of the active slot
via fastboot, and reboots back to Android.

## Creating a patched image for a new device

If the pre-patched image for your device is not included (or you want to patch against a newer firmware):

1. Install the [Magisk app](https://github.com/topjohnwu/Magisk) on the device
2. Get `init_boot.img` (from your OTA package, or push via `adb push init_boot.img /sdcard/`)
3. In Magisk: **Install → Select and patch a file**, pick `init_boot.img`
4. Pull and rename the result:
   ```
   adb pull /sdcard/Download/magisk_patched-*.img magisk/{device}-init_boot-patched.img
   ```
5. Run: `python installer/install.py stage_magisk --serial <ip:port>`

The `*.img` files in this directory are gitignored (device/build-specific binaries).
