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
(`ro.product.model`) cross-checked against **`ro.product.name`** (`adastra` on the stick,
`twilight` on the box), then picks that device's image from the table above. Add a new
device once, in `build/devices.py`, and `stage_magisk` picks up its image automatically.

Those are both Android properties, i.e. strings out of `build.prop`. The usual third check
— the physical eMMC sector count — is **not available in this stage**: reading
`/sys/class/block/mmcblk0/size` needs root, and rooting is precisely what has not happened
yet. So `stage_magisk` passes `read_sectors=None` (skipping the check honestly rather than
faking it) and asks the **bootloader** instead, in the moment before it flashes:
`fastboot getvar partition-size:userdata` makes the bootloader read the real GPT off the
eMMC, with no Android in the path. Stick userdata is 4176 MiB stock / 2376 MiB carved, box
is 26540 / 14800 — ~6× apart, no overlap — so a wrong unit cannot hide. On a mismatch the
stage reboots the device to Android and refuses to flash. This gate runs even with
`--magisk-img`: which image to flash is your call, which device is attached is not.

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
`init_boot` (below) and pass `--magisk-img <path>`. A supplied image **warns** on a
firmware mismatch instead of refusing — you may have patched against a build this repo
knows nothing about, so it is your call. Everything that is *not* your call still runs on
that path: the device-identity gate, the `ANDROID!` boot-image header check, and the size
sanity check.

## What else stage_magisk checks before it writes

`init_boot` is boot-critical, and `fastboot flash` writes whatever bytes it is handed, so
the image is gated on the way in:

1. **`ANDROID!` header magic + plausible size** — a zip, a truncated download or a stock
   firmware payload is refused rather than written to a partition the device boots from.
2. **SHA-256 against `SHA256SUMS.txt`** — every dist bundle ships one covering
   `magisk/*.img`; a corrupt unzip is caught before 8 MiB of it reaches the eMMC. Skipped
   (not failed) when there is no manifest, e.g. a source checkout or your own image.
3. **Active slot** — read from `ro.boot.slot_suffix` and never guessed. The stick runs
   slot `_b`; flashing `init_boot_a` there would root the *inactive* slot and leave the
   running system unrooted.

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
