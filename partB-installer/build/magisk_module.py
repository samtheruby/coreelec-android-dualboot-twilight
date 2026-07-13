#!/usr/bin/env python3
"""
Install a Magisk module from the PC -- the same way, with the same checks, every time.

blockota, blockgms and toolbox_export each grew their own copy of this: locate the module
source, check root, check Magisk, push module.prop + service.sh, cp them into
/data/adb/modules/<id>/, chmod, remove the staging copies. Three copies that had quietly
drifted apart -- only ONE checked that the module source had actually been found (the other
two raised `TypeError: join(None, ...)` instead of saying so), and NONE of them checked that
what landed on the device was what was sent.

service.sh runs as ROOT on every boot, which makes it the most privileged thing this
installer puts on the device. So it gets the same treatment as anything we flash: verified
against SHA256SUMS.txt before it is pushed, and hashed again off /data/adb/modules
afterwards to prove it arrived intact. A half-written root boot script is not something to
discover later.
"""
import hashlib, os, subprocess, sys
import bundle

FILES = ("module.prop", "service.sh")


def find_source(*candidates):
    """The first candidate directory that holds a module.prop, else None.
    (repo layout: modules/<name>/ ; shipped bundle: <name>/ )"""
    for p in candidates:
        if p and os.path.exists(os.path.join(p, "module.prop")):
            return p
    return None


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out",
                        "su -c '" + cmd.replace("'", "'\\''") + "'"], capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


def require_rooted_android(serial):
    if "uid=0" not in su(serial, "id")[0]:
        sys.exit("no root -- run `python install.py stage1b` first (the userdata reformat "
                 "erased the Magisk app, and with it the su database).")
    if su(serial, "[ -d /data/adb/magisk ] && echo y")[0].strip() != "y":
        sys.exit("/data/adb/magisk not found -- Magisk-rooted + booted into Android required")


def install(serial, src, modid, log=print):
    """Place <src>/{module.prop,service.sh} into /data/adb/modules/<modid>/ and verify the
    bytes that landed. Returns the module dir; sys.exit on any failure."""
    if src is None:
        sys.exit(f"{modid}: module source not found (no module.prop in any known location)")
    mdir = f"/data/adb/modules/{modid}"
    paths = [os.path.join(src, f) for f in FILES]
    for p in paths:
        if not os.path.exists(p):
            sys.exit(f"{modid}: missing {os.path.basename(p)} in {src}")

    bundle.verify(paths)          # vs SHA256SUMS.txt, before it goes near the device
    want = {f: hashlib.sha256(open(p, "rb").read()).hexdigest() for f, p in zip(FILES, paths)}

    for f, p in zip(FILES, paths):
        r = subprocess.run(["adb", "-s", serial, "push", p, f"/data/local/tmp/{f}"],
                           capture_output=True)
        if r.returncode != 0:
            sys.exit(f"{modid}: push {f} failed: "
                     f"{r.stderr.decode('utf-8', 'replace').strip()}")
        log(f"  pushed {f}")

    out, rc = su(serial, (
        f"set -e; mkdir -p {mdir}; "
        f"cp /data/local/tmp/module.prop {mdir}/module.prop; "
        f"cp /data/local/tmp/service.sh {mdir}/service.sh; "
        f"chmod 0755 {mdir}/service.sh; chmod 0644 {mdir}/module.prop; "
        f"rm -f /data/local/tmp/module.prop /data/local/tmp/service.sh; echo PLACED"))
    if rc != 0 or "PLACED" not in out:
        sys.exit(f"{modid}: module placement failed: {out.strip() or '(no output)'}")

    # Read the bytes back off /data/adb/modules -- see the note above about root scripts.
    got, _ = su(serial, f"sha256sum {mdir}/module.prop {mdir}/service.sh")
    have = {os.path.basename(p[1]): p[0].lower()
            for p in (ln.split() for ln in got.splitlines()) if len(p) == 2}
    bad = [f for f in FILES if have.get(f) != want[f]]
    for f in FILES:
        log(f"  {'OK  ' if have.get(f) == want[f] else 'FAIL'} {mdir}/{f}  "
            f"{have.get(f, '(missing)')[:16]}")
    if bad:
        sys.exit(f"{modid}: {bad} on the device do NOT match what was sent -- refusing to "
                 f"leave a half-written root boot script in place.")
    log(f"  module '{modid}' placed + verified in {mdir}")
    return mdir
