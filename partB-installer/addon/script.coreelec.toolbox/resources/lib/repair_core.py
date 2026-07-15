# repair_core.py -- pure detection + fix-planning for the Toolbox repair suite.
# NO xbmc import: importable by tests, runs outside Kodi.
import hashlib

try:
    from resources.lib import envcodec  # Kodi: loaded as resources.lib.repair_core
except ImportError:
    import envcodec  # tests / standalone: resources/lib on sys.path

OK, NEEDS_FIX, UNKNOWN, NOT_APPLICABLE = "OK", "NEEDS_FIX", "UNKNOWN", "NOT_APPLICABLE"


class CheckResult:
    def __init__(self, id, label, status, detail="", reboot=False):
        self.id, self.label, self.status, self.detail, self.reboot = id, label, status, detail, reboot

    def __eq__(self, o):
        return isinstance(o, CheckResult) and (
            self.id, self.status, self.detail, self.reboot) == (o.id, o.status, o.detail, o.reboot)

    def __repr__(self):
        return f"CheckResult({self.id!r}, {self.status!r}, {self.detail!r}, reboot={self.reboot})"


def _env_ok(b):
    return bool(b) and len(b) >= envcodec.ENV_SIZE and envcodec.crc_ok(b)


def check_boot_gate(env_bytes, env_dualboot_bytes):
    if not _env_ok(env_bytes):
        return CheckResult("boot_gate", "Boot gate", UNKNOWN, "env unreadable or bad CRC")
    d = envcodec.parse(env_bytes)
    slot = envcodec.detect_ce_slot(d)
    if not slot:
        return CheckResult("boot_gate", "Boot gate", NOT_APPLICABLE, "not a dual-boot install")
    want = envcodec.gate_vars(slot, envcodec.detect_default(d))
    if d.get("bootcefromemmc") != want["bootcefromemmc"]:
        return CheckResult("boot_gate", "Boot gate", NEEDS_FIX, "display/HDMI gate stale", True)
    if d.get("bootcmd") != want["bootcmd"]:
        return CheckResult("boot_gate", "Boot gate", NEEDS_FIX, "boot_ce gate stale", True)
    if not (_env_ok(env_dualboot_bytes)
            and envcodec.parse(env_dualboot_bytes).get("bootcefromemmc") == want["bootcefromemmc"]):
        return CheckResult("boot_gate", "Boot gate", NEEDS_FIX, "update-restore image stale", True)
    return CheckResult("boot_gate", "Boot gate", OK, "current")


def check_file(id, label, on_disk_bytes, canonical_bytes):
    if on_disk_bytes is None:
        return CheckResult(id, label, NEEDS_FIX, "missing", True)
    if hashlib.sha256(on_disk_bytes).digest() == hashlib.sha256(canonical_bytes).digest():
        return CheckResult(id, label, OK, "current")
    return CheckResult(id, label, NEEDS_FIX, "differs from bundled", True)


def build_fixed_env(env_bytes):
    """Return (live_env_bytes, env_dualboot_bytes) with the current gate applied.
    Preserves the box's boot default. Raises ValueError on bad/gateless env."""
    if not _env_ok(env_bytes):
        raise ValueError("refusing to fix an env with a bad CRC")
    d = envcodec.parse(env_bytes)
    slot = envcodec.detect_ce_slot(d)
    if not slot:
        raise ValueError("no CoreELEC gate in env")
    default = envcodec.detect_default(d)
    d.update(envcodec.gate_vars(slot, default))
    live = envcodec.serialize(d)
    dual_d = dict(d)
    if default == "android":
        dual_d["boot_ce"] = "1"
    dual = envcodec.serialize(dual_d)
    if not (envcodec.crc_ok(live) and envcodec.crc_ok(dual)):
        raise ValueError("internal CRC error building fixed env")
    return live, dual
