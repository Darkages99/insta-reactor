package com.instareactor.companion

import org.json.JSONObject

data class AutoReplyItem(val chat: String, val reelId: String, val reply: String, val confidence: Double)
data class FlaggedItem(val chat: String, val reelId: String, val label: String, val reason: String)

data class RunSummary(
    val planOnly: Boolean,
    val autoReplied: List<AutoReplyItem>,
    val flagged: List<FlaggedItem>,
)

/**
 * Parses the trailing JSON line produced by `python -m insta_reactor run
 * --json` (see report.py:to_dict). Termux's RUN_COMMAND stdout may include
 * shell/session noise before the JSON, so we scan for the last '{' that
 * parses cleanly rather than assuming stdout is JSON-only.
 */
object RunSummaryParser {

    fun parse(stdout: String): RunSummary? {
        val candidate = extractJsonObject(stdout) ?: return null
        return try {
            val obj = JSONObject(candidate)
            val planOnly = obj.optBoolean("plan_only", false)

            val autoReplied = mutableListOf<AutoReplyItem>()
            val autoArr = obj.optJSONArray("auto_replied")
            if (autoArr != null) {
                for (i in 0 until autoArr.length()) {
                    val o = autoArr.getJSONObject(i)
                    autoReplied.add(
                        AutoReplyItem(
                            chat = o.optString("chat"),
                            reelId = o.optString("reel_id"),
                            reply = o.optString("reply"),
                            confidence = o.optDouble("confidence", 0.0),
                        )
                    )
                }
            }

            val flagged = mutableListOf<FlaggedItem>()
            val flagArr = obj.optJSONArray("flagged")
            if (flagArr != null) {
                for (i in 0 until flagArr.length()) {
                    val o = flagArr.getJSONObject(i)
                    flagged.add(
                        FlaggedItem(
                            chat = o.optString("chat"),
                            reelId = o.optString("reel_id"),
                            label = o.optString("label"),
                            reason = o.optString("reason"),
                        )
                    )
                }
            }

            RunSummary(planOnly = planOnly, autoReplied = autoReplied, flagged = flagged)
        } catch (e: Exception) {
            null
        }
    }

    private fun extractJsonObject(text: String): String? {
        val start = text.lastIndexOf('{')
        if (start == -1) return null
        val candidate = text.substring(start).trim()
        return if (candidate.startsWith("{") && candidate.endsWith("}")) candidate else null
    }
}
