# Porting findings — Xiaomi TV Box S 3rd Gen

Analysis of `recon_out/` against `research.md` and the current `twilight` implementation.
Verdict, what ports untouched, and the exact files/constants a port must change.

## Headline

The Box S 3rd Gen is **the same SoC family as the TV Stick 4K 2nd Gen** the project already
targets — it even reports the same codename. It is a *bigger sibling*, not a new architecture:

| | TV Stick 4K 2nd Gen (done) | **TV Box S 3rd Gen (recon)** |
|---|---|---|
| `ro.product.device` | `twilight` | **`twilight`** (same) |
| `ro.product.model` | `MiTV-AFMU1` | **`MiTV-AFMU0`** (different) |
| `ro.board.platform` | `s7d` (S905X5M) | **`s7d`** (same) |
| eMMC | 7.28 GiB | **29.12 GiB** (61,071,360 sectors) |
| userdata carve | 4176 MiB | **26540 MiB** |
| Android | 14, A/B, FBE | 14, A/B, FBE (same) |
| Active slot | — | `_b` → **CE slot `_a`** |

Because the SoC, u-boot, env format, and A/B scheme are identical, **the boot mechanism and almost
all of the software ports unchanged.** The real work is *geometry* (a much larger disk) plus the
per-device artifacts every port needs (GPT reference, rebuilt CE images, validated dtb, own Magisk
init_boot).

> ⚠️ **Model collision.** Both devices report `device=twilight`. The current model-lock (`!= "twilight"`)
> would let the *stick* installer run against the *box* and vice-versa. The box is saved today only by
> the geometry preflight (`STOCK_UD_LAST_LBA`) tripping — a port must discriminate by **model
> (`AFMU0`/`AFMU1`) or eMMC size**, not codename alone.

## Feasibility — the 4 porting questions (research.md §3.1)

| Question | Answer | Source |
|---|---|---|
| 1. Can u-boot boot a GPT-added FAT partition (cfgload)? | **No** — `cfgloademmc` absent → **named-partition injection** (same as stick) | `recon_data.json` `has_cfgloademmc=false` |
| 2. Is it A/B (inactive boot/dtbo to borrow)? | **Yes** — active `_b`, CE slot `_a`; `boot_a` 64 MiB, `dtbo_a` 2 MiB | geometry |
| 3. Correct CoreELEC dtb for the SoC? | **Unverified** — the one open gap (see below) | — |
| 4. env format (size / redundant / CRC)? | **non-redundant, 0x10000, CRC valid** → `envtool.py` reusable as-is | `env.bin` parsed clean |

Plus the two extra confirmations the recon gathered:
- **GPT** 32 entries, 16 KiB array room → **expands to 128 non-destructively** (same as stick).
- **No Amlogic MPT** (`reserved[0:4]` zero, `AMLNORMAL` at 0x4000) → kernel uses the GPT natively;
  `wipe_mpt` is a harmless no-op here.
- **misc `BCAB`** A/B control block present, CRC valid → `ab_misc.py` reusable.

**Verdict: viable, same method as `twilight`.** No blocker in the recon. Only the dtb is unproven,
and that is testable with an SD/USB boot before any internal write.

## Ports UNCHANGED (the big win — same u-boot/SoC)

These need **no edits**:

- `build/envtool.py` — env codec (CRC matched at 0x10000, non-redundant).
- `build/build_env.py` + `refdata/env_additions.json` — the 9 generic boot keys + gate composition.
- `envtool.gate_vars()` — the `boot_ce` gate. Stock boot flow here is `preboot → switch_bootmode →
  bootcmd=run storeboot`; the gate replaces `bootcmd` exactly as on the stick (identical u-boot), and
  `bootfromsd/bootfromusb` come from the generic additions. No change expected.
- `build/ab_misc.py` — `BCAB` struct, same offsets.
- `flash_to_coreelec.py` write/verify machinery — push+dd / nc-stream / base64 / SHA read-back /
  `wipe_mpt` / `arm_factory_reset` (BCB). Tool inventory confirms the same constraints
  (`fw_setenv`/`mkfs.vfat`/`parted` MISSING; `nc`/`base64`/`sgdisk`/`mke2fs` present).
- `boota.img` (CE kernel.img) — same SoC + CE build ⇒ reuse (dtb is the device-specific piece, not
  the kernel).
- OTA blocking `blockgms` (generic Google TV); self-heal `payload/flash/user-update.sh` (slot already
  dynamic); Toolbox addon; Kodi sources; Reboot-to-CoreELEC app.

## Must CHANGE — exact points

### A. Geometry — `build/layout.py`
Re-derive from the box's own userdata span:

| Const | Stick | **Box** | Note |
|---|---|---|---|
| `TOTAL_SECTORS` (L20) | 15,269,888 | **61,071,360** | whole eMMC |
| `CARVE_START_MIB` (L25) | 3278 | **3278** | *unchanged* — userdata starts at the same LBA (6,713,344) |
| `CARVE_END_MIB` (L26) | 7454 | **29818** | = 3278 + 26540 |
| `SIZES_MIB` (L31) | 2376/600/1200 | **rebalance** (see below) | must sum to 26540 |

**Suggested split** (research.md §2.4 scaling; 26 GiB is abundant):
`CE_FLASH 1024 · CE_STORAGE 8192 · userdata 17324` (sum 26540). CE_FLASH never needs to be big;
spend the surplus on CE_STORAGE (texture cache / addons / the ~790 M update transient peak). Final
numbers are the collaborator's call — this is the single knob.

### B. Installer geometry constants — `installer/flash_to_coreelec.py`

| Const | Stick | **Box** | Derivation |
|---|---|---|---|
| `GPT_BACKUP_LBA` (L30) | 15,265,792 | **61,071,327** | = `last_usable_lba (61,071,326) + 1` |
| `STOCK_UD_LAST_LBA` (L32) | 15,265,791 | **61,067,263** | = ud start 6,713,344 + size 54,353,920 − 1 |
| `STOCK_NUM_ENTRIES` (L31) | 32 | 32 | unchanged |

> **Subtlety:** on the stick, userdata ended exactly at `last_usable`, so `CARVE_END`, the backup-GPT
> location, and `GPT_BACKUP_LBA` all coincided. On the **box they do not** — userdata ends at
> 61,067,263 but the backup GPT sits at 61,071,327 (~2 MiB gap after the carve). Do **not** derive
> `GPT_BACKUP_LBA` from `CARVE_END`; take it from the actual backup GPT location (confirmed: recon
> pulled `gpt_backup.bin` from sector 61,071,327).

Also add **model/geometry discrimination** to `preflight()` (L249-251) and `reguard()` (L373) — see the
model-collision warning above.

### C. GPT builder + reference — `build/build_gpt_layout.py` + `refdata/`
- `BACK_START_LBA` (L33) → **61,071,327**.
- Provide the box's **identity-free stock GPT** as `refdata/stock_gpt_first2m.bin` +
  `stock_gpt_last2m.bin`. The recon `gpt_primary.bin` (34 sectors) already holds the full 128-slot
  primary array and `gpt_backup.bin` (33 sectors) holds the backup array + alt header — enough to
  derive the refs (re-pull a clean 2 MiB primary/last-2 MiB if you want them byte-for-byte for the
  builder's current file-size expectations). The builder self-verifies all CRCs and refuses to write
  on any mismatch, so this stays safe.

### D. Rebuilt artifacts
- `artifacts/gpt_primary.bin` + `gpt_backup.bin` — regenerated by the GPT builder from B/C.
- `artifacts/ce_flash.img(.gz)` + `ce_storage.img(.gz)` — rebuilt to the new sizes
  (`build_ce_flash.sh` / `build_ce_storage.sh` read `layout.py`).
- `artifacts/dtboa.img` — **validate/replace** (see gap).
- `magisk/twilight-init_boot-patched.img` — the box (AFMU0) runs different firmware than the stick
  (AFMU1); patch the **box's own** `init_boot` with Magisk. `init_boot_a/b` (p23/p24) exist, so
  stage_magisk applies.

### E. Per-device extras (non-blocking, stage 3 / optional)
- `payload/remote/99-xiaomi-remote.hwdb` + `xiaomi.xml` — the box's remote may use different
  vendor/product + scancodes; recapture if the remap is wanted.
- `installer/install_blockota.py` — confirm the box's updater package is still
  `com.xiaomi.mitv.updateservice` (very likely).

## The one open gap — dtb validation

Not scriptable headless. Before any internal write:
1. Write a CoreELEC Amlogic-ng image (matching the project's CE build) to SD/USB.
2. Try the SoC dtb (the stick used `s7d_s905x5m_xiaomi_3rd_gen` — note the name literally says
   "3rd_gen"; it may or may not be the box's correct one) as `dtb.img`.
3. Boot the box externally; confirm Kodi, HW H.265 decode, WiFi/BT/remote, clean dmesg.
4. The working dtb becomes `artifacts/dtboa.img` (via `build_boota_dtboa.py`, padded to 128 KiB).

If it boots externally, the internal port is very likely to succeed given everything else already
matches.

## Suggested port sequence

1. Get the box's stock GPT refs into `refdata/` + build a Magisk-patched box `init_boot`.
2. Edit `layout.py` (geometry + new sizes) and the 2 constants in `flash_to_coreelec.py`; add
   model discrimination.
3. Validate the dtb on SD/USB; drop the CE payload; run `build/build_all.py` to regenerate GPT +
   CE images + boota/dtboa.
4. Dry-run `stage1` against the box, review the plan, then the destructive install — same stage flow
   as the README.

## Risks specific to this port

- **Model collision** (biggest): both are `twilight`. Must discriminate by model/geometry or the two
  installers are interchangeable. Mitigated today only by the `STOCK_UD_LAST_LBA` preflight.
- **dtb** unproven until the SD/USB test (research.md §2.8 / §3.4 risk table).
- Everything else (env, A/B, MPT-absence, write methods, OTA) is confirmed-matching, so the usual
  "wrong env format / not-A-B / can't-scan-or-inject" blockers do **not** apply here.
- Universal safety net unchanged: `boot0`/`boot1` HW write-protected → USB-burn restores stock.
