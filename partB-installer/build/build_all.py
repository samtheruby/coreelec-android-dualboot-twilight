#!/usr/bin/env python3
"""
Build every PC-side artifact for the Part B installer, in order, with verification.
Run from anywhere:  python build/build_all.py [--device stick|box]   (default: stick)

Pure-Python steps run directly; the two filesystem images are built via WSL
(mkfs.vfat/mtools + mke2fs). Per-device flashables land in artifacts/<slug>/; the
shared CoreELEC payload (payload/flash/) is device-independent. The installer
(flash_to_coreelec.py) streams these to the device.
"""
import os, subprocess, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devices

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ART = os.path.join(ROOT, "artifacts")
import layout as L  # noqa: E402


def run_py(script, *args):
    print(f"\n### {script} {' '.join(args)}")
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *args])
    if r.returncode != 0:
        sys.exit(f"{script} failed")


def win2wsl(p):
    """C:\\a\\b -> /mnt/c/a/b  (deterministic, no arg passes backslashes to wsl)."""
    p = os.path.abspath(p)
    drive, rest = p[0].lower(), p[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def run_wsl(script, *args):
    print(f"\n### (wsl) {script} {' '.join(args)}")
    wp = win2wsl(os.path.join(HERE, script))
    inner = "bash " + shq(wp) + "".join(" " + shq(a) for a in args)
    r = subprocess.run(["wsl.exe", "-e", "bash", "-lc", inner])
    if r.returncode != 0:
        sys.exit(f"{script} (wsl) failed")


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="stick", choices=list(devices.BY_SLUG),
                    help="which device to build artifacts for (default: stick)")
    args = ap.parse_args()
    dev = devices.BY_SLUG[args.device]
    outdir = os.path.join(ART, dev.slug)
    os.makedirs(outdir, exist_ok=True)
    print(f"=== build_all for [{dev.slug}] {dev.name} -> artifacts/{dev.slug}/ ===")
    print("layout:")
    subprocess.run([sys.executable, os.path.join(HERE, "layout.py"), dev.slug])
    run_py("build_gpt_layout.py", "--device", dev.slug)   # gpt_primary.bin + gpt_backup.bin
    run_py("build_boota_dtboa.py", "--device", dev.slug)  # boota.img + dtboa.img
    run_py("build_env.py")                                 # env_additions.json (device-generic)
    run_wsl("build_ce_flash.sh", dev.slug)                 # ce_flash.img (FAT32, populated)
    run_wsl("build_ce_storage.sh", dev.slug)               # ce_storage.img (empty ext4)

    print(f"\n=== artifacts/{dev.slug}/ ===")
    for f in sorted(os.listdir(outdir)):
        p = os.path.join(outdir, f)
        if os.path.isfile(p):
            print(f"  {f:<24} {os.path.getsize(p):>12,} B")


if __name__ == "__main__":
    main()
