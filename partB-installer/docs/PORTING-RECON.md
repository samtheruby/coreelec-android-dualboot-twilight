# Porting recon — Xiaomi TV Box S 3rd Gen

**Recon only.** Collect the device-specific facts a port needs. No install, no manual — once the
data is back we build a device-specific version from it. Everything is **read-only** except the one
unavoidable destructive prereq: **getting root** (unlock bootloader + Magisk-patched `init_boot`),
because the raw block reads need `su`.

## What the collaborator does

1. **Get root** on the box (only destructive step):
   - Unlock the bootloader (`fastboot flashing unlock` — this factory-resets the box).
   - Root with a Magisk-patched `init_boot` (standard Magisk flow for this device).
   - Confirm: `adb shell su -c id` → `uid=0`.
   - Re-enable **USB + Network debugging** after the reset.

2. **Run the recon script** (from the `partB-installer/` folder, PC with `adb` on PATH):
   ```
   python installer/porting_recon.py --serial <ip:port|usbid>
   ```
   Read-only. Writes nothing to the device.

3. **Send back the whole `recon_out/` folder.** It contains:
   - `recon_report.txt` — human-readable findings
   - `recon_data.json` — machine-readable facts (feeds the new `layout.py`)
   - `part_geometry.json` — every partition's start/size
   - `env.bin`, `misc.bin`, `reserved_head.bin`, `gpt_primary.bin`, `gpt_backup.bin` — baseline blobs

## What it answers (the 4 porting questions, research.md §3.1)

- Codename / SoC / A/B? / unlock+root state / OTA partitions
- GPT entry count + whether it expands 32→128 non-destructively
- userdata carve span + eMMC size (→ new partition sizes)
- u-boot env format (size / redundant / CRC) + `cfgloademmc` present → **cfgload vs injection** method
- Amlogic MPT present? (the logo-hang trap)
- misc A/B `BCAB` struct + on-device tool inventory

The only thing the script can't do headless: confirm CoreELEC actually **boots from SD/USB with a
matching dtb**. That's the next manual check once the data says the port is viable — we'll spec it
after seeing `recon_out/`.
