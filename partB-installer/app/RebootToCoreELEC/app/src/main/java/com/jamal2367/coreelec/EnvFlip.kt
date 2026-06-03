package com.jamal2367.coreelec

import java.io.DataInputStream
import java.util.zip.CRC32

/**
 * Flips the u-boot `boot_ce` env flag on the Xiaomi TV Stick 2nd Gen (twilight)
 * so the next boot goes to CoreELEC (boot_ce=1) or Android (boot_ce=0).
 *
 * The live u-boot env is partition `env` (mmcblk0p2), offset 0, size 0x10000
 * (64 KiB), NON-redundant:  [4-byte CRC32 LE][ key=val \0 ... \0 ][0x00 pad].
 * CRC32 covers bytes [4 : 0x10000].  (Verified; identical to the PC envtool.)
 *
 * Pure root + Kotlin -- no fw_setenv (Android lacks it), no native binary.
 * Reads the env via `su -c dd`, edits the single `boot_ce` value, recomputes the
 * CRC, writes it back, then reboots. All other env vars (incl. per-device
 * identity) are preserved byte-for-byte except boot_ce.
 */
object EnvFlip {
    private const val ENV_SIZE = 0x10000
    private const val ENV_DEV = "/dev/block/by-name/env"

    sealed class Result {
        object Ok : Result()
        data class Err(val msg: String) : Result()
    }

    /** Set boot_ce then (optionally) reboot. Runs blocking; call off the UI thread. */
    fun bootCoreElec(reboot: Boolean = true): Result = setBootCe(1, reboot)
    fun bootAndroid(reboot: Boolean = true): Result = setBootCe(0, reboot)

    fun setBootCe(value: Int, reboot: Boolean): Result {
        return try {
            val env = readEnv() ?: return Result.Err("read env failed")
            if (env.size < ENV_SIZE) return Result.Err("short env ${env.size}")
            if (!verifyCrc(env)) return Result.Err("env CRC invalid -- refusing to write")
            val newEnv = withBootCe(env, value)
            if (!writeEnv(newEnv)) return Result.Err("write env failed")
            val check = readEnv() ?: return Result.Err("verify read failed")
            if (!verifyCrc(check) || parse(check)["boot_ce"] != value.toString())
                return Result.Err("read-back mismatch")
            if (reboot) runSu("reboot")
            Result.Ok
        } catch (e: Exception) {
            Result.Err(e.message ?: e.toString())
        }
    }

    // ---- env codec (must match build/envtool.py) ----------------------------
    private fun parse(env: ByteArray): LinkedHashMap<String, String> {
        val m = LinkedHashMap<String, String>()
        var i = 4
        val sb = StringBuilder()
        while (i < ENV_SIZE) {
            val b = env[i].toInt() and 0xff
            if (b == 0) {
                if (sb.isEmpty()) break          // empty entry terminates the list
                val s = sb.toString()
                val eq = s.indexOf('=')
                if (eq >= 0) m[s.substring(0, eq)] = s.substring(eq + 1) else m[s] = ""
                sb.setLength(0)
            } else {
                sb.append(b.toChar())            // latin1
            }
            i++
        }
        return m
    }

    private fun serialize(map: LinkedHashMap<String, String>): ByteArray {
        // format: key=val \0 key=val \0 ... \0 (terminator) then 0x00 padding.
        // Separators/terminator are NUL (0x00), matching build/envtool.py exactly.
        val bodyBytes = ByteArray(ENV_SIZE - 4)        // zero-filled = padding
        var pos = 0
        for ((k, v) in map) {
            val entry = "$k=$v"
            if (pos + entry.length + 1 > bodyBytes.size) throw IllegalStateException("env overflow")
            for (ch in entry) bodyBytes[pos++] = (ch.code and 0xff).toByte()
            bodyBytes[pos++] = 0               // NUL between entries
        }
        bodyBytes[pos] = 0                     // empty entry terminates the list
        val crc = CRC32(); crc.update(bodyBytes)
        val c = crc.value
        val out = ByteArray(ENV_SIZE)
        out[0] = (c and 0xff).toByte()
        out[1] = ((c shr 8) and 0xff).toByte()
        out[2] = ((c shr 16) and 0xff).toByte()
        out[3] = ((c shr 24) and 0xff).toByte()
        System.arraycopy(bodyBytes, 0, out, 4, bodyBytes.size)
        return out
    }

    private fun withBootCe(env: ByteArray, value: Int): ByteArray {
        val m = parse(env)
        m["boot_ce"] = value.toString()
        return serialize(m)
    }

    private fun verifyCrc(env: ByteArray): Boolean {
        val stored = (env[0].toLong() and 0xff) or
                ((env[1].toLong() and 0xff) shl 8) or
                ((env[2].toLong() and 0xff) shl 16) or
                ((env[3].toLong() and 0xff) shl 24)
        val crc = CRC32(); crc.update(env, 4, ENV_SIZE - 4)
        return crc.value == stored
    }

    // ---- root I/O -----------------------------------------------------------
    private fun readEnv(): ByteArray? {
        val p = ProcessBuilder("su", "-c", "dd if=$ENV_DEV bs=4096 count=16")
            .redirectErrorStream(false).start()
        val din = DataInputStream(p.inputStream)
        val buf = ByteArray(ENV_SIZE)
        var read = 0
        while (read < ENV_SIZE) {
            val n = din.read(buf, read, ENV_SIZE - read)
            if (n < 0) break
            read += n
        }
        p.waitFor()
        return if (read >= ENV_SIZE) buf else null
    }

    private fun writeEnv(env: ByteArray): Boolean {
        // write exactly 64 KiB to offset 0 of the env partition
        val p = ProcessBuilder("su", "-c", "dd of=$ENV_DEV bs=4096 conv=fsync")
            .redirectErrorStream(true).start()
        p.outputStream.use { it.write(env); it.flush() }
        return p.waitFor() == 0
    }

    private fun runSu(cmd: String) {
        ProcessBuilder("su", "-c", cmd).start().waitFor()
    }
}
