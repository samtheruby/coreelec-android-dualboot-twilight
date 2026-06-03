# envcodec.py -- minimal u-boot env codec for CoreELEC (matches build/envtool.py).
# Non-redundant Amlogic env: [crc32 LE (4)][key=val\0 ... \0\0][0x00 pad] over 0x10000.
import os
import struct
import zlib

ENV_SIZE = 0x10000
ENV_DEV = "/dev/env"            # CoreELEC: block 179,2 == mmcblk0p2


def parse(b):
    d = {}
    for chunk in b[4:ENV_SIZE].split(b"\x00"):
        if not chunk:
            break
        k, _, v = chunk.partition(b"=")
        d[k.decode("latin1")] = v.decode("latin1")
    return d


def crc_ok(b):
    if len(b) < ENV_SIZE:
        return False
    return struct.unpack_from("<I", b, 0)[0] == (zlib.crc32(b[4:ENV_SIZE]) & 0xffffffff)


def serialize(d):
    body = bytearray()
    for k, v in d.items():
        body += f"{k}={v}".encode("latin1") + b"\x00"
    body += b"\x00"
    if len(body) > ENV_SIZE - 4:
        raise ValueError("env body too large")
    body += b"\x00" * (ENV_SIZE - 4 - len(body))
    crc = zlib.crc32(body) & 0xffffffff
    return struct.pack("<I", crc) + bytes(body)


def gate_vars(ce_slot, default):
    boot, dtbo = "boot" + ce_slot, "dtbo" + ce_slot
    bootcefromemmc = (
        'setenv bootargs "${bootargs} BOOT_IMAGE=kernel.img boot=LABEL=CE_FLASH '
        'disk=LABEL=CE_STORAGE console=tty0 no_console_suspend quiet"; '
        'setenv loadaddr ${loadaddr_kernel}; '
        f'store read ${{dtb_mem_addr}} {dtbo} 0 0x20000; '
        f'if imgread kernel {boot} ${{loadaddr}}; then bootm ${{loadaddr}}; fi'
    )
    if default == "coreelec":
        bootcmd = (
            'if test ${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; '
            'else run bootfromsd; run bootfromusb; run bootcefromemmc; fi; run storeboot'
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


def detect_default(d):
    bc = d.get("bootcmd", "")
    return "coreelec" if ("run bootcefromemmc; fi; run storeboot" in bc
                          and "boot_ce} = 1" not in bc) else "android"


def detect_ce_slot(d):
    g = d.get("bootcefromemmc", "")
    if "imgread kernel boot_a" in g:
        return "_a"
    if "imgread kernel boot_b" in g:
        return "_b"
    return None


def read_env():
    with open(ENV_DEV, "rb") as f:
        return f.read(ENV_SIZE)


def write_env(b):
    with open(ENV_DEV, "r+b") as f:
        f.write(b)
        f.flush()
        os.fsync(f.fileno())
