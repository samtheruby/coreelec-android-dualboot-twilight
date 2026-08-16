#!/usr/bin/env python3
"""Syntax gates for every tracked source file -- Python, shell, XML.

Dependency-free on purpose: run it directly.

    python tests/test_syntax.py

Why this exists at all, when "it's just a syntax error, you'd notice":

Most of installer/ is never imported. install.py runs each stage as a SUBPROCESS
(`run("restore_stock_gpt.py")`), so a syntax error in a stage script is invisible until
that stage runs -- and the stages you would least like to discover this way are the
recovery ones. `restore_stock_gpt.py` and `restore_env_misc_factory.py` exist to put a
half-installed box back; finding out they do not parse, at the moment you need them, is
the worst possible time.

Same argument for the other two languages. The three modules/*/service.sh run as ROOT on
every boot -- magisk_module.py calls service.sh "the most privileged thing this installer
puts on the device" and hash-verifies it for exactly that reason, but a verified hash of a
broken script is still a broken script. And the XML (Kodi skin, AndroidManifest, addon.xml,
string resources) is parsed on the device, where a malformed file is a silent no-show
rather than an error you see.

Shell is checked with the interpreter its OWN shebang names. Checking everything with one
shell is wrong in both directions: `sh -n build/build_ce_flash.sh` reports a syntax error
on its bash arrays, while `bash -n modules/blockota/service.sh` happily accepts bashisms
that Android's /system/bin/sh would reject at boot.
"""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(ROOT, ".."))

# Generated / vendored trees to skip when git is not available to tell us what is tracked.
SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".gradle", ".idea", "pulled_backups",
             "pulled_backups_prerestore", "recon", "tools"}

_FAILURES = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        _FAILURES.append(f"{name}: {e}")
        print(f"  FAIL  {name}: {e}")
    except Exception as e:                                       # noqa: BLE001
        _FAILURES.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
    else:
        print(f"  ok    {name}")


def tracked_files(ext):
    """Every tracked file with this extension, as absolute paths.

    Prefer `git ls-files` -- it is the only source that agrees exactly with what CI will
    check out, and this repo's .gitignore deliberately excludes large generated artifacts
    that are present in a working tree. Fall back to a filtered walk (source tarball, no
    git) rather than silently checking nothing.
    """
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files", f"*{ext}"],
                             capture_output=True, text=True, check=True).stdout
        paths = [os.path.join(REPO, p) for p in out.split("\n") if p.strip()]
        if paths:
            return sorted(paths)
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found += [os.path.join(base, f) for f in files if f.endswith(ext)]
    return sorted(found)


def rel(p):
    return os.path.relpath(p, REPO)


def shebang(path):
    with open(path, "rb") as fh:
        first = fh.readline(200).decode("utf-8", "replace").strip()
    return first if first.startswith("#!") else ""


def python_files_compile():
    files = tracked_files(".py")
    assert files, "no Python files found -- the file discovery is broken, not the sources"
    bad = []
    for p in files:
        r = subprocess.run([sys.executable, "-m", "py_compile", p], capture_output=True,
                           text=True)
        if r.returncode != 0:
            bad.append(f"{rel(p)}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'failed'}")
    assert not bad, f"{len(bad)} file(s) do not compile:\n    " + "\n    ".join(bad)
    print(f"        ({len(files)} Python files)")


def shell_files_parse():
    """Each script checked by the interpreter its shebang names.

    /system/bin/sh on Android is mksh; dash is the closest POSIX-strict checker generally
    installed, and is stricter than mksh, so it will not wave through a bashism that the
    device would reject. Being stricter than the target is the safe direction here.
    """
    files = tracked_files(".sh")
    assert files, "no shell scripts found -- the file discovery is broken"
    bad, unknown = [], []
    for p in files:
        sb = shebang(p)
        if "bash" in sb:
            checker = ["bash", "-n"]
        elif sb.endswith("/sh") or sb.endswith("sh"):
            checker = ["sh", "-n"]
        else:
            unknown.append(f"{rel(p)}: shebang {sb!r}")
            continue
        r = subprocess.run(checker + [p], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{rel(p)} ({checker[0]} -n): {r.stderr.strip()}")
    assert not unknown, ("script(s) with no recognised shebang -- add one, or teach this "
                         "test the interpreter:\n    " + "\n    ".join(unknown))
    assert not bad, f"{len(bad)} shell script(s) fail syntax check:\n    " + "\n    ".join(bad)
    print(f"        ({len(files)} shell scripts)")


def xml_files_are_well_formed():
    files = tracked_files(".xml")
    assert files, "no XML files found -- the file discovery is broken"
    bad = []
    for p in files:
        try:
            ET.parse(p)
        except ET.ParseError as e:
            bad.append(f"{rel(p)}: {e}")
    assert not bad, f"{len(bad)} XML file(s) are malformed:\n    " + "\n    ".join(bad)
    print(f"        ({len(files)} XML files)")


if __name__ == "__main__":
    print("syntax gates -- every tracked source file parses")
    check("all tracked Python compiles", python_files_compile)
    check("all tracked shell parses (per its own shebang)", shell_files_parse)
    check("all tracked XML is well-formed", xml_files_are_well_formed)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all syntax gates passed")
