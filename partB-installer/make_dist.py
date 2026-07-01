#!/usr/bin/env python3
"""
Assemble a fully pre-built, self-contained installer bundle in dist/ (+ a .zip).
No WSL / build step needed by the end user -- just Python 3 + adb + a stock
rooted twilight unit.

Bundle layout (mirrors the repo so the driver's imports work unchanged):
  dist/
    build/      envtool.py build_env.py ab_misc.py layout.py
    installer/  flash_to_coreelec.py
                validate_nondestructive.py probe_android.py
    artifacts/  gpt_primary.bin gpt_backup.bin boota.img dtboa.img
                env_additions.json ce_flash.img.gz ce_storage.img.gz
                RebootToCoreELEC.apk script.coreelec.toolbox-*.zip
    blockota/ blockgms/ toolbox_export/   (Magisk modules: module.prop + service.sh)
    payload/remote/  (99-xiaomi-remote.hwdb xiaomi.xml)
    flash/      user-update.sh   (CoreELEC OS-update self-heal hook)
    INSTALL.md  SHA256SUMS.txt
"""
import os, glob, shutil, gzip, zipfile, hashlib

ROOT = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(ROOT, "artifacts")
DIST = os.path.join(ROOT, "dist")

BUILD_PY = ["envtool.py", "build_env.py", "ab_misc.py", "layout.py"]
INSTALLER = ["install.py", "adb_serial.py", "flash_to_coreelec.py", "deploy_flash_recovery.py",
             "reassert_env_gate.py", "install_blockgms.py", "install_blockota.py",
             "install_toolbox_export.py", "deploy_toolbox_addon.py",
             "deploy_remote_keymap.py", "deploy_kodi_sources.py",
             "validate_nondestructive.py", "probe_android.py",
             # reverse/recovery (read pulled_backups/, written by stage0)
             "restore_stock_gpt.py", "restore_env_misc_factory.py", "finish_install.py"]
REMOTE = ["99-xiaomi-remote.hwdb", "xiaomi.xml"]   # payload/remote -> dist/payload/remote
FLASH = ["user-update.sh"]                          # payload/flash -> dist/flash (update-recovery hook)
BLOCKOTA = ["module.prop", "service.sh"]            # app/blockota -> dist/blockota
BLOCKGMS = ["module.prop", "service.sh"]            # app/blockgms -> dist/blockgms
TOOLBOX_EXPORT = ["module.prop", "service.sh"]      # app/toolbox_export -> dist/toolbox_export
ADDON_ZIP_GLOB = "script.coreelec.toolbox-*.zip"    # prebuilt CoreELEC Toolbox addon -> dist/artifacts
ART_RAW = ["gpt_primary.bin", "gpt_backup.bin", "boota.img", "dtboa.img",
           "env_additions.json", "RebootToCoreELEC.apk"]
ART_GZ = ["ce_flash.img", "ce_storage.img"]   # shipped gzipped


def gzip_to(src, dst):
    with open(src, "rb") as i, gzip.open(dst, "wb", compresslevel=6) as o:
        shutil.copyfileobj(i, o, length=1 << 20)


def main():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    for sub in ("build", "installer", "artifacts", "blockota", "blockgms", "toolbox_export",
                "flash", "magisk", os.path.join("payload", "remote")):
        os.makedirs(os.path.join(DIST, sub))

    for f in BUILD_PY:
        shutil.copy2(os.path.join(ROOT, "build", f), os.path.join(DIST, "build", f))
    for f in INSTALLER:
        shutil.copy2(os.path.join(ROOT, "installer", f), os.path.join(DIST, "installer", f))
    for f in BLOCKOTA:
        shutil.copy2(os.path.join(ROOT, "app", "blockota", f), os.path.join(DIST, "blockota", f))
    for f in BLOCKGMS:
        shutil.copy2(os.path.join(ROOT, "app", "blockgms", f), os.path.join(DIST, "blockgms", f))
    for f in TOOLBOX_EXPORT:
        shutil.copy2(os.path.join(ROOT, "app", "toolbox_export", f),
                     os.path.join(DIST, "toolbox_export", f))
    for f in REMOTE:
        shutil.copy2(os.path.join(ROOT, "payload", "remote", f),
                     os.path.join(DIST, "payload", "remote", f))
    for f in FLASH:   # update-recovery hook -> dist/flash (deploy_flash_recovery.py fallback path)
        shutil.copy2(os.path.join(ROOT, "payload", "flash", f), os.path.join(DIST, "flash", f))
    # magisk/: copy all files (APK + any pre-patched .img)
    magisk_src = os.path.join(ROOT, "magisk")
    for f in os.listdir(magisk_src):
        src = os.path.join(magisk_src, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(DIST, "magisk", f))

    addon_zips = sorted(glob.glob(os.path.join(ROOT, ADDON_ZIP_GLOB)))
    if not addon_zips:
        raise SystemExit(f"missing prebuilt addon zip ({ADDON_ZIP_GLOB}) in {ROOT}")
    shutil.copy2(addon_zips[-1], os.path.join(DIST, "artifacts", os.path.basename(addon_zips[-1])))
    for f in ART_RAW:
        shutil.copy2(os.path.join(ART, f), os.path.join(DIST, "artifacts", f))
    for f in ART_GZ:
        dst = os.path.join(DIST, "artifacts", f + ".gz")
        src_gz = os.path.join(ART, f + ".gz")
        src_raw = os.path.join(ART, f)
        if os.path.exists(src_gz):
            print(f"  copy {f}.gz ...", end="", flush=True)
            shutil.copy2(src_gz, dst)
        else:
            print(f"  gzip {f} ...", end="", flush=True)
            gzip_to(src_raw, dst)
        print(f" {os.path.getsize(dst)//1048576} MiB")

    open(os.path.join(DIST, "INSTALL.md"), "w", newline="\n", encoding="utf-8").write(INSTALL_MD)

    # SHA256SUMS
    lines = []
    for r, _, fs in os.walk(DIST):
        for f in sorted(fs):
            p = os.path.join(r, f)
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {os.path.relpath(p, DIST).replace(os.sep, '/')}")
    open(os.path.join(DIST, "SHA256SUMS.txt"), "w", newline="\n").write("\n".join(lines) + "\n")

    # zip
    zpath = os.path.join(ROOT, "partB-installer-dist.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:   # images already gz
        for r, _, fs in os.walk(DIST):
            for f in fs:
                p = os.path.join(r, f)
                z.write(p, os.path.join("partB-installer", os.path.relpath(p, DIST)))

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(DIST) for f in fs)
    print(f"\ndist/ = {total//1048576} MiB   zip = {os.path.getsize(zpath)//1048576} MiB -> {zpath}")
    print("contents:")
    for r, _, fs in os.walk(DIST):
        for f in sorted(fs):
            p = os.path.join(r, f)
            print(f"  {os.path.relpath(p, DIST):<40} {os.path.getsize(p):>12,} B")


INSTALL_MD = """# CoreELEC internal dual-boot installer -- staged (Xiaomi TV Stick 2nd Gen `twilight`)

Self-contained. Needs only **Python 3** + **adb** and a **stock, rooted, unlocked
`twilight`** (Amlogic s7d / S905X5M). No WSL / build step.

> Stage 1 ONLY for `twilight`. It shrinks/erases userdata (factory-reset-like);
> pre-flight refuses non-stock / already-modified units. Stage 2's app + GMS block
> are generic to Google/Android TV; stage 2a is Xiaomi-only.

Prep on the stick: Developer options + ADB on, bootloader unlocked (most sticks were
never unlocked -- see stage_unlock below). Connect by **USB** (plug in, authorize the
prompt) **or wireless** (`adb connect <ip>:<port>`); confirm `adb devices`. Every
`--serial` below takes either a **USB id or `ip:port`** -- or omit `--serial` entirely to
auto-pick when just one device is attached. (USB is typically faster + more stable for the
big stage-1 image streams.)

## Stage_unlock -- unlock the bootloader (run before stage_magisk, DESTRUCTIVE)
`stage_magisk` flashes via fastboot, which a **locked** bootloader refuses. Most sticks
were never unlocked, so run this first. **Unlocking factory-resets the stick.** Skip if
`fastboot getvar unlocked` already shows `unlocked: yes`.

With **USB connected**, run:
```
python installer/install.py stage_unlock --serial <ip:port> --yes
```
Reboots into the bootloader (a Mi-logo splash appears on the stick), checks the lock state
with `fastboot getvar unlocked`, and -- if locked -- runs `fastboot flashing unlock` +
`fastboot flashing unlock_critical` (confirm on the device with the remote/volume+power keys
if prompted), then reboots. Because unlocking wipes the stick, **re-setup Android from
scratch** (skip Google sign-in if you like), re-enable Developer options + ADB, reconnect,
then run stage_magisk.

## Stage_magisk -- install Magisk and flash patched init_boot (run before stage 0)
If the stick is not yet rooted, use this step to get root before stage 0 requires it. The
bootloader must be **unlocked** first (see stage_unlock).

With **USB connected** (required for the fastboot flash), run:
```
python installer/install.py stage_magisk --serial <ip:port>
```
The script automatically:
1. Installs the bundled Magisk APK via adb
2. Reboots into the bootloader and flashes the pre-patched `init_boot` of the active slot
3. Reboots back to Android and verifies root

If root is not immediately confirmed, open the Magisk app to complete any first-time setup,
then verify with `adb shell su -c id` → `uid=0`. Skip this entire step if root is already active.

## Stage 0 -- preflight + backups (read-only)
```
python installer/install.py stage0 --serial <ip:port>
```
Checks stock/rooted/twilight and pulls per-region backups to `pulled_backups/`.

## Stage 1 -- CORE install  (DESTRUCTIVE; ends at first reboot)
```
python installer/install.py stage1 --serial <ip:port> --yes      # omit --yes = dry-run
adb -s <ip:port> reboot
```
Writes GPT + CE_FLASH/CE_STORAGE + kernel/dtb + misc + env; every region is SHA-256
verified. Then it **arms the bootloader control block** (`misc`: `boot-recovery` +
`--wipe_data`). The reboot therefore enters **recovery**, which reformats the shrunk
userdata to its new size and re-keys encryption (factory-reset-like, one-time), then
boots Android first-boot setup. Let the wipe + setup finish, then reconnect adb and
run **stage 1b** before stage 2. (The reset also clears the u-boot env gate --
**stage 2 re-applies it**, so always run stage 2 before trying to boot CoreELEC.)

> Why the BCB and not just a superblock wipe: a clean `adb reboot` makes Android flush
> the cached original superblock back over the wipe, so the reformat never fires and
> `/data` is left oversized on the smaller partition. The BCB recovery wipe is the
> deterministic path (same one `fastboot -w` / OTA use).

## Stage 1b -- re-install Magisk APK (after the stage 1 factory reset)
```
python installer/install.py stage1b --serial <ip:port>
```
The stage 1 factory reset wipes `/data`, which removes the Magisk APK and its
root-grant database. The active slot's `init_boot` is **still patched** (that partition is untouched),
so no fastboot is needed -- only the APK needs to be re-installed.

The script installs the APK, then waits up to 120 s for root confirmation. During that
window: open the **Magisk app** on the device, complete any first-time setup, and
**approve the root-access dialog** for ADB shell. Once `uid=0` is confirmed the script
prints the stage 2 command and exits.

## Stage 2 -- apps + modules  (Android side)
```
python installer/install.py stage2 --serial <ip:port>
adb -s <ip:port> reboot
```
- **(Re)assert the env boot gate.** Stage 1's reboot factory-resets userdata via
  recovery, which on this SoC also resets the u-boot env to stock -- dropping the gate.
  Stage 2 rebuilds the full gate (generic helpers + `boot_ce`) on the post-reset env so
  the switcher works. (Add `--default coreelec` here too if you set it in stage 1.)
- **Reboot to CoreELEC** app + `/flash` update-recovery files (`env_dualboot.bin`,
  `ce_slot.conf`, hook -- so the dual-boot survives CoreELEC OS updates) *[Xiaomi]*.
- **toolbox_export** Magisk module *[generic]* -- each Android boot it copies the
  decrypted BT pairings (`bt_config.conf`) + WiFi/BT MAC to `/flash`, so the CoreELEC
  Toolbox addon can sync your BT remote into CoreELEC.
- **blockgms** *[generic Google TV]* -- disables the Play Services *system-update*
  components so "Check for updates" can't push an A/B OTA that would clobber CoreELEC
  (GMS core untouched).

## Stage 2a -- Xiaomi auto-update block  (OPTIONAL, Xiaomi only)
```
python installer/install.py stage2a --serial <ip:port>
```
Installs **blockota** (disables the Xiaomi `com.xiaomi.mitv.updateservice` updater).

## Stage 3 -- CoreELEC side  (after first CoreELEC boot; SSH, not adb)
Reboot into CoreELEC, note its IP + enable SSH, then from the PC (needs `pip install paramiko`):
```
python installer/install.py stage3 --host <coreelec-ip>
```
Runs three CoreELEC-side steps:
- **CoreELEC Toolbox addon** *[generic]* -- extracts the addon (BT-remote sync /
  default-boot-OS / WiFi-MAC helpers) into Kodi, rescans, and via JSON-RPC **enables it**
  + turns on **Unknown sources** + **Update official add-ons from = Any repositories**
  (so the PM4K/TinyPPI zips below install). Turns Kodi's web server on if needed.
- **Kodi sources** *[generic]* -- adds `PM4K` = `https://pm4k.eu/` and `jamal2362` =
  `https://ce-repo.github.io/repository.jamal2362/` under `<files>` (Kodi can't reload
  sources.xml live, so this stops Kodi, edits, restarts ~15s). Then *Add-ons > Install
  from zip file* to install PM4K (`script.plexmod`) + TinyPPI (`script.tinyppi`).
- **Xiaomi remote keymap** *[Xiaomi]* -- auto-detected; remaps the remote's special
  buttons (OK->Select, Netflix->PM4K, Voice & PrimeVideo->TinyPPI). Skipped on
  non-Xiaomi units; `--xiaomi` forces it, `--no-keymap` skips it.

All stage-3 files land in `/storage`, so they survive CoreELEC OS updates. The two
**[generic]** steps also run on any CoreELEC box, standalone:
```
python installer/deploy_toolbox_addon.py --host <coreelec-ip>
python installer/deploy_kodi_sources.py  --host <coreelec-ip>
```

## verify -- layout/env readiness (read-only, Android)
```
python installer/install.py verify --serial <ip:port>
```

## Using it
- Normal reboot -> Android (default).
- Open **Reboot to CoreELEC** -> CoreELEC. A normal reboot returns to Android.
- After a CoreELEC OS update the dual-boot self-heals (the `/flash/user-update.sh`
  hook re-syncs the kernel/dtb and restores the env gate). If the switcher ever
  stops working post-update: `python installer/reassert_env_gate.py --serial <ip:port> --boot-ce 1`.

## Boot default -- Android or CoreELEC
By default a normal reboot boots **Android** (the app enters CoreELEC). To make
**CoreELEC the default** instead:
- at install: add `--default coreelec` to stage1, OR
- on an installed unit (from Android): `python installer/reassert_env_gate.py --serial <ip:port> --default coreelec`
Then a normal power-on boots CoreELEC, and CoreELEC's built-in **"reboot to
eMMC/nand"** option boots Android (one-shot; next reboot returns to CoreELEC). If
CoreELEC ever fails to boot, u-boot automatically falls through to Android -- so
CoreELEC-default is safe. Flip back with `--default android`.

## Reverse
Restore `env`, `misc`, `boot_<slot>`, `dtbo_<slot>`, GPT from `pulled_backups/`;
remove the `blockgms_sysupdate` / `blockota_twilight` Magisk modules to restore OTA.
Worst case: USB-burn the factory image.
"""


if __name__ == "__main__":
    main()
