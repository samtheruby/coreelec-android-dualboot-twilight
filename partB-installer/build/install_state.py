#!/usr/bin/env python3
"""
What the PC remembers about an install, BETWEEN stages.

One fact so far, and it is here because losing it is a real bug: the boot DEFAULT
(--default android|coreelec) the user chose at stage1.

stage1's reboot runs a recovery factory-reset, and on this SoC that resets the u-boot env
to stock -- so by the time stage2 runs, the DEVICE no longer knows which OS the user chose
to boot by default. stage2 rebuilds the gate from scratch, and with nothing to tell it
otherwise it rebuilds it as 'android'. A user who installed with `--default coreelec` and
then ran the very stage2 command stage1 printed for them would silently end up with an
android-default box. (The same trap was already fixed once, in finish_install -- see the
warning in its --default help.) The choice only ever existed on the command line, so the
PC is the only place that can still hold it.

Stored PER DEVICE, beside that unit's pulled backups: two units installed from one PC must
not overwrite each other's choice. Absent state is not an error -- it just means "nobody
recorded one", and the caller falls back to its own default.
"""
import json, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def path_for(dev):
    return os.path.join(ROOT, "pulled_backups", dev.slug, "install_state.json")


def load(dev):
    """The recorded state for `dev`, or {} if there is none / it is unreadable."""
    try:
        with open(path_for(dev), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save(dev, **fields):
    """Merge `fields` into this device's recorded state. Returns the file path."""
    p = path_for(dev)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    state = load(dev)
    state.update(fields)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    return p
