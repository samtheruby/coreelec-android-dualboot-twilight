#!/usr/bin/env python3
"""
Bundle integrity: check a shipped file against the dist's SHA256SUMS.txt.

make_dist writes SHA256SUMS.txt into every dist bundle, covering every file it ships
-- the Magisk-patched init_boot, the GPT blobs, the CoreELEC filesystem images. This
module is the ONLY thing that proves the file on disk is the file we shipped, and it
must run BEFORE the first write.

It is not redundant with the installer's own SHA-256 read-back. That read-back proves
"what landed on the eMMC == the file on the PC" -- so a truncated unzip or a
half-copied image is written to a boot-critical region and then cheerfully verified
against its own corruption. This check is the other half: "the file on the PC == the
file that was built". Both are needed.

No-ops (returns, does not fail) when the bundle ships no manifest, or when a file is
not listed in it -- a source checkout, or an init_boot the user patched themselves.
That is a real gap, not a hidden one: the manifest can only vouch for what shipped in
the bundle. Callers say how many files were actually checked so a run can print it.
"""
import hashlib, os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SUMS = os.path.join(ROOT, "SHA256SUMS.txt")


def _manifest():
    """{path-relative-to-bundle-root: expected-sha256}; {} when there is no manifest."""
    if not os.path.exists(SUMS):
        return {}
    want = {}
    with open(SUMS, encoding="utf-8", errors="replace") as f:
        for ln in f:
            p = ln.split()
            if len(p) == 2:
                # sha256sum marks a binary file as '*path'
                want[p[1].lstrip("*").replace("\\", "/")] = p[0].lower()
    return want


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(paths, log=print):
    """SHA-256 every listed path against the manifest. sys.exit on ANY mismatch.

    Returns the number of files actually checked -- 0 means "nothing could be
    checked" (no manifest, or none of these paths shipped in it), NOT "all good".
    """
    want = _manifest()
    if not want:
        return 0
    checked = 0
    for path in ([paths] if isinstance(paths, str) else paths):
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        exp = want.get(rel)
        if exp is None:                      # not shipped in this bundle -- nothing to say
            continue
        got = _sha256(path)
        if got != exp:
            sys.exit(f"SHA-256 MISMATCH for {rel}\n"
                     f"  SHA256SUMS.txt says: {exp}\n"
                     f"  the file on disk is: {got}\n"
                     f"The bundle is corrupt or the file was modified. REFUSING to flash it "
                     f"-- re-download / re-extract the bundle.")
        checked += 1
        if log:
            log(f"  sha256 OK  {rel}")
    return checked
