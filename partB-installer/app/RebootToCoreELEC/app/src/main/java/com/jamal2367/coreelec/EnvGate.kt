package com.jamal2367.coreelec

import android.os.Build

/**
 * In-app port of `partB-installer/installer/reassert_env_gate.py`.
 *
 * A CoreELEC OS update can rewrite `bootcmd` back to a stock version that drops our
 * `if ${boot_ce} = 1 ... run bootcefromemmc` check, while `bootcefromemmc` itself
 * survives. When that happens the switcher silently stops working: [EnvFlip] sets
 * boot_ce=1 correctly, but nothing in bootcmd reads it any more.
 *
 * The CE update-hook (user-update.sh) runs in the initramfs, where fw_setenv does not
 * exist, so CoreELEC cannot repair its own env. The PC script did it over adb -- but
 * every privileged step there was already `su -c` on the device, so it ports directly.
 *
 * Two cases, matching the Python:
 *   * gate PARTIALLY present (bootcefromemmc survives) -> re-apply the gate. Safe and
 *     fully automatic: the CE slot is read back out of the surviving bootcefromemmc,
 *     so nothing is inferred.
 *   * gate FULLY gone (recovery factory-reset put the env back to stock, dropping the
 *     gate AND the generic boot helpers) -> the CE slot has to be INFERRED from the
 *     inactive Android slot. That inference is only sound while OTA is blocked; if
 *     Android ever slot-switched, it names the wrong slot. So this path is never taken
 *     automatically -- [reassert] returns [GateResult.NeedsRebuild] and the caller must
 *     pass allowRebuild=true after an explicit user confirmation.
 *
 * Identity vars (serial, did_key, cpu_id, ethaddr, assm_sn) are never touched: the env
 * is read from the device, edited in place, and written back.
 */
object EnvGate {

    /** Identity vars that must survive any edit untouched. Mirrors build_env.IDENTITY_KEYS. */
    private val IDENTITY_KEYS = listOf(
        "serial", "serial#", "assm_sn", "assm_mn", "did_key", "cpu_id", "ethaddr"
    )

    /** Supported units: ro.product.model -> mmcblk0 size in 512-byte sectors. */
    private val KNOWN_DEVICES = mapOf(
        "MiTV-AFMU1" to 15_269_888L,   // Xiaomi TV Stick 4K 2nd Gen
        "MiTV-AFMU0" to 61_071_360L,   // Xiaomi TV Box S 3rd Gen
    )

    const val DEFAULT_ANDROID = "android"
    const val DEFAULT_COREELEC = "coreelec"

    sealed class GateResult {
        /** Gate is in place (re-asserted or already fine). [rebuilt] = full env rebuild was done. */
        data class Ok(val ceSlot: String, val default: String, val rebuilt: Boolean) : GateResult()

        /**
         * No gate at all. [inferredCeSlot] is derived from the inactive Android slot and is
         * only trustworthy if Android has never slot-switched. Ask the user, then call
         * [reassert] again with allowRebuild=true -- or send them to the PC script.
         */
        data class NeedsRebuild(val activeSlot: String, val inferredCeSlot: String) : GateResult()

        data class Err(val msg: String) : GateResult()
    }

    /**
     * @param present       the gate is fully wired: bootcefromemmc names a slot AND bootcmd
     *                      actually reaches it. False after a CoreELEC update stomps bootcmd.
     *                      Only a pre-flight signal ("does this need repair?") -- do NOT use
     *                      it to choose the repair strategy, that is [ceSlot]'s job.
     * @param ceSlot        CE slot read out of bootcefromemmc, or null if that var is gone
     *                      too. Non-null means the repair needs no slot inference at all.
     * @param currentDefault which OS a plain reboot boots, per the CURRENT bootcmd.
     */
    data class GateState(
        val present: Boolean,
        val ceSlot: String?,
        val currentDefault: String,
    )

    // ---- gate construction (must match build/envtool.py gate_vars) -----------

    private fun bootCeFromEmmc(ceSlot: String): String =
        "setenv bootargs \"\${bootargs} BOOT_IMAGE=kernel.img " +
            "boot=LABEL=CE_FLASH disk=LABEL=CE_STORAGE console=tty0 " +
            "no_console_suspend quiet vout=1080p60hz,dis frac_rate_policy=0 " +
            "hdmitx= hdr_policy=1\"; " +
            "setenv loadaddr \${loadaddr_kernel}; " +
            "store read \${dtb_mem_addr} dtbo$ceSlot 0 0x20000; " +
            "if imgread kernel boot$ceSlot \${loadaddr}; then bootm \${loadaddr}; fi"

    private fun bootCmd(default: String): String =
        if (default == DEFAULT_COREELEC) {
            // bootfromnand=1 (set by CoreELEC's "reboot to eMMC/nand") -> reset + storeboot
            // (Android). Otherwise external boot, then CoreELEC; a CE failure falls through
            // to storeboot on its own.
            "if test \${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; " +
                "else run bootfromsd; run bootfromusb; run bootcefromemmc; fi; " +
                "run storeboot"
        } else {
            "if test \${bootfromnand} = 1; then setenv bootfromnand 0; saveenv; " +
                "else run bootfromsd; run bootfromusb; " +
                "if test \${boot_ce} = 1; then setenv boot_ce 0; saveenv; " +
                "run bootcefromemmc; fi; fi; run storeboot"
        }

    private fun gateVars(ceSlot: String, default: String): LinkedHashMap<String, String> {
        require(ceSlot == "_a" || ceSlot == "_b") { "bad ce slot $ceSlot" }
        require(default == DEFAULT_ANDROID || default == DEFAULT_COREELEC) { "bad default $default" }
        return linkedMapOf(
            "bootcefromemmc" to bootCeFromEmmc(ceSlot),
            "bootcmd" to bootCmd(default),
            "boot_ce" to "0",
            "bootfromnand" to "0",
        )
    }

    // ---- inspection ---------------------------------------------------------

    /** Read the gate's state out of a parsed env. */
    fun inspect(env: ByteArray): GateState {
        val d = EnvFlip.parse(env)
        val g = d["bootcefromemmc"] ?: ""
        val ceSlot = when {
            g.contains("imgread kernel boot_a") -> "_a"
            g.contains("imgread kernel boot_b") -> "_b"
            else -> null
        }
        val bc = d["bootcmd"] ?: ""
        // Same discrimination as reassert_env_gate.py: the coreelec-default bootcmd runs
        // bootcefromemmc unconditionally and has no boot_ce test.
        val cur = if (bc.contains("run bootcefromemmc; fi; run storeboot") &&
            !bc.contains("boot_ce} = 1")
        ) DEFAULT_COREELEC else DEFAULT_ANDROID
        // "Wired up" == bootcmd actually reaches bootcefromemmc, by either route. Mirrors
        // the post-write check in reassert_env_gate.py.
        val present = ceSlot != null && bc.contains("bootcefromemmc") &&
            (bc.contains("boot_ce} = 1") || bc.contains("run bootcefromemmc; fi; run storeboot"))
        return GateState(present, ceSlot, cur)
    }

    /** Cheap pre-flight: is the gate intact right now? Used to decide whether to repair. */
    fun gateIntact(): Boolean {
        val env = EnvFlip.readEnv() ?: return false
        if (!EnvFlip.verifyCrc(env)) return false
        return inspect(env).present
    }

    // ---- the main entry point ----------------------------------------------

    /**
     * Re-assert the gate, optionally set boot_ce, optionally reboot.
     *
     * @param bootCe      1 -> CoreELEC next boot, 0 -> Android, null -> leave alone.
     * @param default     which OS a plain reboot boots. null -> keep the current direction.
     * @param allowRebuild permit the full env rebuild when no gate survives (needs the
     *                     slot inference -- confirm with the user first).
     * @param reboot      reboot on success.
     */
    fun reassert(
        bootCe: Int? = null,
        default: String? = null,
        allowRebuild: Boolean = false,
        reboot: Boolean = false,
    ): GateResult {
        return try {
            identifyDevice()?.let { return GateResult.Err(it) }

            val env = EnvFlip.readEnv() ?: return GateResult.Err("read env failed")
            if (env.size < EnvFlip.ENV_SIZE) return GateResult.Err("short env ${env.size}")
            if (!EnvFlip.verifyCrc(env)) return GateResult.Err("env CRC invalid -- refusing to write")

            val before = EnvFlip.parse(env)
            val state = inspect(env)

            val ceSlot: String
            val target: String
            var rebuilt = false
            val d = LinkedHashMap(before)

            if (state.ceSlot != null) {
                // bootcefromemmc survives -> re-apply the gate around it. This is the
                // CoreELEC-update case (bootcmd stomped back to stock, bootcefromemmc
                // intact) and it is the common one, so it must NOT fall through to the
                // rebuild path: the CE slot is READ from the surviving bootcefromemmc, so
                // nothing is inferred and the user is never asked. Keying this off
                // state.present instead would send exactly this case to the inference
                // path. Matches `if ce_slot:` in reassert_env_gate.py. The generic helpers
                // are still in the env, so the gate vars alone are enough (== apply_gate).
                ceSlot = state.ceSlot
                target = default ?: state.currentDefault
            } else {
                val active = getprop("ro.boot.slot_suffix")
                val inferred = when (active) {
                    "_a" -> "_b"
                    "_b" -> "_a"
                    else -> null
                } ?: return GateResult.Err(
                    "no gate in env AND bad slot_suffix '$active' -- cannot rebuild gate"
                )
                if (!allowRebuild) return GateResult.NeedsRebuild(active, inferred)
                // Full rebuild: a stock env lacks the generic boot helpers the gated
                // bootcmd runs, so add those back too (matches build_env.build_target_env).
                ceSlot = inferred
                target = default ?: DEFAULT_ANDROID
                d.putAll(ENV_ADDITIONS)
                rebuilt = true
            }

            d.putAll(gateVars(ceSlot, target))
            if (bootCe != null) d["boot_ce"] = bootCe.toString()

            // Identity must be byte-identical to what we read.
            for (k in IDENTITY_KEYS) {
                if (d[k] != before[k]) return GateResult.Err("identity var $k would change -- aborting")
            }

            val newEnv = EnvFlip.serialize(d)
            if (!EnvFlip.verifyCrc(newEnv)) return GateResult.Err("serialize produced bad CRC -- aborting")
            if (!EnvFlip.writeEnv(newEnv)) return GateResult.Err("write env failed")

            // Read-back verify against the device, not against what we think we wrote.
            val check = EnvFlip.readEnv() ?: return GateResult.Err("verify read failed")
            if (!EnvFlip.verifyCrc(check)) return GateResult.Err("read-back CRC invalid")
            val after = inspect(check)
            if (!after.present || after.ceSlot != ceSlot) return GateResult.Err("FAILED to re-assert gate")
            if (bootCe != null && EnvFlip.parse(check)["boot_ce"] != bootCe.toString())
                return GateResult.Err("read-back boot_ce mismatch")

            if (reboot) EnvFlip.runSu("reboot")
            GateResult.Ok(ceSlot, target, rebuilt)
        } catch (e: Exception) {
            GateResult.Err(e.message ?: e.toString())
        }
    }

    // ---- device guard -------------------------------------------------------

    /**
     * Fail-closed identity check before any env write, mirroring build/devices.identify().
     * ro.product.device is "twilight" on BOTH units, so the model string is the primary key
     * and the physical eMMC size is the hardware-rooted second opinion.
     *
     * Returns null when the unit is recognised, or an error string.
     */
    private fun identifyDevice(): String? {
        val model = Build.MODEL
        val expected = KNOWN_DEVICES[model]
            ?: return "unrecognised model '$model' -- refusing to touch the env"
        val sectors = readSectors()
            ?: return "cannot read mmcblk0 size (root?) -- refusing to touch the env"
        if (sectors != expected)
            return "eMMC size mismatch for $model: $sectors sectors, expected $expected"
        return null
    }

    private fun readSectors(): Long? =
        runSuCapture("cat /sys/class/block/mmcblk0/size").trim().toLongOrNull()

    private fun getprop(p: String): String = runSuCapture("getprop $p").trim()

    private fun runSuCapture(cmd: String): String = try {
        val p = ProcessBuilder("su", "-c", cmd).redirectErrorStream(false).start()
        val out = p.inputStream.bufferedReader().readText()
        p.waitFor()
        out
    } catch (e: Exception) {
        ""
    }
}
