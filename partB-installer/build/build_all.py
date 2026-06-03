#!/usr/bin/env python3
"""
Build every PC-side artifact for the Part B installer, in order, with verification.
Run from anywhere:  python build/build_all.py

Pure-Python steps run directly; the two filesystem images are built via WSL
(mkfs.vfat/mtools + mke2fs). Emits artifacts/. The installer (flash_to_coreelec.py)
streams these to the device; there is no on-device staging script.
"""
import os, subprocess, sys, json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ART = os.path.join(ROOT, "artifacts")
sys.path.insert(0, HERE)
import layout as L


def run_py(script):
    print(f"\n### {script}")
    r = subprocess.run([sys.executable, os.path.join(HERE, script)])
    if r.returncode != 0:
        sys.exit(f"{script} failed")


def win2wsl(p):
    """C:\\a\\b -> /mnt/c/a/b  (deterministic, no arg passes backslashes to wsl)."""
    p = os.path.abspath(p)
    drive, rest = p[0].lower(), p[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def run_wsl(script):
    print(f"\n### (wsl) {script}")
    wp = win2wsl(os.path.join(HERE, script))
    r = subprocess.run(["wsl.exe", "-e", "bash", "-lc", f"bash {shq(wp)}"])
    if r.returncode != 0:
        sys.exit(f"{script} (wsl) failed")


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    os.makedirs(ART, exist_ok=True)
    print("layout:")
    subprocess.run([sys.executable, os.path.join(HERE, "layout.py")])
    run_py("build_gpt_layout.py")     # gpt_primary.bin + gpt_backup.bin
    run_py("build_boota_dtboa.py")    # boota.img + dtboa.img
    run_py("build_env.py")            # env_additions.json + env_ref_ce_*.bin
    run_wsl("build_ce_flash.sh")      # ce_flash.img (FAT32, populated)
    run_wsl("build_ce_storage.sh")    # ce_storage.img (empty ext4)

    print("\n=== artifacts ===")
    for f in sorted(os.listdir(ART)):
        p = os.path.join(ART, f)
        if os.path.isfile(p):
            print(f"  {f:<24} {os.path.getsize(p):>12,} B")


if __name__ == "__main__":
    main()
