#!/usr/bin/env python3
"""What the PC remembers between stages, per device.

Dependency-free on purpose: run it directly.

    python tests/test_install_state.py

install_state holds one fact -- the boot default the user chose at stage1 -- and its own
docstring records why: stage1's reboot factory-resets the u-boot env, so by stage2 the
DEVICE no longer knows the choice, and stage2 rebuilds the gate as 'android' if nothing
tells it otherwise. A user who installed with --default coreelec, then ran the very stage2
command stage1 printed for them, ended up with an android-default box and no error.

That is the bug this module exists to prevent, and it had no tests. The two properties that
prevent it are that a saved value survives a reload, and that saving one field does not
drop another. The third is isolation: two units installed from one PC must not overwrite
each other's choice.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))

import install_state  # noqa: E402

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


class Dev:
    """Just enough of a device: install_state only ever reads .slug."""

    def __init__(self, slug):
        self.slug = slug


class TempRoot:
    """Redirect install_state's module-level ROOT at a throwaway tree."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="install_state_")
        self._root = install_state.ROOT
        install_state.ROOT = self.dir
        return self

    def __exit__(self, *exc):
        install_state.ROOT = self._root
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def absent_state_is_empty_not_an_error():
    """Nobody recorded a choice is a normal outcome -- the caller falls back to its own
    default. It must not raise, and must not invent a value."""
    with TempRoot():
        assert install_state.load(Dev("stick")) == {}


def a_saved_choice_survives_a_reload():
    """The whole point: stage1 writes it, a reboot happens, stage2 reads it back."""
    with TempRoot():
        dev = Dev("box")
        install_state.save(dev, default="coreelec")
        assert install_state.load(dev).get("default") == "coreelec"


def saving_one_field_keeps_the_others():
    """save() merges. A later stage recording something else must not drop the boot
    default -- losing it is the exact silent bug this module was added to prevent."""
    with TempRoot():
        dev = Dev("box")
        install_state.save(dev, default="coreelec")
        install_state.save(dev, ce_slot="_b")
        state = install_state.load(dev)
        assert state.get("default") == "coreelec", f"boot default was lost: {state}"
        assert state.get("ce_slot") == "_b", state


def two_devices_do_not_share_state():
    """Two units installed from one PC. Each keeps its own choice."""
    with TempRoot():
        stick, box = Dev("stick"), Dev("box")
        install_state.save(stick, default="coreelec")
        install_state.save(box, default="android")
        assert install_state.load(stick).get("default") == "coreelec"
        assert install_state.load(box).get("default") == "android"


def corrupt_json_reads_as_empty():
    """A half-written file must not crash stage2 -- absent and unreadable are the same
    answer here: nobody recorded one."""
    with TempRoot():
        dev = Dev("stick")
        p = install_state.path_for(dev)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert install_state.load(dev) == {}


def a_non_dict_document_reads_as_empty():
    """Valid JSON that is not an object (a list, say) would break .get() downstream."""
    with TempRoot():
        dev = Dev("stick")
        p = install_state.path_for(dev)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(["coreelec"], fh)
        assert install_state.load(dev) == {}


def save_returns_a_path_that_exists():
    with TempRoot():
        p = install_state.save(Dev("box"), default="android")
        assert os.path.exists(p), f"save() returned {p}, which does not exist"


if __name__ == "__main__":
    print("install_state -- the boot default the PC remembers between stages")
    check("absent state is empty, not an error", absent_state_is_empty_not_an_error)
    check("a saved choice survives a reload", a_saved_choice_survives_a_reload)
    check("saving one field keeps the others", saving_one_field_keeps_the_others)
    check("two devices do not share state", two_devices_do_not_share_state)
    check("corrupt json reads as empty", corrupt_json_reads_as_empty)
    check("a non-dict document reads as empty", a_non_dict_document_reads_as_empty)
    check("save returns a path that exists", save_returns_a_path_that_exists)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all install_state checks passed")
