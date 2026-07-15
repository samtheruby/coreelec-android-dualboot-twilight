#!/usr/bin/env python3
"""Package the CoreELEC Toolbox addon into artifacts/script.coreelec.toolbox-<version>.zip.

Refuses to build if the bundled repair references have drifted:
  * resources/repair/dovi.ko must be the pinned module
  * resources/repair/user-update.sh must equal payload/flash/user-update.sh
"""
import hashlib
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
ADDON = os.path.join(ROOT, "addon", "script.coreelec.toolbox")
ART = os.path.join(ROOT, "artifacts")
DOVI_SHA256 = "f6c26659a255447685ceac9441e399c999b1fae9c6435c48d70e14a14dd7f8f7"


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    # Target the <addon> tag's version, not the XML prolog's version="1.0".
    ver = re.search(r'<addon\b[^>]*\bversion="([^"]+)"',
                    open(os.path.join(ADDON, "addon.xml")).read()).group(1)

    dovi = os.path.join(ADDON, "resources", "repair", "dovi.ko")
    if _sha(dovi) != DOVI_SHA256:
        sys.exit(f"bundled dovi.ko {_sha(dovi)} != pinned {DOVI_SHA256}")
    hook_a = open(os.path.join(ADDON, "resources", "repair", "user-update.sh"), "rb").read()
    hook_b = open(os.path.join(ROOT, "payload", "flash", "user-update.sh"), "rb").read()
    if hook_a != hook_b:
        sys.exit("bundled user-update.sh has drifted from payload/flash/user-update.sh")

    os.makedirs(ART, exist_ok=True)
    out = os.path.join(ART, f"script.coreelec.toolbox-{ver}.zip")
    for old in os.listdir(ART):
        if old.startswith("script.coreelec.toolbox-") and old.endswith(".zip"):
            os.remove(os.path.join(ART, old))

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _, files in os.walk(ADDON):
            if "__pycache__" in base:
                continue
            for f in files:
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(base, f)
                arc = os.path.join("script.coreelec.toolbox", os.path.relpath(full, ADDON))
                z.write(full, arc)
    print(f"built {out}")


if __name__ == "__main__":
    main()
