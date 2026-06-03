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

    /** Flip boot_ce=1 (root, local) then reboot -> CoreELEC. Off the UI thread. */
    private fun bootCoreElecViaEnv() {
        Toast.makeText(this, getString(R.string.reboot_to_coreelec) + "...", Toast.LENGTH_SHORT).show()
        Thread {
            when (val r = EnvFlip.bootCoreElec(reboot = true)) {
                is EnvFlip.Result.Ok -> Log.d("MainActivity", "boot_ce=1 set; rebooting")
                is EnvFlip.Result.Err -> runOnUiThread {
                    Toast.makeText(this, "CoreELEC switch failed: ${r.msg}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }
}
