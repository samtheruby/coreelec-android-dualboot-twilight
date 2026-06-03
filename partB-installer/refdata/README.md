# refdata — committed, identity-free build inputs

These are the only device-derived files the from-source build needs. Each has been
checked to contain **no per-device identity** (no serial, MAC, `cpu_id`, `did_key`,
`assm_sn`). They are safe to publish.

| file | what it is | why it's safe |
|---|---|---|
| `stock_gpt_first2m.bin` | first 2 MiB of a stock `twilight` eMMC — the primary GPT (32-entry) | only the disk GUID + 33 random partition GUIDs + generic partition names (`boot_a`, `super`, `userdata`, …). No serial/MAC/cpu_id. GUIDs are random UUIDs, not traceable; the boot scheme reads partitions by **name** and CoreELEC mounts by **label**, so GUID values are not load-bearing. |
| `stock_gpt_last2m.bin` | last 2 MiB of the same eMMC — the backup GPT | same as above (mirror of the primary entry array + alt header). |
| `env_additions.json` | the 9 generic u-boot boot keys the installer adds to every unit (`bootfromnand/sd/usb`, `cfgload*`, `bootfromemmc`, `ce_on_emmc`) | pure boot logic, model-generic, copied from a known-good env with all identity vars excluded by construction. |

## What is NOT here (and never committed)

The full u-boot env dumps (`env_p2.bin`, `env_live_p2.bin`) carry `serial`, `assm_sn`,
`cpu_id`, `ethaddr`, and `did_key` (real MAC + key material). They are **not needed** by
the build: per-device identity is read **live from each target at install time** and left
untouched (`build_env.build_target_env` asserts identity vars are unchanged), so nothing
device-specific is ever baked into a shipped artifact. The `.gitignore` also blocks those
filenames tree-wide as defense-in-depth.
