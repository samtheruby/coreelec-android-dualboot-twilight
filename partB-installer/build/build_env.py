#!/usr/bin/env python3
"""
Env handling for the installer.

Strategy (decided after finding identity baked into the live env):
  We do NOT ship a verbatim env -- the working unit's env contains its serial,
  MAC (did_key), assm_sn, cpu_id, etc. Instead the installer reads the TARGET's
  OWN env from p2 and ADDS only:
    (a) the boot_ce gate (bootcefromemmc + bootcmd + boot_ce), slot-correct, and
    (b) the generic boot-source helpers a fresh stock env lacks (bootfromsd/usb/
        nand + cfgloadsd/usb/_env + cfgloademmc + bootfromemmc + ce_on_emmc),
        whose values are model-generic and copied verbatim from the known-good
        live env.
  Everything else in the target env (all identity) is left untouched. u-boot's
  cmdline_keys/keyman repopulates per-device values at every boot regardless.

This module's build_target_env(env_bytes, ce_slot) is THE function the PC
installer driver calls per unit. It also writes additions.json (the generic
add set) and reference env_ce_a.bin / env_ce_b.bin for offline validation.
"""
import os, json, sys
import envtool

BASE = os.path.dirname(__file__)
OUT = os.path.join(BASE, "..", "artifacts")
REFDATA = os.path.join(BASE, "..", "refdata")
# Committed, identity-free build input: the 9 generic boot keys.
ADDITIONS_JSON = os.path.join(REFDATA, "env_additions.json")
# Dev-only, NEVER committed (full of device identity: serial/did_key/cpu_id/MAC).
# Only consulted to (re)derive ADDITIONS_JSON when it is absent.
LIVE = os.path.join(BASE, "..", "payload", "env_live_p2.bin")
# Optional factory env, used ONLY to build offline validation refs (env_ref_ce_*.bin),
# which are NOT shipped and NOT read at runtime. Absent in a clean clone -> ref build
# is skipped. If you want the refs, drop an (identity-scrubbed) env here yourself.
FACTORY = os.path.join(REFDATA, "env_factory.bin")

# generic, identity-free vars copied verbatim from the known-good live env.
# (NOT bootcmd/bootcefromemmc/boot_ce -- those are produced slot-correct by the gate.)
GENERIC_KEYS = [
    "bootfromnand", "bootfromsd", "bootfromusb",
    "cfgloadsd", "cfgloadusb", "cfgload_env",
    "cfgloademmc", "bootfromemmc", "ce_on_emmc",
]

# identity vars that MUST NOT be transplanted from one unit to another
IDENTITY_KEYS = ["serial", "serial#", "assm_sn", "assm_mn", "did_key",
                 "cpu_id", "ethaddr"]


def generic_additions():
    """The generic (identity-free) vars to add. Prefer a prebuilt env_additions.json
    (artifacts/ if a build already ran, else the committed refdata/ copy) so neither
    a clone nor a packaged installer needs any device dump. Only fall back to mining
    the live env when no JSON exists at all (dev-only path)."""
    add = None
    for j in (os.path.join(OUT, "env_additions.json"), ADDITIONS_JSON):
        if os.path.exists(j):
            add = json.load(open(j)); break
    if add is None:
        if not os.path.exists(LIVE):
            raise SystemExit("env_additions.json not found in artifacts/ or refdata/, and no "
                             "payload/env_live_p2.bin to derive it from -- cannot build env.")
        live = envtool.parse(open(LIVE, "rb").read())
        add = {k: live[k] for k in GENERIC_KEYS if k in live}
    missing = [k for k in GENERIC_KEYS if k not in add]
    if missing:
        print(f"  WARN missing generic keys: {missing}", file=sys.stderr)
    for k in add:                       # safety: never an identity var
        assert k not in IDENTITY_KEYS
    return add


def build_target_env(env_bytes, ce_slot, default="android"):
    """Apply generic additions + the dual-boot gate to a target's OWN env.
    ce_slot is the INACTIVE Android slot ('_a' or '_b'). default selects which OS a
    normal reboot boots ('android' or 'coreelec'). Returns new 64 KiB env; identity
    vars in env_bytes are preserved untouched."""
    assert ce_slot in ("_a", "_b")
    d = envtool.parse(env_bytes)
    before_identity = {k: d.get(k) for k in IDENTITY_KEYS}
    d.update(generic_additions())
    d.update(envtool.gate_vars(ce_slot, default))   # bootcefromemmc, bootcmd, boot_ce, bootfromnand
    # assert identity untouched
    for k in IDENTITY_KEYS:
        assert d.get(k) == before_identity[k], f"identity var {k} changed!"
    return envtool.serialize(d)


def _validate():
    os.makedirs(OUT, exist_ok=True)
    add = generic_additions()
    json.dump(add, open(os.path.join(OUT, "env_additions.json"), "w"), indent=2)
    print(f"env_additions.json: {len(add)} generic vars {sorted(add)}")

    if not os.path.exists(FACTORY):
        print(f"  (skip env_ref build: no factory env at {os.path.relpath(FACTORY)} -- the refs "
              "are offline-validation only, not shipped or read at runtime)")
        print("env build OK (env_additions.json written; validation refs skipped)")
        return

    factory = open(FACTORY, "rb").read()
    for slot in ("_a", "_b"):
        env = build_target_env(factory, slot)
        ok, st, ca = envtool.crc_ok(env)
        d = envtool.parse(env)
        # checks
        assert ok, "crc invalid"
        assert d["boot_ce"] == "0"
        assert f"imgread kernel boot{slot}" in d["bootcefromemmc"]
        assert f"store read ${{dtb_mem_addr}} dtbo{slot}" in d["bootcefromemmc"]
        assert "run bootfromusb" in d["bootcmd"] and "run bootfromsd" in d["bootcmd"]
        for k in GENERIC_KEYS:
            assert k in d, f"missing {k}"
        fn = os.path.join(OUT, f"env_ref_ce{slot}.bin")
        open(fn, "wb").write(env)
        print(f"  built {os.path.basename(fn)}  crc_ok={ok} vars={len(d)} "
              f"boot_ce={d['boot_ce']} gate->boot{slot}")
    # prove identity is NOT present when starting from a clean factory stub
    fac = envtool.parse(factory)
    present = [k for k in IDENTITY_KEYS if k in fac]
    print(f"  identity vars in factory stub: {present or 'none'} "
          f"(real per-device values are injected at boot by keyman)")
    print("env reference build + validation OK")


if __name__ == "__main__":
    _validate()
