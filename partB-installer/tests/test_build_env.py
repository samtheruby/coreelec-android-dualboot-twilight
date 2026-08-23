#!/usr/bin/env python3
"""Building a target unit's u-boot env without transplanting its identity.

Dependency-free on purpose: run it directly.

    python tests/test_build_env.py

build_target_env() takes the env read off THIS box, adds the generic dual-boot variables,
writes the boot gate, and hands back 64 KiB to flash. The thing it must never do is carry
one unit's identity onto another: serial, assm_sn, did_key, cpu_id and ethaddr live in the
same env blob as everything else, and a build that copied them would give two boxes the
same MAC and the same device keys.

The module guards that with asserts. Asserts vanish under `python -O`, and the guard is
only as good as the list of keys it covers, so the property is pinned here as a test
instead of only as a runtime check on the build machine.

The gate itself is covered by test_boot_gate.py; what is checked here is that the gate
build_target_env writes is the one envtool would produce, and that nothing else moved.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "build"))

import build_env  # noqa: E402
import envtool  # noqa: E402

# Values that must survive untouched. Deliberately distinctive so a transplant is obvious.
IDENTITY = {
    "serial": "TESTSERIAL0001",
    "serial#": "TESTSERIAL0001",
    "assm_sn": "ASSM-SN-TEST",
    "assm_mn": "ASSM-MN-TEST",
    "did_key": "deadbeefdeadbeef",
    "cpu_id": "0123456789abcdef",
    "ethaddr": "aa:bb:cc:dd:ee:ff",
}

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


def source_env(**extra):
    """An env blob standing in for one read off a real unit."""
    d = {"bootcmd": "run storeboot", "bootdelay": "1"}
    d.update(IDENTITY)
    d.update(extra)
    return envtool.serialize(d)


def identity_is_never_transplanted():
    """The property the whole module is arranged around."""
    for slot in ("_a", "_b"):
        for default in ("android", "coreelec"):
            out = envtool.parse(build_target_env_for(slot, default))
            for k, v in IDENTITY.items():
                assert out.get(k) == v, (
                    f"{k} changed building slot={slot} default={default}: "
                    f"{v!r} -> {out.get(k)!r}")


def build_target_env_for(slot, default):
    return build_env.build_target_env(source_env(), slot, default)


def the_gate_written_is_the_gate_envtool_defines():
    """build_env must not carry its own idea of the gate -- that is the drift
    test_boot_gate.py exists to prevent, one layer up."""
    for slot in ("_a", "_b"):
        for default in ("android", "coreelec"):
            out = envtool.parse(build_target_env_for(slot, default))
            for k, v in envtool.gate_vars(slot, default).items():
                assert out.get(k) == v, (
                    f"slot={slot} default={default}: {k} is {out.get(k)!r}, "
                    f"envtool.gate_vars says {v!r}")


def the_generic_additions_are_applied():
    out = envtool.parse(build_target_env_for("_a", "android"))
    add = build_env.generic_additions()
    assert add, "no generic additions were found at all -- refdata/env_additions.json?"
    for k, v in add.items():
        assert out.get(k) == v, f"generic var {k} is {out.get(k)!r}, expected {v!r}"


def the_generic_additions_carry_no_identity():
    """The committed env_additions.json is shipped to every unit. One identity key in it
    would be copied onto every box that installs."""
    add = build_env.generic_additions()
    leaked = sorted(set(add) & set(build_env.IDENTITY_KEYS))
    assert not leaked, f"env_additions.json carries identity vars: {leaked}"


def unrelated_variables_survive():
    """Only the gate and the generic additions may change. A var the unit had and neither
    list mentions must come back unchanged."""
    src = source_env(mystery_var="keep-me")
    out = envtool.parse(build_env.build_target_env(src, "_a", "android"))
    assert out.get("mystery_var") == "keep-me", (
        f"an unrelated variable was dropped or rewritten: {out.get('mystery_var')!r}")


def the_result_is_a_valid_env_image():
    """It gets flashed as-is. A bad CRC or a wrong size is not recoverable on the box."""
    for slot in ("_a", "_b"):
        blob = build_target_env_for(slot, "android")
        assert len(blob) == envtool.ENV_SIZE, (
            f"env is {len(blob)} bytes, expected {envtool.ENV_SIZE}")
        ok = envtool.crc_ok(blob)[0]
        assert ok, f"built env for slot {slot} has a bad CRC"


def the_two_defaults_differ():
    """If android and coreelec produced the same env, --default would do nothing and
    nothing would say so."""
    a = build_target_env_for("_a", "android")
    c = build_target_env_for("_a", "coreelec")
    assert a != c, "--default android and --default coreelec produced identical envs"


def an_unknown_slot_is_refused():
    for slot in ("_c", "a", "", None):
        try:
            build_env.build_target_env(source_env(), slot, "android")
        except (AssertionError, KeyError, TypeError, ValueError):
            continue
        raise AssertionError(f"accepted ce_slot={slot!r}; only _a and _b exist")


if __name__ == "__main__":
    print("build_env -- the target unit's env, without its neighbour's identity")
    check("identity vars are never transplanted", identity_is_never_transplanted)
    check("the gate written is the gate envtool defines", the_gate_written_is_the_gate_envtool_defines)
    check("the generic additions are applied", the_generic_additions_are_applied)
    check("the generic additions carry no identity", the_generic_additions_carry_no_identity)
    check("unrelated variables survive untouched", unrelated_variables_survive)
    check("the result is a valid 64 KiB env with a good CRC", the_result_is_a_valid_env_image)
    check("android and coreelec defaults differ", the_two_defaults_differ)
    check("an unknown ce_slot is refused", an_unknown_slot_is_refused)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("all build_env checks passed")
