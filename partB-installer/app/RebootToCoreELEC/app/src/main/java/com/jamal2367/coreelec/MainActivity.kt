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
                when (val r = EnvFlip.bootCoreElec(reboot = true)) {
                    is EnvFlip.Result.Ok -> Log.d(TAG, "boot_ce=1 set; rebooting")
                    is EnvFlip.Result.Err -> toast(getString(R.string.switch_failed, r.msg))
                }
                return@Thread
            }
            Log.w(TAG, "boot_ce gate missing or damaged -- attempting repair")
            handleGateResult(EnvGate.reassert(bootCe = 1, reboot = true))
        }.start()
    }

    private fun handleGateResult(r: EnvGate.GateResult) {
        when (r) {
            is EnvGate.GateResult.Ok ->
                Log.d(TAG, "gate re-asserted: ce=${r.ceSlot} default=${r.default} rebuilt=${r.rebuilt}")

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
                                EnvGate.reassert(bootCe = 1, allowRebuild = true, reboot = true)
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
