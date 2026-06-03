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

  python deploy_kodi_sources.py --host 192.168.1.195 [--pass coreelec]

Needs paramiko (pip install paramiko).
"""
import argparse, sys
import xml.etree.ElementTree as ET

SOURCES_PATH = "/storage/.kodi/userdata/sources.xml"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.195")
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
        _, o, _ = cli.exec_command(cmd, timeout=60)
        o.channel.recv_exit_status()
        return o.read().decode(errors="replace")

    # stop Kodi so it can't clobber our edit on shutdown
    sh("systemctl stop kodi")
    sftp = cli.open_sftp()

    # read existing sources.xml (or start from a skeleton)
    try:
        with sftp.open(SOURCES_PATH, "r") as f:
            raw = f.read()
        root = ET.fromstring(raw)
        if root.tag != "sources":
            raise ValueError("unexpected root")
    except Exception:
        root = skeleton()

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
        sh(f"mkdir -p $(dirname {SOURCES_PATH})")
        with sftp.open(SOURCES_PATH, "w") as f:
            f.write(data)
    sftp.close()
    sh("systemctl start kodi")
    cli.close()
    print(f"OK -- {len(added)} source(s) added, Kodi restarted."
          if added else "OK -- nothing to add (both sources already present); Kodi restarted.")


if __name__ == "__main__":
    main()
