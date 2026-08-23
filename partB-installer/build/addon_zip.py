#!/usr/bin/env python3
"""Pick the newest of several `<name>-<version>.zip` files, by version.

Two places choose an addon zip and neither may choose the wrong one:

  installer/deploy_toolbox_addon.py   the zip unpacked into Kodi's addon tree on the box
  make_dist.py                        the zip copied into the shipped dist bundle

Both used `sorted(glob(...))[-1]`, which is a STRING sort. build_toolbox_zip.py names the
zip from addon.xml's <addon version>, so the trailing field is a dotted release number, and
as text "1.1.10" sorts before "1.1.2". The first release after 1.1.9 would start selecting
the older zip, and every release after it would too -- silently, since neither caller reads
the version again afterwards and Kodi reports the addon installed either way.

Lives in build/ rather than beside either caller so there is exactly one copy of the rule:
both callers already have build/ on sys.path.
"""
import os


def version_key(path):
    """Order `<name>-<version>.zip` by version. Numeric parts compare numerically.

    Anything non-numeric (a "-beta" suffix, say) sorts after any number in the same
    position, so a prerelease never outranks the release it precedes.
    """
    stem = os.path.basename(path)
    if stem.endswith(".zip"):
        stem = stem[:-len(".zip")]
    version = stem.rsplit("-", 1)[-1] if "-" in stem else stem
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in version.split(".")]


def newest(paths):
    """The highest-version path, or None if there are none."""
    paths = list(paths)
    return max(paths, key=version_key) if paths else None
