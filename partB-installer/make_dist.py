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
    platform-tools/  adb.exe fastboot.exe + DLLs (bundled; PATH-prepended at runtime)
    README.md  SHA256SUMS.txt
"""
import os, glob, shutil, gzip, zipfile, hashlib, argparse, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(ROOT)           # platform-tools/ + README.md live at the repo root
ART = os.path.join(ROOT, "artifacts")
sys.path.insert(0, os.path.join(ROOT, "build"))
import devices  # noqa: E402

# Runtime modules the installer imports (layout imports devices; flash_to_coreelec
# imports all of these). devices.py MUST ride along or the bundle fails to import.
BUILD_PY = ["envtool.py", "build_env.py", "ab_misc.py", "layout.py", "devices.py"]
INSTALLER = ["install.py", "adb_serial.py", "flash_to_coreelec.py", "deploy_flash_recovery.py",
             "reassert_env_gate.py", "install_blockgms.py", "install_blockota.py",
             "install_toolbox_export.py", "deploy_toolbox_addon.py",
             "deploy_remote_keymap.py", "deploy_kodi_sources.py",
             "validate_nondestructive.py", "probe_android.py",
             # reverse/recovery (read pulled_backups/, written by stage0)
             "restore_stock_gpt.py", "restore_env_misc_factory.py", "finish_install.py"]
REMOTE = ["99-xiaomi-remote.hwdb", "xiaomi.xml"]   # payload/remote -> dist/payload/remote
FLASH = ["user-update.sh"]                          # payload/flash -> dist/flash (update-recovery hook)
BLOCKOTA = ["module.prop", "service.sh"]            # modules/blockota -> dist/blockota
BLOCKGMS = ["module.prop", "service.sh"]            # modules/blockgms -> dist/blockgms
TOOLBOX_EXPORT = ["module.prop", "service.sh"]      # modules/toolbox_export -> dist/toolbox_export
ADDON_ZIP_GLOB = "script.coreelec.toolbox-*.zip"    # prebuilt CoreELEC Toolbox addon -> dist/artifacts
# Per-device flashables live in artifacts/<slug>/ and ship into dist/artifacts/<slug>/
# (the installer reads artifacts/<identified-device-slug>/). Generic ones stay flat.
ART_DEV_RAW = ["gpt_primary.bin", "gpt_backup.bin", "boota.img", "dtboa.img"]
ART_DEV_GZ = ["ce_flash.img", "ce_storage.img"]     # per-device, shipped gzipped
ART_GENERIC = ["env_additions.json", "RebootToCoreELEC.apk"]   # device-agnostic (flat)


def gzip_to(src, dst):
    with open(src, "rb") as i, gzip.open(dst, "wb", compresslevel=6) as o:
        shutil.copyfileobj(i, o, length=1 << 20)


def main():
    ap = argparse.ArgumentParser(description="Assemble a per-device installer bundle.")
    ap.add_argument("--device", default="stick", choices=list(devices.BY_SLUG),
                    help="which device to bundle (default: stick)")
    args = ap.parse_args()
    dev = devices.BY_SLUG[args.device]
    art_dev = os.path.join(ART, dev.slug)              # artifacts/<slug>/ (per-device flashables)
    DIST = os.path.join(ROOT, "dist", dev.slug)        # build output dir (gitignored)
    print(f"=== make_dist for [{dev.slug}] {dev.name} -> dist/{dev.slug}/ ===")

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    for sub in ("build", "installer", os.path.join("artifacts", dev.slug),
                "blockota", "blockgms", "toolbox_export", "flash", "magisk",
                os.path.join("payload", "remote")):
        os.makedirs(os.path.join(DIST, sub))

    for f in BUILD_PY:
        shutil.copy2(os.path.join(ROOT, "build", f), os.path.join(DIST, "build", f))
    for f in INSTALLER:
        shutil.copy2(os.path.join(ROOT, "installer", f), os.path.join(DIST, "installer", f))
    for f in BLOCKOTA:
        shutil.copy2(os.path.join(ROOT, "modules", "blockota", f), os.path.join(DIST, "blockota", f))
    for f in BLOCKGMS:
        shutil.copy2(os.path.join(ROOT, "modules", "blockgms", f), os.path.join(DIST, "blockgms", f))
    for f in TOOLBOX_EXPORT:
        shutil.copy2(os.path.join(ROOT, "modules", "toolbox_export", f),
                     os.path.join(DIST, "toolbox_export", f))
    for f in REMOTE:
        shutil.copy2(os.path.join(ROOT, "payload", "remote", f),
                     os.path.join(DIST, "payload", "remote", f))
    for f in FLASH:   # update-recovery hook -> dist/flash (deploy_flash_recovery.py fallback path)
        shutil.copy2(os.path.join(ROOT, "payload", "flash", f), os.path.join(DIST, "flash", f))

    # magisk/: the Magisk APK + THIS device's patched init_boot + README. Skip other
    # devices' *.img so the bundle carries only the image that matches its target.
    magisk_src = os.path.join(ROOT, "magisk")
    for f in os.listdir(magisk_src):
        if f.endswith(".img") and f != dev.magisk_img:
            continue
        src = os.path.join(magisk_src, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(DIST, "magisk", f))
    if not os.path.exists(os.path.join(DIST, "magisk", dev.magisk_img)):
        print(f"  WARNING: magisk/{dev.magisk_img} not found -- bundle has no patched "
              f"init_boot for {dev.slug} (stage_magisk will need --magisk-img)")

    # generic (flat) artifacts + the prebuilt CoreELEC Toolbox addon zip
    addon_zips = sorted(glob.glob(os.path.join(ART, ADDON_ZIP_GLOB)) +
                        glob.glob(os.path.join(ROOT, ADDON_ZIP_GLOB)))
    if not addon_zips:
        raise SystemExit(f"missing prebuilt addon zip ({ADDON_ZIP_GLOB}) in artifacts/")
    shutil.copy2(addon_zips[-1], os.path.join(DIST, "artifacts", os.path.basename(addon_zips[-1])))
    for f in ART_GENERIC:
        shutil.copy2(os.path.join(ART, f), os.path.join(DIST, "artifacts", f))

    # per-device flashables -> dist/artifacts/<slug>/
    for f in ART_DEV_RAW:
        shutil.copy2(os.path.join(art_dev, f), os.path.join(DIST, "artifacts", dev.slug, f))
    for f in ART_DEV_GZ:
        dst = os.path.join(DIST, "artifacts", dev.slug, f + ".gz")
        src_gz = os.path.join(art_dev, f + ".gz")
        src_raw = os.path.join(art_dev, f)
        if os.path.exists(src_gz):
            print(f"  copy {dev.slug}/{f}.gz ...", end="", flush=True)
            shutil.copy2(src_gz, dst)
        elif os.path.exists(src_raw):
            print(f"  gzip {dev.slug}/{f} ...", end="", flush=True)
            gzip_to(src_raw, dst)
        else:
            raise SystemExit(f"missing {dev.slug}/{f}[.gz] -- run build/build_all.py --device {dev.slug}")
        print(f" {os.path.getsize(dst)//1048576} MiB")

    # platform-tools/ -- bundle Google's adb/fastboot so the end user needs no
    # separate Android SDK install. adb_serial.py prepends dist/platform-tools/ to
    # PATH at runtime. Windows build as shipped (adb.exe + DLLs); ships Google's
    # NOTICE.txt alongside. Absent -> warn; bundle then needs adb on the user's PATH.
    pt_src = os.path.join(REPO_ROOT, "platform-tools")
    if os.path.isdir(pt_src):
        shutil.copytree(pt_src, os.path.join(DIST, "platform-tools"))
        n = sum(len(fs) for _, _, fs in os.walk(pt_src))
        print(f"  bundled platform-tools/ ({n} files)")
    else:
        print("  WARNING: platform-tools/ not found at repo root -- end user will "
              "need adb/fastboot on PATH")

    # README.md -- the top-level guide rides along in the bundle root (the single
    # user-facing doc; the old generated INSTALL.md was redundant and was removed).
    shutil.copy2(os.path.join(REPO_ROOT, "README.md"), os.path.join(DIST, "README.md"))

    # SHA256SUMS
    lines = []
    for r, _, fs in os.walk(DIST):
        for f in sorted(fs):
            p = os.path.join(r, f)
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {os.path.relpath(p, DIST).replace(os.sep, '/')}")
    open(os.path.join(DIST, "SHA256SUMS.txt"), "w", newline="\n").write("\n".join(lines) + "\n")

    # zip -> partB-installer-<slug>-dist.zip (internal root: partB-installer/)
    zpath = os.path.join(ROOT, f"partB-installer-{dev.slug}-dist.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:   # images already gz
        for r, _, fs in os.walk(DIST):
            for f in fs:
                p = os.path.join(r, f)
                z.write(p, os.path.join("partB-installer", os.path.relpath(p, DIST)))

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(DIST) for f in fs)
    print(f"\n[{dev.slug}] dist = {total//1048576} MiB   zip = {os.path.getsize(zpath)//1048576} MiB -> {os.path.basename(zpath)}")
    print("contents:")
    for r, _, fs in os.walk(DIST):
        for f in sorted(fs):
            p = os.path.join(r, f)
            print(f"  {os.path.relpath(p, DIST):<40} {os.path.getsize(p):>12,} B")



if __name__ == "__main__":
    main()
