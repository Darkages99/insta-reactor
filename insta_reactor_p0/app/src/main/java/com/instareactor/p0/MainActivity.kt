package com.instareactor.p0

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.instareactor.p0.databinding.ActivityMainBinding
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * P0 control panel. No PC anywhere: you enable the accessibility service once,
 * open a reel in an Instagram DM, then use these buttons to (a) read IG's live
 * node tree and (b) fire scripted gestures at it.
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
        binding.dumpButton.setOnClickListener { onDump() }
        binding.doubleTapButton.setOnClickListener { onDoubleTap() }
        binding.swipeUpButton.setOnClickListener { onSwipeUp() }
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun service(): ReactorAccessibilityService? = ReactorAccessibilityService.instance

    private fun refreshStatus() {
        val on = service() != null
        binding.statusText.text = if (on) {
            "Service: CONNECTED ✓  — open a reel in an IG DM, then Dump / react."
        } else {
            "Service: OFF — tap \"Enable service\", turn on \"InstaReactor P0\", " +
                "then come back."
        }
        binding.dumpButton.isEnabled = on
        binding.doubleTapButton.isEnabled = on
        binding.swipeUpButton.isEnabled = on
    }

    private fun onDump() {
        val svc = service() ?: return
        val dump = svc.dumpTree()
        binding.outputText.text = dump

        // Persist it too, so a tree captured mid-scroll can be studied later —
        // still no PC needed; the file lives in the app's own storage.
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val file = File(getExternalFilesDir(null), "iurtree-$stamp.txt")
        runCatching { file.writeText(dump) }
            .onSuccess { toast("Saved: ${file.name}") }
            .onFailure { toast("Dump shown (save failed: ${it.message})") }
    }

    private fun onDoubleTap() {
        val svc = service() ?: return
        val (w, h) = svc.screenSize()
        val cx = w / 2f
        val cy = h * 0.45f  // upper-middle: where a reel bubble usually sits
        binding.statusText.text = "Double-tapping ($cx, $cy)…"
        svc.doubleTap(cx, cy) { ok ->
            runOnUiThread {
                binding.statusText.text =
                    if (ok) "Double-tap dispatched ✓ (did IG register a like?)"
                    else "Double-tap FAILED — gesture was cancelled/refused."
            }
        }
    }

    private fun onSwipeUp() {
        val svc = service() ?: return
        val (w, h) = svc.screenSize()
        val x = w / 2f
        binding.statusText.text = "Swiping up…"
        svc.swipe(x, h * 0.70f, x, h * 0.30f, durationMs = 250) { ok ->
            runOnUiThread {
                binding.statusText.text =
                    if (ok) "Swipe-up dispatched ✓ (did the reel move?)"
                    else "Swipe FAILED — gesture was cancelled/refused."
            }
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
