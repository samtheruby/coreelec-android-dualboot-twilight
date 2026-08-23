package com.jamal2367.coreelec

/**
 * GENERATED -- do not hand-edit. Mirror of `partB-installer/refdata/env_additions.json`.
 *
 * The 9 generic, identity-free u-boot boot-source helpers. A stock env (or an env
 * that a recovery factory-reset has reset to stock) lacks these, and the gated
 * `bootcmd` calls into them, so the full-rebuild path in [EnvGate] has to re-add
 * them before the gate means anything.
 *
 * These are model-generic: no serial, no MAC, no did_key, no cpu_id. Copying them
 * between units is safe -- unlike the identity vars, which [EnvGate] never touches.
 *
 * Regenerate after any change to refdata/env_additions.json:
 *
 *   python - <<'EOF' > app/src/main/java/com/jamal2367/coreelec/EnvAdditions.kt
 *   import json
 *   d = json.load(open("partB-installer/refdata/env_additions.json"))
 *   print("package com.jamal2367.coreelec\n")
 *   print("internal val ENV_ADDITIONS: LinkedHashMap<String, String> = linkedMapOf(")
 *   for k, v in d.items():
 *       esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
 *       print(f'    "{k}" to "{esc}",')
 *   print(")")
 *   EOF
 *
 * (that snippet drops this header comment -- paste it back, or keep the header in a
 * separate file and generate only the map)
 */
internal val ENV_ADDITIONS: LinkedHashMap<String, String> = linkedMapOf(
    "bootfromnand" to "0",
    "bootfromsd" to "if mmcinfo; then run cfgloadsd; fi",
    "bootfromusb" to "usb start; if usb storage; then run cfgloadusb; fi",
    "cfgloadsd" to "if fatload mmc 0:1 \${loadaddr} cfgload; then setenv device mmc; " +
            "setenv devnr 0; setenv partnr 1; source \${loadaddr}; autoscr \${loadaddr}; " +
            "run cfgload_env; fi",
    "cfgloadusb" to "if fatload usb 0:1 \${loadaddr} cfgload; then setenv device usb; " +
            "setenv devnr 0; setenv partnr 1; source \${loadaddr}; autoscr \${loadaddr}; " +
            "run cfgload_env; fi",
    "cfgload_env" to "if fatload \${device} 0:1 \${loadaddr} cfgload_env; then " +
            "env import -t \${loadaddr} \${filesize}; run ceboot; fi",
    "cfgloademmc" to "for p in 1 2 3 4 5 6 7 8 9 A B C D E F 10 11 12 13 14 15 16 17 18 " +
            "19 1A 1B 1C 1D 1E 1F 20 21 22; do if fatload mmc 1:\${p} \${loadaddr} cfgload; " +
            "then setenv device mmc; setenv devnr 1; setenv partnr \${p}; " +
            "setenv ce_on_emmc \"yes\"; source \${loadaddr}; autoscr \${loadaddr}; fi; done;",
    "bootfromemmc" to "run cfgloademmc",
    "ce_on_emmc" to "no",
)
