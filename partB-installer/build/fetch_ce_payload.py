#!/usr/bin/env python3
"""
Populate payload/flash/ with the CoreELEC-provided files from a released CoreELEC image.

Until now payload/flash/ was filled by installer/pull_coreelec_payload.py, which SSHes into a
RUNNING, known-good CoreELEC box. That cannot work in CI, and it also means a human implicitly
vouched for the payload. This script takes the same files straight from a published image so a
release can be built from nothing but the repo plus two URLs.

  --source nightly    relkai.coreelec.org  Amlogic-no/ce-22 Piers_nightly_<date>-Generic
  --source samurihl   github.com/SamuriHL/CoreELEC releases (the DV fork's pre-releases)

HOW THE FILES COME OUT
The image is a partitioned disk image whose first partition is the FAT that CoreELEC mounts as
/flash. We read it with mtools at a byte offset taken from the partition table:

    sfdisk -J ce.img            -> start sector of the FAT partition
    mcopy -i ce.img@@<offset>   -> copy files straight out of it

No loop device, no mount, no root. mtools is already required by build_ce_flash.sh (which
WRITES our FAT with mcopy/mdir), so reading with the same toolchain adds no new dependency and
no privileged step. losetup+mount would need root and is the flakiest thing available in a
container; 7z/bsdtar guess at the layout instead of reading the partition table.

WHY THE VERIFY STEP IS THE IMPORTANT PART
The extraction method barely matters; proving what came out does. CoreELEC ships SYSTEM.md5 and
kernel.img.md5 INSIDE the image, so we check both after extracting. That is an end-to-end test
of the download, the gunzip, the partition offset and the copy at once -- and it fails here,
with a clear message, instead of surfacing as a corrupt ce_flash.img ten minutes later or, worse,
as a box that does not boot. SamuriHL publishes a .sha256 per asset, which is checked before we
unpack; relkai does not, so there gzip -t plus those two md5s are the integrity story.

Our own additions are NOT touched: user-update.sh and resolution.ini are tracked in git, and
dovi.ko is copied from the Toolbox addon, which is where it is single-sourced and sha256-pinned.

Usage:
  python build/fetch_ce_payload.py --source nightly
  python build/fetch_ce_payload.py --source nightly  --version 20260823
  python build/fetch_ce_payload.py --source samurihl --version v22.0-samurihl-20260812171608
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLASH = os.path.join(ROOT, "payload", "flash")
ADDON_DOVI = os.path.join(ROOT, "addon", "script.coreelec.toolbox", "resources", "repair", "dovi.ko")

RELKAI_DIR = "https://relkai.coreelec.org/Amlogic-no/ce-22/"
RELKAI_INDEX = "https://relkai.coreelec.org/?dir=Amlogic-no/ce-22"
SAMURIHL_API = "https://api.github.com/repos/SamuriHL/CoreELEC/releases"

# Files CoreELEC puts on /flash that we re-ship. Missing REQUIRED is fatal: every one of these
# is in a stock CoreELEC image, so its absence means the extraction went wrong, and a payload
# that is quietly short by one file is exactly how this project has been bitten before.
REQUIRED = ["SYSTEM", "SYSTEM.md5", "kernel.img", "kernel.img.md5",
            "recovery.img", "cfgload", "aml_autoscript", "config.ini"]
# dtb.img is NOT in a stock image -- CoreELEC picks one out of device_trees/ for the detected
# board and writes it as dtb.img during install/update ("Updating dtb.img by dtb.xml..." in its
# update log). We are building an image for a known board, so we select it ourselves, below.
# It is taken from the device_trees/ of the image we just downloaded, never from a copy in the
# repo: the dtb has to match the kernel it will be booted with.
CE_DT_ID = "s7d_s905x5m_xiaomi_3rd_gen"   # stick and box are one board (see build_boota_dtboa.py)
# Present in some builds only. Warn -- loudly -- rather than fail.
OPTIONAL = ["cfgload_env"]
# Directories copied wholesale.
DIRS = ["device_trees"]
# Ours, not CoreELEC's. Listed so the log can say what was deliberately left alone.
OURS = ["user-update.sh", "resolution.ini", "dovi.ko"]


def log(m):
    print(f"[fetch-ce] {m}", file=sys.stderr, flush=True)


def get(url, binary=True):
    req = urllib.request.Request(url, headers={"User-Agent": "coreelec-dualboot-build"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def download(url, dest):
    log(f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "coreelec-dualboot-build"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    log(f"  -> {dest} ({os.path.getsize(dest)} B)")


# ---- resolving which build to fetch -----------------------------------------------------

def resolve_nightly(version):
    """(url, name). version 'latest' scrapes the index for the newest Piers_nightly date."""
    if version and version != "latest":
        if not re.fullmatch(r"\d{8}", version):
            sys.exit(f"--version for nightly must be an 8-digit date or 'latest', got {version!r}")
        date = version
    else:
        html = get(RELKAI_INDEX, binary=False)
        dates = re.findall(r"Piers_nightly_(\d{8})-Generic\.img\.gz", html)
        if not dates:
            sys.exit("no Piers_nightly_<date>-Generic.img.gz found in the relkai index -- "
                     "the listing format changed, or the directory moved")
        date = max(dates)
        log(f"newest nightly on relkai: {date} (of {len(set(dates))} listed)")
    name = f"CoreELEC-Amlogic-no.aarch64-22.0-Piers_nightly_{date}-Generic.img.gz"
    return RELKAI_DIR + name, name


def resolve_samurihl(version):
    """(url, name, sha256_url). version 'latest' takes the newest release with a Generic image."""
    rels = json.loads(get(SAMURIHL_API, binary=False))
    if version and version != "latest":
        rels = [r for r in rels if r["tag_name"] == version]
        if not rels:
            sys.exit(f"no SamuriHL/CoreELEC release tagged {version!r}")
    for rel in rels:                       # the API returns newest first
        img = next((a for a in rel["assets"] if a["name"].endswith("-Generic.img.gz")), None)
        if not img:
            continue
        sha = next((a for a in rel["assets"] if a["name"] == img["name"] + ".sha256"), None)
        log(f"SamuriHL release {rel['tag_name']} ({'pre-release' if rel['prerelease'] else 'release'})")
        return img["browser_download_url"], img["name"], (sha or {}).get("browser_download_url")
    sys.exit("no SamuriHL release carries a *-Generic.img.gz asset")


# ---- reading the FAT out of the image ---------------------------------------------------

def need(tool):
    if shutil.which(tool) is None:
        sys.exit(f"{tool} is not installed -- this script needs sfdisk (util-linux) and mtools")


def fat_offset(img):
    """Byte offset of the FAT partition, from the partition table -- never assumed."""
    out = subprocess.run(["sfdisk", "-J", img], capture_output=True, text=True, check=True).stdout
    parts = json.loads(out)["partitiontable"]
    sector = parts.get("sectorsize", 512)
    fat_types = {"c", "b", "e", "6", "0c", "0b", "0e", "06",
                 "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"}   # MBR FAT ids + MS Basic Data GUID
    for p in parts["partitions"]:
        if str(p.get("type", "")).lower() in {t.lower() for t in fat_types}:
            return p["start"] * sector
    # Fall back to the first partition: CoreELEC images put /flash first.
    first = min(parts["partitions"], key=lambda p: p["start"])
    log(f"WARNING: no partition typed as FAT; falling back to the first one at "
        f"sector {first['start']}")
    return first["start"] * sector


def mcopy_out(img, offset, names, dirs, dest):
    src = f"{img}@@{offset}"
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    got, missing = [], []
    for n in names:
        r = subprocess.run(["mcopy", "-n", "-i", src, f"::{n}", os.path.join(dest, n)],
                           capture_output=True, text=True, env=env)
        (got if r.returncode == 0 else missing).append(n)
    for d in dirs:
        target = os.path.join(dest, d)
        shutil.rmtree(target, ignore_errors=True)
        r = subprocess.run(["mcopy", "-n", "-s", "-i", src, f"::{d}", target],
                           capture_output=True, text=True, env=env)
        (got if r.returncode == 0 else missing).append(d + "/")
    return got, missing


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_pair(dest, data_name, md5_name):
    """CoreELEC ships <file>.md5 next to <file>; check it. End-to-end proof of the extraction."""
    stored = open(os.path.join(dest, md5_name)).read().split()[0].strip().lower()
    calc = md5(os.path.join(dest, data_name))
    if calc != stored:
        sys.exit(f"{data_name} md5 {calc} != {md5_name} {stored} -- the image, the download or "
                 f"the extraction is bad; refusing to build a payload from it")
    log(f"  {data_name} md5 OK ({calc})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, choices=["nightly", "samurihl"])
    ap.add_argument("--version", default="latest",
                    help="'latest' (default), an 8-digit nightly date, or a SamuriHL tag")
    ap.add_argument("--dest", default=FLASH)
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, ".ce-cache"))
    ap.add_argument("--keep-image", action="store_true",
                    help="keep the decompressed .img (debugging; it is ~2 GB)")
    a = ap.parse_args()

    need("sfdisk")
    need("mcopy")
    os.makedirs(a.cache_dir, exist_ok=True)
    os.makedirs(a.dest, exist_ok=True)

    sha_url = None
    if a.source == "nightly":
        url, name = resolve_nightly(a.version)
    else:
        url, name, sha_url = resolve_samurihl(a.version)
    build = name[:-len("-Generic.img.gz")] if name.endswith("-Generic.img.gz") else name
    log(f"CoreELEC build: {build}")

    gz = os.path.join(a.cache_dir, name)
    if not os.path.exists(gz):
        download(url, gz)
    else:
        log(f"using cached {gz}")

    # SamuriHL publishes a checksum; relkai does not. Check it when it exists.
    if sha_url:
        want = get(sha_url, binary=False).split()[0].strip().lower()
        h = hashlib.sha256()
        with open(gz, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        if h.hexdigest() != want:
            sys.exit(f"sha256 {h.hexdigest()} != published {want} -- bad download")
        log(f"  sha256 OK ({want})")
    else:
        log("  no published checksum for this source; relying on gzip integrity + the "
            "SYSTEM/kernel md5s shipped inside the image")

    img = os.path.join(a.cache_dir, name[:-3])
    log(f"gunzip -> {img}")
    with gzip.open(gz, "rb") as i, open(img, "wb") as o:   # raises on a truncated/corrupt gz
        shutil.copyfileobj(i, o, 1 << 20)

    off = fat_offset(img)
    log(f"FAT partition at byte offset {off}")
    got, missing = mcopy_out(img, off, REQUIRED + OPTIONAL, DIRS, a.dest)
    for g in got:
        log(f"  + {g}")
    hard = [m for m in missing if m in REQUIRED]
    for m in missing:
        log(f"  {'!! MISSING' if m in REQUIRED else '   absent (optional)'} {m}")
    if hard:
        sys.exit(f"required files not found in the image FAT: {hard}")

    # dtb.img: select this board's device tree out of the image's own device_trees/.
    dt_src = os.path.join(a.dest, "device_trees", CE_DT_ID + ".dtb")
    if not os.path.exists(dt_src):
        have = sorted(os.listdir(os.path.join(a.dest, "device_trees")))
        sys.exit(f"{CE_DT_ID}.dtb is not in this image's device_trees/ "
                 f"({len(have)} present, e.g. {have[:5]}) -- CoreELEC dropped or renamed this "
                 f"board's device tree, and guessing another one would boot the wrong hardware "
                 f"description")
    shutil.copyfile(dt_src, os.path.join(a.dest, "dtb.img"))
    dtb = open(os.path.join(a.dest, "dtb.img"), "rb").read()
    # Same two properties build_ce_flash.sh checks later, asserted here so a bad device tree is
    # reported against the image it came from rather than 10 minutes into the image build.
    if dtb[:4] != b"\xd0\x0d\xfe\xed":
        sys.exit(f"{CE_DT_ID}.dtb does not start with the FDT magic d00dfeed")
    if len(dtb) > 0x20000:
        sys.exit(f"{CE_DT_ID}.dtb is {len(dtb)} B, past the 128 KiB dtbo span u-boot reads")
    log(f"  dtb.img <- device_trees/{CE_DT_ID}.dtb ({len(dtb)} B, FDT magic OK)")

    log("verifying the checksums CoreELEC ships inside the image")
    verify_pair(a.dest, "SYSTEM", "SYSTEM.md5")
    verify_pair(a.dest, "kernel.img", "kernel.img.md5")

    # dovi.ko is single-sourced from the addon (build_ce_flash.sh pins its sha256). The other
    # two additions are tracked in git and already sitting in dest.
    shutil.copyfile(ADDON_DOVI, os.path.join(a.dest, "dovi.ko"))
    log(f"our additions left in place: {', '.join(OURS)}")
    for o in OURS:
        if not os.path.exists(os.path.join(a.dest, o)):
            sys.exit(f"{o} is missing from {a.dest} -- build_ce_flash.sh will abort on it")

    meta = {"source": a.source, "build": build, "image": name, "url": url}
    with open(os.path.join(ROOT, "payload", "ce_build.json"), "w") as f:
        json.dump(meta, f, indent=2)
    if not a.keep_image:
        os.remove(img)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"build={build}\nimage={name}\n")
    log(f"done: payload/flash is now {build}")
    print(build)


if __name__ == "__main__":
    main()
