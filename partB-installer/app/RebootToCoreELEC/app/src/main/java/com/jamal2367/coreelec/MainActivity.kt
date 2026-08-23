package com.jamal2367.coreelec

import android.app.Activity
import android.app.AlertDialog
import android.os.Bundle
import android.text.Html
import android.text.Html.FROM_HTML_MODE_LEGACY
import android.util.Log
import android.widget.Button
import android.widget.ImageButton
import android.widget.Toast

/**
 * Reboot to CoreELEC (internal dual-boot, twilight).
 *
 * One button: flip the u-boot `boot_ce` gate via root, then reboot into CoreELEC.
 * Done locally with `su` (no adb/TCP, no fw_setenv) -- see EnvFlip. The upstream
 * "first reboot / reboot update" (USB-recovery) button is removed: the internal
 * boot needs no USB step.
 *
 * The button also self-heals: a CoreELEC OS update can rewrite `bootcmd` to a stock
 * version that drops the `boot_ce` check, at which point flipping the flag does
 * nothing. Before flipping we check the gate and re-assert it if needed (see EnvGate),
 * so the PC-side `reassert_env_gate.py` is only needed when Android itself won't boot
 * or root is gone.
 */
class MainActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_coreelec)

        findViewById<Button>(R.id.btnReboot).setOnClickListener {
            bootCoreElecViaEnv()
        }

        findViewById<ImageButton>(R.id.btnRebootInfo).setOnClickListener {
            AlertDialog.Builder(this)
                .setTitle(R.string.information)
                .setMessage(Html.fromHtml(getString(R.string.reboot_to_coreelec_info), FROM_HTML_MODE_LEGACY))
                .create()
                .show()
        }
    }

    /**
     * Repair the gate if a CoreELEC update broke it, flip boot_ce=1, reboot -> CoreELEC.
     * Off the UI thread: every step shells out to `su`.
     */
    private fun bootCoreElecViaEnv() {
        Toast.makeText(this, getString(R.string.reboot_to_coreelec) + "...", Toast.LENGTH_SHORT).show()
        Thread {
            // Fast path: gate intact -> touch nothing but boot_ce, so a hand-tuned
            // bootcefromemmc (custom vout, extra bootargs) is preserved.
            if (EnvGate.gateIntact()) {
                if (!kernelImageUsable()) return@Thread
                when (val r = EnvFlip.bootCoreElec(reboot = true)) {
                    is EnvFlip.Result.Ok -> Log.d(TAG, "boot_ce=1 set; rebooting")
                    is EnvFlip.Result.Err -> toast(getString(R.string.switch_failed, r.msg))
                }
                return@Thread
            }
            Log.w(TAG, "boot_ce gate missing or damaged -- attempting repair")
            // Deliberately reboot=false. A CoreELEC update breaks the gate and re-syncs
            // boot_<slot> in the same pass, so this is the path MOST likely to be sitting on a
            // bad kernel image -- and bootcmd spends boot_ce before bootcefromemmc runs, so a
            // reboot into a bad kernel costs the flag as well as the boot. Reboot happens in
            // handleGateResult, after the image has been checked.
            handleGateResult(EnvGate.reassert(bootCe = 1, reboot = false))
        }.start()
    }

    /**
     * Second pre-flight: is the image the gate hands off to actually the CoreELEC kernel?
     *
     * A working gate is only half the handoff. A CoreELEC update re-syncs `boot_<slot>` from
     * /flash with a blind dd, and when that dd came up short on a real stick the gate kept
     * working perfectly while the kernel panicked on a ramdisk it could not unpack -- which
     * looked exactly like this button doing nothing.
     *
     * Fails OPEN. Only a VERIFIED mismatch we could not repair stops the reboot; if we cannot
     * tell (no CE_FLASH, mount refused) we behave as before rather than block a boot that would
     * have worked. A bad /flash/kernel.img is not itself a reason to stop: u-boot reads
     * `boot_<slot>`, so CoreELEC can boot fine with a corrupt copy sitting on /flash.
     *
     * @return true if it is safe to flip boot_ce and reboot.
     */
    private fun kernelImageUsable(): Boolean {
        val slot = KernelGate.ceSlot() ?: return true          // no gate to reason about
        when (val k = KernelGate.check(slot)) {
            is KernelGate.State.Ok -> return true

            is KernelGate.State.Unknown -> {
                Log.w(TAG, "kernel image not verifiable (${k.detail}) -- continuing anyway")
                return true
            }

            is KernelGate.State.SourceBad -> {
                Log.w(TAG, "/flash/kernel.img is not trustworthy (${k.detail}) -- continuing anyway")
                return true
            }

            is KernelGate.State.Stale -> {
                Log.w(TAG, "kernel image stale (${k.detail}) -- repairing before reboot")
                toast(getString(R.string.kernel_repairing))
                return when (val after = KernelGate.repair(slot)) {
                    is KernelGate.State.Ok -> {
                        Log.d(TAG, "kernel image repaired and verified")
                        true
                    }
                    // The repair is verified by reading the partition back, so anything else
                    // means boot_<slot> is still wrong. Rebooting now just panics the box and
                    // drops it back to Android with nothing to show for it.
                    is KernelGate.State.Stale -> { toast(getString(R.string.kernel_repair_failed, after.detail)); false }
                    is KernelGate.State.SourceBad -> { toast(getString(R.string.kernel_repair_failed, after.detail)); false }
                    is KernelGate.State.Unknown -> { toast(getString(R.string.kernel_repair_failed, after.detail)); false }
                }
            }
        }
    }

    private fun handleGateResult(r: EnvGate.GateResult) {
        when (r) {
            is EnvGate.GateResult.Ok -> {
                Log.d(TAG, "gate re-asserted: ce=${r.ceSlot} default=${r.default} rebuilt=${r.rebuilt}")
                // The gate is good again; now make sure it has something bootable to hand off
                // to. boot_ce is already set, so if the image cannot be made good we simply do
                // not reboot -- the flag survives for a later attempt.
                if (kernelImageUsable()) EnvFlip.runSu("reboot")
            }

            is EnvGate.GateResult.Err ->
                toast(getString(R.string.switch_failed, r.msg))

            is EnvGate.GateResult.NeedsRebuild -> runOnUiThread {
                // No gate survives, so the CoreELEC slot can only be inferred from the
                // inactive Android slot. That holds while OTA is blocked; if Android ever
                // slot-switched it names the WRONG slot, so make the user own the call.
                AlertDialog.Builder(this)
                    .setTitle(R.string.gate_missing_title)
                    .setMessage(getString(R.string.gate_missing_msg, r.activeSlot, r.inferredCeSlot))
                    .setNegativeButton(android.R.string.cancel, null)
                    .setPositiveButton(R.string.gate_missing_rebuild) { _, _ ->
                        Thread {
                            handleGateResult(
                                EnvGate.reassert(bootCe = 1, allowRebuild = true, reboot = false)
                            )
                        }.start()
                    }
                    .show()
            }
        }
    }

    private fun toast(msg: String) = runOnUiThread {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }

    private companion object {
        const val TAG = "MainActivity"
    }
}
