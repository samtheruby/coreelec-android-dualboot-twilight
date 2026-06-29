# magisk/ — device-specific Magisk-patched init_boot images

Place your Magisk-patched `init_boot` image here, named `{device}-init_boot-patched.img`.

For the Xiaomi TV Stick 4K 2nd Gen (`twilight`):
```
magisk/twilight-init_boot-patched.img
```

`stage_magisk` auto-detects the connected device's name via `adb getprop ro.product.device`
and picks the matching file automatically. This naming scheme lets a single bundle folder
support multiple device types without ambiguity.

## How to create the patched image

1. Install the [Magisk app](https://github.com/topjohnwu/Magisk) on the device
2. Get `init_boot.img` (from your OTA package, or push via `adb push init_boot.img /sdcard/`)
3. In Magisk: **Install → Select and patch a file**, pick `init_boot.img`
4. Pull and rename the result:
   ```
   adb pull /sdcard/Download/magisk_patched-*.img magisk/twilight-init_boot-patched.img
   ```
5. Run: `python installer/install.py stage_magisk --serial <ip:port>`

The `*.img` files in this directory are gitignored (device/build-specific binaries).
