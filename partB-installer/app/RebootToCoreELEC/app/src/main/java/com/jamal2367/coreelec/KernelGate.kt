package com.jamal2367.coreelec

import android.util.Log

/**
 * Checks -- and repairs -- the CoreELEC kernel image in `boot_<slot>`.
 *
 * u-boot boots CoreELEC with `imgread kernel boot_<slot>`, NOT from /flash. So `/flash/kernel.img`
 * and the boot partition are two copies of one image, and CoreELEC's OS-update hook
 * (`/flash/user-update.sh`) keeps them in sync with a blind `dd`. On a real stick that dd wrote
 * all but the last 659,456 bytes, exited 0, and logged success. The kernel region of `boot_b`
 * matched perfectly; only the tail of the zstd initramfs was left over from the previous kernel.
 * u-boot loaded the image, the kernel could not unpack the ramdisk, freed it, and panicked:
 *
 *     rootfs image is not initramfs (ZSTD-compressed data is corrupt); looks like an initrd
 *     Kernel panic - not syncing: Requested init /init failed (error -2).
 *
 * `bootcmd` consumes `boot_ce` BEFORE running `bootcefromemmc`, so the next boot went to Android
 * and the whole failure presented as "the Reboot button does nothing".
 *
 * [EnvGate] checks the gate -- which was intact and working the entire time. Nothing checked the
 * thing the gate hands off to. That is this class.
 *
 * The update hook cannot do this itself: its initramfs has no `cmp` and no `md5sum`, so it can
 * only confirm the byte count `dd` reported. Here we are in Android's full toybox userspace, so
 * we compare for real -- and because the good copy is sitting on /flash, we can repair too.
 *
 * Everything runs through one `su -c` script per operation: the mount must be undone even when a
 * step fails, and a single shell keeps that guaranteed without a finally-block per round trip.
 */
object KernelGate {
    private const val TAG = "KernelGate"
    private const val CE_FLASH = "/dev/block/by-name/CE_FLASH"

    sealed class State {
        /** boot_<slot> holds exactly the image /flash/kernel.img says it should. */
        object Ok : State()

        /** Verified mismatch. Repairable: the good copy is on /flash. */
        data class Stale(val detail: String) : State()

        /**
         * /flash/kernel.img itself is bad, so there is nothing trustworthy to repair FROM.
         * Note this does NOT mean CoreELEC will fail to boot -- u-boot reads boot_<slot>,
         * which may well be fine.
         */
        data class SourceBad(val detail: String) : State()

        /** Could not determine anything (no root, mount failed, no CE_FLASH). */
        data class Unknown(val detail: String) : State()
    }

    /** The CoreELEC slot, read from the live gate. `_a` / `_b`, or null if there is no gate. */
    fun ceSlot(): String? {
        val env = EnvFlip.readEnv() ?: return null
        if (!EnvFlip.verifyCrc(env)) return null
        return EnvGate.inspect(env).ceSlot
    }

    /**
     * Compare `boot_<slot>` against `/flash/kernel.img`.
     *
     * Compares the WHOLE image, deliberately. The incident left the kernel region byte-perfect
     * and corrupted only the tail, so anything that samples the head reports a healthy image.
     */
    fun check(ceSlot: String): State {
        val part = "boot${ceSlot}"
        val out = su(script(part, repair = false)) ?: return State.Unknown("su failed")
        val f = parse(out)
        f["ERR"]?.let { return errState(it) }

        val size = f["SIZE"]?.toLongOrNull() ?: return State.Unknown("no kernel.img size")
        val src = f["SRC"] ?: return State.Unknown("could not hash /flash/kernel.img")
        val dst = f["DST"] ?: return State.Unknown("could not hash $part")
        val shipped = f["SHIPPED"]

        // CoreELEC ships kernel.img.md5 next to the image. If the source fails its own checksum,
        // repairing from it would copy that corruption onto the boot partition.
        if (shipped != null && !shipped.equals(src, ignoreCase = true))
            return State.SourceBad("/flash/kernel.img fails its own kernel.img.md5")

        Log.d(TAG, "$part size=$size src=$src dst=$dst shipped=$shipped")
        return if (src.equals(dst, ignoreCase = true)) State.Ok
        else State.Stale("$part differs from /flash/kernel.img")
    }

    /**
     * Rewrite `boot_<slot>` from `/flash/kernel.img`, then re-verify by reading it back.
     *
     * Returns the state AFTER the attempt, so [State.Ok] means the repair is confirmed on the
     * partition -- not merely that dd exited 0. That distinction is the whole point of this file.
     */
    fun repair(ceSlot: String): State {
        val part = "boot${ceSlot}"
        // Refuse to copy a source that cannot vouch for itself.
        when (val pre = check(ceSlot)) {
            is State.SourceBad, is State.Unknown -> return pre
            else -> Unit
        }
        Log.w(TAG, "rewriting $part from /flash/kernel.img")
        val out = su(script(part, repair = true)) ?: return State.Unknown("su failed")
        parse(out)["ERR"]?.let { return errState(it) }
        return check(ceSlot)
    }

    // ---- the shell ----------------------------------------------------------
    // One script, mounted read-only for a check and read-only for the repair too: the repair
    // READS /flash and writes the block device, so /flash never needs to be writable.
    private fun script(part: String, repair: Boolean): String {
        val write = if (repair) """
              dd if=${'$'}M/kernel.img of=/dev/block/by-name/$part bs=1048576 conv=fsync 2>/dev/null \
                || { echo "ERR write"; umount ${'$'}M; rmdir ${'$'}M; exit 1; }
              sync
        """.trimIndent() else ""
        // SZ is always a multiple of 512 (an Android boot image is a whole number of 2048-byte
        // pages) but the remainder is handled anyway rather than assumed.
        return """
            M=/data/local/tmp/.cegate.${'$'}${'$'}
            mkdir -p ${'$'}M || { echo "ERR mkdir"; exit 1; }
            mount -o ro $CE_FLASH ${'$'}M 2>/dev/null || { echo "ERR mount"; rmdir ${'$'}M; exit 1; }
            [ -f ${'$'}M/kernel.img ] || { echo "ERR nokernel"; umount ${'$'}M; rmdir ${'$'}M; exit 1; }
            $write
            SZ=${'$'}(stat -c %s ${'$'}M/kernel.img 2>/dev/null)
            [ -n "${'$'}SZ" ] && [ "${'$'}SZ" -gt 0 ] || { echo "ERR size"; umount ${'$'}M; rmdir ${'$'}M; exit 1; }
            echo "SIZE ${'$'}SZ"
            echo "SRC ${'$'}(md5sum ${'$'}M/kernel.img | cut -d' ' -f1)"
            [ -f ${'$'}M/kernel.img.md5 ] && echo "SHIPPED ${'$'}(cut -d' ' -f1 < ${'$'}M/kernel.img.md5)"
            FULL=${'$'}((SZ / 512)); REM=${'$'}((SZ - FULL * 512))
            echo "DST ${'$'}({ dd if=/dev/block/by-name/$part bs=512 count=${'$'}FULL 2>/dev/null; \
              [ "${'$'}REM" -gt 0 ] && dd if=/dev/block/by-name/$part bs=1 skip=${'$'}((FULL * 512)) count=${'$'}REM 2>/dev/null; \
              } | md5sum | cut -d' ' -f1)"
            umount ${'$'}M; rmdir ${'$'}M
        """.trimIndent()
    }

    private fun errState(code: String): State = when (code) {
        "mount" -> State.Unknown("cannot mount CE_FLASH")
        "nokernel" -> State.SourceBad("no /flash/kernel.img")
        "size" -> State.SourceBad("/flash/kernel.img is empty or unreadable")
        "write" -> State.Unknown("dd onto the boot partition failed")
        else -> State.Unknown(code)
    }

    private fun parse(out: String): Map<String, String> =
        out.lineSequence().mapNotNull { ln ->
            val s = ln.trim()
            val sp = s.indexOf(' ')
            if (sp <= 0) null else s.substring(0, sp) to s.substring(sp + 1).trim()
        }.toMap()

    private fun su(cmd: String): String? = try {
        val p = ProcessBuilder("su", "-c", cmd).redirectErrorStream(false).start()
        val out = p.inputStream.bufferedReader().readText()
        p.waitFor()
        out
    } catch (e: Exception) {
        Log.e(TAG, "su failed", e)
        null
    }
}
