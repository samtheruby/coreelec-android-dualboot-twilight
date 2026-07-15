# repair.py -- "Repair dual-boot install": scan the box, fix only what is stale.
# Detection lives in repair_core (pure). This layer does dialogs + device writes.
import os
import subprocess

import xbmc
import xbmcgui
import xbmcaddon

from resources.lib import envcodec, repair_core as rc

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")
REPAIR_DIR = os.path.join(ADDON.getAddonInfo("path"), "resources", "repair")

FLASH = "/flash"
ENV_DUALBOOT = f"{FLASH}/env_dualboot.bin"
DOVI = f"{FLASH}/dovi.ko"
HOOK = f"{FLASH}/user-update.sh"


def log(m):
    xbmc.log(f"[{NAME}] repair: {m}", xbmc.LOGINFO)


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _bundled(name):
    with open(os.path.join(REPAIR_DIR, name), "rb") as f:
        return f.read()


def scan():
    """Return a list of CheckResult. Side-effect free."""
    env = _read("/dev/env")
    results = [rc.check_boot_gate(env, _read(ENV_DUALBOOT))]
    results.append(rc.check_file("dovi_ko", "Dolby Vision module (dovi.ko)",
                                 _read(DOVI), _bundled("dovi.ko")))
    results.append(rc.check_file("update_hook", "CoreELEC update hook",
                                 _read(HOOK), _bundled("user-update.sh")))
    return results


def _summary(results):
    lines = []
    for r in results:
        tag = {rc.OK: "OK", rc.NEEDS_FIX: "NEEDS FIX", rc.UNKNOWN: "UNKNOWN",
               rc.NOT_APPLICABLE: "n/a"}[r.status]
        lines.append(f"{r.label} ... [B]{tag}[/B]" + (f" ({r.detail})" if r.detail else ""))
    return "\n".join(lines)


def _remount(mode):
    subprocess.run(["mount", "-o", f"remount,{mode}", FLASH], check=False)


def _fix_boot_gate():
    env = envcodec.read_env()
    live, dual = rc.build_fixed_env(env)
    envcodec.write_env(live)                 # raw /dev/env, fail-closed read-back
    if os.path.isdir(FLASH):
        _remount("rw")
        try:
            with open(ENV_DUALBOOT, "wb") as f:
                f.write(dual)
            subprocess.run(["sync"], check=False)
        finally:
            _remount("ro")


def _fix_file(dst, bundled_name, mode=None):
    data = _bundled(bundled_name)
    _remount("rw")
    try:
        with open(dst, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(dst, mode)
        subprocess.run(["sync"], check=False)
    finally:
        _remount("ro")
    if _read(dst) != data:
        raise IOError(f"{dst} did not match after write")


FIXERS = {
    "boot_gate": _fix_boot_gate,
    "dovi_ko": lambda: _fix_file(DOVI, "dovi.ko"),
    "update_hook": lambda: _fix_file(HOOK, "user-update.sh", 0o755),
}


def run():
    dlg = xbmcgui.Dialog()
    results = scan()
    todo = [r for r in results if r.status == rc.NEEDS_FIX]
    if not todo:
        dlg.ok(NAME, "[B]Everything looks correct[/B] -- nothing to repair.\n\n" + _summary(results))
        return
    if not dlg.yesno(NAME, _summary(results) + f"\n\n[B]Fix {len(todo)} issue(s)?[/B]\n"
                     "Your default boot OS is kept.", yeslabel=f"Fix {len(todo)}", nolabel="Cancel"):
        return

    done, failed, reboot = [], [], False
    for r in todo:
        try:
            FIXERS[r.id]()
            done.append(r.label)
            reboot = reboot or r.reboot
            log(f"fixed {r.id}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{r.label}: {e}")
            log(f"FAILED {r.id}: {e}")

    msg = ""
    if done:
        msg += "[B]Fixed:[/B]\n" + "\n".join(f"- {d}" for d in done) + "\n\n"
    if failed:
        msg += "[B]Failed:[/B]\n" + "\n".join(f"- {f}" for f in failed) + "\n\n"
    if reboot and not failed:
        msg += "[B]Reboot for the changes to take effect.[/B]"
    dlg.ok(NAME, msg.strip())
