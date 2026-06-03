#!/usr/bin/env python3
"""
u-boot environment tool for the twilight internal dual-boot.

Verified env layout on this device:
  partition  /dev/block/by-name/env  (mmcblk0p2)
  the live env lives at OFFSET 0, SIZE 0x10000 (64 KiB), NON-redundant.
  format: [4-byte CRC32 LE] [ key=val \0 key=val \0 ... \0 ] [0x00 padding]
  CRC32 covers bytes [4 : 0x10000].

This module is the single implementation of env read/parse/edit/crc, used by:
  - the PC installer driver (operate on each unit's pulled env, pure bytes)
  - build_env.py (produce a reference env.bin for testing)
No fw_setenv / fw_printenv needed anywhere (Android lacks them).

set_boot_ce(env_bytes, 1/0) is the flag the "Reboot to CoreELEC" app flips.
apply_gate(...) installs the full boot_ce gate (slot-aware).
"""
import struct, zlib

ENV_SIZE = 0x10000          # 64 KiB env area at start of p2
CRC_OFF = 0


def parse(env_bytes):
    """env_bytes: at least ENV_SIZE. Returns ordered dict of {key: value}."""
    assert len(env_bytes) >= ENV_SIZE, f"env too small: {len(env_bytes)}"
    data = env_bytes[4:ENV_SIZE]
    d = {}
    for chunk in data.split(b"\x00"):
        if not chunk:
            break                      # empty entry terminates the list
        k, _, v = chunk.partition(b"=")
        d[k.decode("latin1")] = v.decode("latin1")
    return d


def crc_ok(env_bytes):
    stored = struct.unpack_from("<I", env_bytes, CRC_OFF)[0]
    calc = zlib.crc32(env_bytes[4:ENV_SIZE]) & 0xffffffff
    return stored == calc, stored, calc


def serialize(env_dict):
    """Rebuild a full ENV_SIZE blob with a correct CRC. Preserves key order."""
    body = bytearray()
    for k, v in env_dict.items():
        body += f"{k}={v}".encode("latin1") + b"\x00"
    body += b"\x00"                                  # list terminator
    if len(body) > ENV_SIZE - 4:
        raise ValueError(f"env body {len(body)} exceeds {ENV_SIZE-4}")
    body += b"\x00" * (ENV_SIZE - 4 - len(body))     # pad (matches device: 0x00)
    crc = zlib.crc32(body) & 0xffffffff
    return struct.pack("<I", crc) + bytes(body)


def set_var(env_bytes, key, value):
    d = parse(env_bytes)
    d[key] = value
    return serialize(d)


def set_boot_ce(env_bytes, on):
    """The one-shot gate flag. on=1 -> next boot is CoreELEC; 0 -> Android."""
    return set_var(env_bytes, "boot_ce", "1" if on else "0")


# --- the working gate, parameterised by CoreELEC slot + which OS is default ---
def gate_vars(ce_slot, default="android"):
    """Env vars that install the dual-boot gate for CE on ce_slot.
    ce_slot is the INACTIVE Android slot (e.g. '_a' when Android runs on '_b');
    u-boot reads the CE kernel from boot{ce_slot} + CE dtb from dtbo{ce_slot}.

    default="android"  -> normal reboot = Android; the switcher app sets boot_ce=1
                          (one-shot) to enter CoreELEC.
    default="coreelec" -> normal reboot = CoreELEC; CoreELEC's existing
                          "reboot to eMMC/nand" (rebootfromnand -> bootfromnand=1)
                          routes to Android (one-shot). If CE ever fails to boot,
                          u-boot falls through to storeboot=Android automatically.
    """
    assert ce_slot in ("_a", "_b")
    assert default in ("android", "coreelec")
    boot = "boot" + ce_slot
    dtbo = "dtbo" + ce_slot
    bootcefromemmc = (
        'setenv bootargs "${bootargs} BOOT_IMAGE=kernel.img '
        'boot=LABEL=CE_FLASH disk=LABEL=CE_STORAGE console=tty0 '
        'no_console_suspend quiet"; '
        'setenv loadaddr ${loadaddr_kernel}; '
        f'store read ${{dtb_mem_addr}} {dtbo} 0 0x20000; '
        f'if imgread kernel {boot} ${{loadaddr}}; then bootm ${{loadaddr}}; fi'
    )
    if default == "coreelec":
        # bootfromnand=1 (set by rebootfromnand) -> reset + storeboot (Android).
        # else -> external boot, then CoreELEC; CE failure falls to storeboot.
        bootcmd = (
            'if test ${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; '
            'else run bootfromsd; run bootfromusb; run bootcefromemmc; fi; '
            'run storeboot'
        )
    else:
        bootcmd = (
            'if test ${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; '
            'else run bootfromsd; run bootfromusb; '
            'if test ${boot_ce} = 1; then setenv boot_ce 0; saveenv; '
            'run bootcefromemmc; fi; fi; run storeboot'
        )
    return {"bootcefromemmc": bootcefromemmc, "bootcmd": bootcmd,
            "boot_ce": "0", "bootfromnand": "0"}


def apply_gate(env_bytes, ce_slot, default="android"):
    d = parse(env_bytes)
    d.update(gate_vars(ce_slot, default))
    return serialize(d)


# --- diff helper (CLI) -------------------------------------------------------
if __name__ == "__main__":
    import sys, os
    if len(sys.argv) >= 4 and sys.argv[1] == "diff":
        a = open(sys.argv[2], "rb").read()
        b = open(sys.argv[3], "rb").read()
        da, db = parse(a), parse(b)
        ka, kb = set(da), set(db)
        print(f"A={sys.argv[2]} ({len(da)} vars)  B={sys.argv[3]} ({len(db)} vars)")
        print("\n-- only in A --");  [print(f"  {k}={da[k][:80]}") for k in sorted(ka - kb)]
        print("\n-- only in B --");  [print(f"  {k}={db[k][:80]}") for k in sorted(kb - ka)]
        print("\n-- changed --")
        for k in sorted(ka & kb):
            if da[k] != db[k]:
                print(f"  {k}:\n     A={da[k][:100]}\n     B={db[k][:100]}")
    elif len(sys.argv) >= 3 and sys.argv[1] == "show":
        e = open(sys.argv[2], "rb").read()
        ok, st, ca = crc_ok(e)
        print(f"crc_ok={ok} stored={st:#010x} calc={ca:#010x}")
        for k, v in parse(e).items():
            print(f"  {k}={v[:120]}")
    else:
        print("usage: envtool.py diff A.bin B.bin | show env.bin")
