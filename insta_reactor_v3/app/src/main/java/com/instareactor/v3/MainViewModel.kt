package com.instareactor.v3

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONArray
import org.json.JSONObject

/**
 * Holds the editable config and talks to the engine through [EngineBridge].
 *
 * Config is round-tripped as JSON: we keep the full config object the engine
 * returned ([raw]) and only overwrite the handful of keys the UI edits, so
 * settings the UI doesn't surface are preserved verbatim on save.
 */
data class ReviewRow(
    val chat: String,
    val reelId: String,
    val kind: String,
    val reason: String,
)

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val dataDir = Store.dataDir(app)
    private var raw = JSONObject()

    // --- edited fields ---
    var emojiPrefs by mutableStateOf("")
    var commonReplies by mutableStateOf("")
    var replyStyle by mutableStateOf("single")
    var minComments by mutableStateOf("15")
    var maxNewReels by mutableStateOf("3")
    var useModel by mutableStateOf(false)
    var enabledChats by mutableStateOf("")

    var saveMsg by mutableStateOf<String?>(null)
    var reviewItems by mutableStateOf<List<ReviewRow>>(emptyList())

    init {
        loadConfig()
        refreshReview()
    }

    /** Whether our accessibility service is connected (enabled by the user). */
    fun serviceEnabled(): Boolean = ReactorAccessibilityService.instance != null

    fun loadConfig() {
        val res = JSONObject(EngineBridge.loadConfig(dataDir))
        if (!res.optBoolean("ok")) return
        raw = res.getJSONObject("config")
        val profile = raw.optJSONObject("profile") ?: JSONObject()
        val settings = raw.optJSONObject("settings") ?: JSONObject()

        emojiPrefs = joinArray(profile.optJSONArray("emoji_prefs"), ", ")
        commonReplies = joinArray(profile.optJSONArray("common_replies"), "\n")
        replyStyle = profile.optString("reply_style", "single")
        minComments = settings.optInt("min_comments", 15).toString()
        maxNewReels = settings.optInt("max_new_reels", 3).toString()
        useModel = settings.optBoolean("use_model", false)
        enabledChats = joinArray(raw.optJSONArray("enabled_chats"), "\n")
    }

    fun saveConfig() {
        val profile = raw.optJSONObject("profile") ?: JSONObject().also { raw.put("profile", it) }
        val settings = raw.optJSONObject("settings") ?: JSONObject().also { raw.put("settings", it) }

        profile.put("emoji_prefs", splitToArray(emojiPrefs, ","))
        profile.put("common_replies", splitToArray(commonReplies, "\n"))
        profile.put("reply_style", replyStyle)
        settings.put("min_comments", minComments.toIntOrNull() ?: 15)
        settings.put("max_new_reels", maxNewReels.toIntOrNull() ?: 3)
        settings.put("use_model", useModel)
        raw.put("enabled_chats", splitToArray(enabledChats, "\n"))

        val res = JSONObject(EngineBridge.saveConfig(dataDir, raw.toString()))
        saveMsg = if (res.optBoolean("ok")) "Saved." else "Save failed: ${res.optString("error")}"
    }

    fun refreshReview() {
        val res = JSONObject(EngineBridge.reviewQueue(dataDir))
        if (!res.optBoolean("ok")) return
        val arr = res.optJSONArray("items") ?: JSONArray()
        reviewItems = (0 until arr.length()).map { i ->
            val it = arr.getJSONObject(i)
            ReviewRow(
                chat = it.optString("chat_name"),
                reelId = it.optString("reel_id"),
                kind = it.optString("flag_kind"),
                reason = it.optString("reason"),
            )
        }
    }

    fun startRun(send: Boolean) {
        RunService.start(getApplication(), send)
    }

    // --- json <-> text helpers ---

    private fun joinArray(arr: JSONArray?, sep: String): String {
        if (arr == null) return ""
        return (0 until arr.length()).joinToString(sep) { arr.optString(it) }
    }

    private fun splitToArray(text: String, sep: String): JSONArray {
        val arr = JSONArray()
        text.split(sep).map { it.trim() }.filter { it.isNotEmpty() }.forEach { arr.put(it) }
        return arr
    }
}
