package com.instareactor.p0

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.appcompat.app.AppCompatActivity
import com.instareactor.p0.databinding.ActivityMainBinding
import java.io.File

/**
 * P0 launcher / reader.
 *
 * The actual testing is driven from the floating overlay (which sits on top of
 * Instagram — see the service). This screen just:
 *   - shortcuts you into Accessibility settings to enable the service,
 *   - toggles the floating control bar,
 *   - and shows the most recent node dump the overlay saved, so you can read
 *     the tree without a PC.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.enableButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        binding.overlayButton.setOnClickListener { toggleOverlay() }
        binding.refreshButton.setOnClickListener { loadLatestDump() }
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        loadLatestDump()
    }

    private fun service(): ReactorAccessibilityService? = ReactorAccessibilityService.instance

    private fun refreshStatus() {
        val svc = service()
        binding.statusText.text = when {
            svc == null ->
                "Service: OFF — tap \"Enable service\", turn on \"InstaReactor P0\", come back."
            svc.overlayShown() ->
                "Service: CONNECTED ✓ — floating bar is up. Open a reel in an IG DM " +
                    "and use the bar's Dump / 2×Tap / Swipe."
            else ->
                "Service: CONNECTED ✓ — floating bar hidden. Tap \"Show floating controls\"."
        }
        binding.overlayButton.isEnabled = svc != null
        binding.overlayButton.text =
            if (svc?.overlayShown() == true) "Hide floating controls" else "Show floating controls"
    }

    private fun toggleOverlay() {
        val svc = service() ?: return
        if (svc.overlayShown()) svc.hideOverlay() else svc.showOverlay()
        refreshStatus()
    }

    /** Loads the newest iurtree-*.txt the overlay wrote, so you can read it here. */
    private fun loadLatestDump() {
        val dir = getExternalFilesDir(null)
        val latest = dir?.listFiles { f -> f.name.startsWith("iurtree-") }
            ?.maxByOrNull { it.lastModified() }
        binding.outputText.text = when {
            latest == null -> "No dump yet. Use the floating bar's \"Dump\" over an IG reel."
            else -> "── ${latest.name} ──\n\n" + runCatching { latest.readText() }
                .getOrElse { "(couldn't read: ${it.message})" }
        }
    }
}
