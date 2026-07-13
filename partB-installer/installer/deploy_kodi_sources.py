#!/usr/bin/env python3
"""
Add Kodi file-manager download sources to a RUNNING CoreELEC (over SSH) -- the
same thing as Settings > File Manager > Add Source, but scripted.

Adds (idempotent, dedup by path):
  PM4K        https://pm4k.eu/                                  (Plex / PM4K build)
  jamal2362   https://ce-repo.github.io/repository.jamal2362/   (TinyPPI repo)

These land in /storage/.kodi/userdata/sources.xml under <files>, so "Install from
zip file" can reach them. Kodi rewrites sources.xml from memory on shutdown, so we
stop Kodi, edit, then start it -- otherwise the edit is clobbered and never appears.

sources.xml is the USER'S file: every source they ever added by hand lives in it. We
only ever ADD to it, and we start from an empty skeleton ONLY when the file genuinely
does not exist yet. Anything else -- a read error, a file we cannot parse -- aborts
without writing, because the alternative is replacing all of their sources with our
two. (That is exactly what the old `except Exception: root = skeleton()` did: any
failure at all, including a transient SFTP hiccup, silently wiped the lot.) The file
is also backed up on the box before it is rewritten.

  python deploy_kodi_sources.py --host <coreelec-ip> [--pass coreelec]

Needs paramiko (pip install paramiko).
"""
import argparse, sys
import xml.etree.ElementTree as ET

SOURCES_PATH = "/storage/.kodi/userdata/sources.xml"
BACKUP_PATH = SOURCES_PATH + ".pre-dualboot.bak"
SECTIONS = ["programs", "video", "music", "pictures", "files", "games"]

# (name, url) -- url normalized to a trailing slash below
WANT = [
    ("PM4K", "https://pm4k.eu/"),
    ("jamal2362", "https://ce-repo.github.io/repository.jamal2362/"),
]


def norm(u):
    return u if u.endswith("/") else u + "/"


def skeleton():
    root = ET.Element("sources")
    for s in SECTIONS:
        sec = ET.SubElement(root, s)
        ET.SubElement(sec, "default", {"pathversion": "1"})
    return root


def ensure_files(root):
    files = root.find("files")
    if files is None:
        files = ET.SubElement(root, "files")
        ET.SubElement(files, "default", {"pathversion": "1"})
    return files


def existing_paths(files):
    out = set()
    for src in files.findall("source"):
        p = src.find("path")
        if p is not None and p.text:
            out.add(norm(p.text.strip()))
    return out


def add_source(files, name, url):
    src = ET.SubElement(files, "source")
    ET.SubElement(src, "name").text = name
    path = ET.SubElement(src, "path", {"pathversion": "1"})
    path.text = url
    ET.SubElement(src, "allowsharing").text = "true"


def read_sources(sftp, log=print):
    """The box's existing sources.xml as an ElementTree, or a fresh skeleton if -- and
    ONLY if -- the file does not exist yet. Raises SystemExit on anything else.

    The distinction is the whole point. A missing file means a Kodi that has never had a
    source added: a skeleton is correct. A file we cannot READ or PARSE means we do not
    know what the user has, and writing our skeleton over it would delete every source
    they own. Refuse instead."""
    try:
        with sftp.open(SOURCES_PATH, "r") as f:
            raw = f.read()
    except IOError:
        log(f"  no {SOURCES_PATH} yet -- starting a fresh one")
        return skeleton(), None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        sys.exit(f"{SOURCES_PATH} exists but does not parse as XML ({e}).\n"
                 f"REFUSING to touch it -- overwriting would delete every Kodi source on "
                 f"this box. Fix or move the file aside, then re-run.")
    if root.tag != "sources":
        sys.exit(f"{SOURCES_PATH} has root <{root.tag}>, expected <sources>. REFUSING to "
                 f"overwrite a file this script does not understand.")
    return root, raw


def main():
    ap = argparse.ArgumentParser()
    # No default: this SSHes in as root and rewrites /storage. A default IP means a bare
    # run reaches out to whatever machine happens to hold it on the user's LAN.
    ap.add_argument("--host", required=True, help="CoreELEC IP/hostname")
    ap.add_argument("--user", default="root")
    ap.add_argument("--pass", dest="pw", default="coreelec")
    a = ap.parse_args()
    try:
        import paramiko
    except ImportError:
        sys.exit("paramiko not installed -- pip install paramiko")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(a.host, username=a.user, password=a.pw, timeout=15,
                look_for_keys=False, allow_agent=False)

    def sh(cmd):
        _, o, e = cli.exec_command(cmd, timeout=60)
        out = o.read().decode(errors="replace")
        rc = o.channel.recv_exit_status()
        return rc, out + e.read().decode(errors="replace")

    # stop Kodi so it can't clobber our edit on shutdown
    rc, out = sh("systemctl stop kodi")
    if rc != 0:
        cli.close()
        sys.exit(f"could not stop Kodi (rc={rc}): {out.strip()}\nRefusing to edit "
                 f"sources.xml while Kodi is running -- it would be overwritten on exit.")
    sftp = cli.open_sftp()

    root, raw = read_sources(sftp)
    files = ensure_files(root)
    have = existing_paths(files)
    added = []
    for name, url in WANT:
        url = norm(url)
        if url in have:
            print(f"  exists: {name}  {url}")
        else:
            add_source(files, name, url)
            have.add(url)
            added.append((name, url))
            print(f"  added : {name}  {url}")

    if added:
        try:
            ET.indent(root, space="    ")   # py3.9+
        except Exception:
            pass
        data = ET.tostring(root, encoding="utf-8")

        # Keep the user's original beside the new one. sources.xml is hand-curated and
        # nothing else on the box backs it up.
        if raw is not None:
            with sftp.open(BACKUP_PATH, "w") as f:
                f.write(raw)
            print(f"  backed up the original -> {BACKUP_PATH}")

        sh(f"mkdir -p $(dirname {SOURCES_PATH})")
        with sftp.open(SOURCES_PATH, "w") as f:
            f.write(data)

        # Read it back: it must parse, and it must still contain every source that was
        # there before plus the ones we added. A truncated SFTP write would otherwise
        # only surface as a Kodi with no sources.
        with sftp.open(SOURCES_PATH, "r") as f:
            back = f.read()
        try:
            got = existing_paths(ensure_files(ET.fromstring(back)))
        except ET.ParseError as e:
            sftp.close(); cli.close()
            sys.exit(f"sources.xml did not survive the write ({e}). The original is at "
                     f"{BACKUP_PATH} on the box -- restore it before starting Kodi.")
        missing = have - got
        if missing:
            sftp.close(); cli.close()
            sys.exit(f"sources.xml is missing {sorted(missing)} after the write. The "
                     f"original is at {BACKUP_PATH} on the box -- restore it.")
        print(f"  verified: {len(got)} source(s) present after the write")

    sftp.close()
    rc, out = sh("systemctl start kodi")
    cli.close()
    if rc != 0:
        sys.exit(f"Kodi did not restart (rc={rc}): {out.strip()}")
    print(f"OK -- {len(added)} source(s) added, Kodi restarted."
          if added else "OK -- nothing to add (both sources already present); Kodi restarted.")


if __name__ == "__main__":
    main()
