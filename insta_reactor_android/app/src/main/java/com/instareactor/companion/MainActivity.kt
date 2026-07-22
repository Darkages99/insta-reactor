package com.instareactor.companion

import android.content.SharedPreferences
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.instareactor.companion.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences("insta_reactor_companion", MODE_PRIVATE)
        binding.chatNameInput.setText(prefs.getString("last_chat_name", ""))

        binding.runButton.setOnClickListener { onRunClicked() }
    }

    override fun onResume() {
        super.onResume()
        RunResultBus.setListener { outcome -> runOnUiThread { showOutcome(outcome) } }
    }

    override fun onPause() {
        super.onPause()
        RunResultBus.setListener(null)
    }

    private fun onRunClicked() {
        val chatName = binding.chatNameInput.text.toString().trim()
        if (chatName.isEmpty()) {
            binding.statusText.text = "Enter a chat/user name first."
            return
        }
        prefs.edit().putString("last_chat_name", chatName).apply()

        val planOnly = binding.dryRunCheckbox.isChecked
        binding.statusText.text = if (planOnly) "Running (dry run)…" else "Running…"
        binding.resultsText.text = ""
        binding.runButton.isEnabled = false

        TermuxRunner.run(this, chatName, planOnly)
    }

    private fun showOutcome(outcome: RunOutcome) {
        binding.runButton.isEnabled = true

        if (outcome.exitCode != 0) {
            binding.statusText.text = "Error (exit ${outcome.exitCode})"
            binding.resultsText.text = outcome.stderr.ifBlank { outcome.stdout }
            return
        }

        val summary = RunSummaryParser.parse(outcome.stdout)
        if (summary == null) {
            binding.statusText.text = "Done, but couldn't parse the result."
            binding.resultsText.text = outcome.stdout
            return
        }

        binding.statusText.text =
            "Done — ${summary.autoReplied.size} reacted, ${summary.flagged.size} flagged" +
                if (summary.planOnly) " (dry run)" else ""

        val sb = StringBuilder()
        if (summary.autoReplied.isNotEmpty()) {
            sb.append("REACTED\n")
            for (item in summary.autoReplied) {
                sb.append("  [${item.chat}] ${item.reelId}: ${item.reply}\n")
            }
            sb.append("\n")
        }
        if (summary.flagged.isNotEmpty()) {
            sb.append("FLAGGED FOR REVIEW\n")
            for (item in summary.flagged) {
                sb.append("  [${item.chat}] ${item.reelId}: ${item.label} — ${item.reason}\n")
            }
        }
        if (sb.isEmpty()) sb.append("Nothing to do — no new reels.")
        binding.resultsText.text = sb.toString()
    }
}
