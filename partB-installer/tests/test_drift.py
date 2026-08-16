#!/usr/bin/env python3
"""Drift tests for values written in more than one place, and for constructs known to be wrong.

Dependency-free on purpose: run it directly.

    python tests/test_drift.py

Same argument as test_boot_gate.py, applied to the rest of the tree. A value that appears
in two files, in two languages, with nothing forcing them to agree, drifts -- and here the
drift is silent, because both halves keep running and only one of them is right.

The `pm path` check is a different shape: not two copies of a value, but a construct that
looked correct and was not. It shipped, and PR #9 found it in the field on a Xiaomi TV
Box S 3rd Gen, where stage2a printed "nothing to block" and returned success while leaving
the Xiaomi OTA updater fully enabled. Once a defect has cost a real box its protection,
the pattern is worth a permanent gate rather than a memory.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

INSTALLER = os.path.join(ROOT, "installer")
MODULES = os.path.join(ROOT, "modules")
ARTIFACTS = os.path.join(ROOT, "artifacts")
ADDON_XML = os.path.join(ROOT, "addon", "script.coreelec.toolbox", "addon.xml")

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


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def installer_sources():
    return sorted(f for f in os.listdir(INSTALLER) if f.endswith(".py"))


# install_blockgms.py gates on `GMS not in pm path ...` and is deliberately NOT covered by
# the check below: the maintainer confirmed the substring form is fine for GMS specifically.
# Scoped by filename rather than skipped globally, so the gate still holds everywhere else.
PM_PATH_EXEMPT = {"install_blockgms.py"}


# --- 1. `pm path` must never be tested by substring ---------------------------------------
def pm_path_is_not_tested_by_substring():
    """`pm path <pkg>` prints the APK's LOCATION, not the package name.

    On a unit whose system directory is not named after the package -- e.g.
    /system/priv-app/updateservice/updateservice.apk for com.xiaomi.mitv.updateservice --
    `PKG in output` is false even though the package is installed, so a presence gate
    written that way takes the "not installed" branch on a device that very much has it.

    The correct signals are emptiness (pm path prints nothing for a missing package) or
    the exit status, which is what modules/blockota/service.sh already uses.
    """
    bad = []
    # `PKG not in <anything> pm path` and the positive `PKG in <anything> pm path`
    pattern = re.compile(r"^(?P<line>.*\b(?P<var>\w+)\s+(?:not\s+)?in\b.*pm path.*)$", re.M)
    for name in installer_sources():
        if name in PM_PATH_EXEMPT:
            continue
        src = read(INSTALLER, name)
        for m in pattern.finditer(src):
            lineno = src[:m.start()].count("\n") + 1
            bad.append(f"{name}:{lineno}: {m.group('line').strip()}")
    assert not bad, (
        "`pm path` output tested by substring containment -- it prints the APK path, not "
        "the package name, so this is false on any unit whose system dir is not named "
        "after the package:\n    " + "\n    ".join(bad)
        + "\n    Test for empty output, or for the exit status, instead.")


# --- 2. each installer and its Magisk module must act on the same things -------------------
def py_literal(path, name):
    """The value assigned to a module-level name, read with ast rather than regex.

    A regex over source would happily match the name inside a docstring or a comment; ast
    only sees the assignment that Python itself would execute.
    """
    tree = ast.parse(read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level {name} = ... in {os.path.basename(path)}")


def sh_var(path, name):
    """A shell scalar or newline-separated list assigned as NAME="..." -- as a list."""
    src = read(path)
    m = re.search(rf'^{name}="([^"]*)"', src, re.M)
    assert m, f"no {name}=\"...\" in {os.path.basename(path)}"
    return [w for w in m.group(1).split() if w]


def blockgms_components_agree():
    """The 8 .update.* components are listed in Python and again in shell.

    install_blockgms.py applies the durable disable from adb; modules/blockgms/service.sh
    re-asserts it at every boot. Drop a component from one list only and it is disabled
    once and then never again -- or re-asserted forever but never actually disabled. Both
    halves keep running either way, so nothing surfaces.
    """
    py = py_literal(os.path.join(INSTALLER, "install_blockgms.py"), "COMPS")
    sh = sh_var(os.path.join(MODULES, "blockgms", "service.sh"), "COMPONENTS")
    assert set(py) == set(sh), (
        "install_blockgms.py COMPS and modules/blockgms/service.sh COMPONENTS disagree:\n"
        f"    only in installer: {sorted(set(py) - set(sh)) or 'none'}\n"
        f"    only in module   : {sorted(set(sh) - set(py)) or 'none'}")


def blockota_package_agrees():
    """The Xiaomi updater package name, in the installer and in the boot-time module.

    modules/blockota/service.sh is the only thing that puts the block back if something
    re-enables the updater. Pointed at a package the installer never disabled, it re-asserts
    nothing, and its log still reads as a healthy boot.
    """
    py = py_literal(os.path.join(INSTALLER, "install_blockota.py"), "PKG")
    sh = sh_var(os.path.join(MODULES, "blockota", "service.sh"), "PKGS")
    assert [py] == sh, (
        f"install_blockota.py PKG={py!r} but modules/blockota/service.sh PKGS={sh!r}")


# --- 3. MODID must match the id Magisk actually reads ------------------------------------
def modid_matches_module_prop():
    """Each installer's MODID is the directory it installs into; module.prop's id is what
    Magisk calls the module. They are written independently, in two file formats.

    A mismatch installs the files under one name while Magisk registers another, so
    --verify reads an empty module dir and the uninstall instructions printed to the user
    point at a path that does not exist.
    """
    bad, pairs = [], 0
    for name in installer_sources():
        src = read(INSTALLER, name)
        m_id = re.search(r'^MODID\s*=\s*"([^"]+)"', src, re.M)
        if not m_id:
            continue
        m_dir = re.search(r'"modules",\s*"([^"]+)"', src)
        assert m_dir, f"{name} sets MODID but no modules/<dir> source could be found"
        prop = os.path.join(MODULES, m_dir.group(1), "module.prop")
        assert os.path.exists(prop), f"{name} points at {prop}, which does not exist"
        m_prop = re.search(r"^id=(.+)$", read(prop), re.M)
        assert m_prop, f"{prop} has no id= line"
        pairs += 1
        if m_id.group(1) != m_prop.group(1).strip():
            bad.append(f"{name}: MODID={m_id.group(1)!r} but "
                       f"modules/{m_dir.group(1)}/module.prop id={m_prop.group(1).strip()!r}")
    # Without this the check passes vacuously the day the MODID regex stops matching.
    modules = [d for d in os.listdir(MODULES) if os.path.isdir(os.path.join(MODULES, d))]
    assert pairs == len(modules), (
        f"found {pairs} installer/module pair(s) but modules/ holds {len(modules)}: "
        f"{sorted(modules)} -- a module with no installer, or a MODID this check missed")
    assert not bad, "installer MODID and module.prop id disagree:\n    " + "\n    ".join(bad)


# --- 4. the shipped addon zip must be the version addon.xml declares ----------------------
def shipped_addon_zip_matches_addon_xml():
    """build_toolbox_zip.py names the zip after addon.xml's <addon version>, and
    deploy_toolbox_addon.py installs whatever zip it finds in artifacts/.

    Nothing runs the build. Bump addon.xml, commit, and stage3 keeps deploying the previous
    release -- with the new version number nowhere in sight, so the box looks up to date in
    Kodi's addon list while running the old code.
    """
    xml = read(ADDON_XML)
    m = re.search(r'<addon\b[^>]*\bversion="([^"]+)"', xml, re.S)
    assert m, "no <addon ... version=\"...\"> in addon.xml"
    m_id = re.search(r'<addon\b[^>]*\bid="([^"]+)"', xml, re.S)
    assert m_id, "no <addon ... id=\"...\"> in addon.xml"
    want = f"{m_id.group(1)}-{m.group(1)}.zip"
    have = sorted(f for f in os.listdir(ARTIFACTS)
                  if f.startswith(m_id.group(1) + "-") and f.endswith(".zip"))
    assert want in have, (
        f"addon.xml declares version {m.group(1)}, so artifacts/ should hold {want}, "
        f"but holds: {have or 'no addon zip at all'}\n"
        "    Rebuild it: python build/build_toolbox_zip.py")


if __name__ == "__main__":
    print("drift -- values written twice, and constructs known to be wrong")
    check("no installer gates on `pm path` by substring", pm_path_is_not_tested_by_substring)
    check("blockgms component list agrees (installer vs module)", blockgms_components_agree)
    check("blockota package agrees (installer vs module)", blockota_package_agrees)
    check("MODID matches module.prop id (every module)", modid_matches_module_prop)
    check("shipped addon zip matches addon.xml version", shipped_addon_zip_matches_addon_xml)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all drift checks passed")
