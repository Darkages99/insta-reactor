package com.instareactor.companion

import android.app.PendingIntent
import android.content.Context
import android.content.Intent

/**
 * Fires the existing `python -m insta_reactor` engine inside Termux via the
 * documented RUN_COMMAND intent (see Termux:API docs). Requires
 * `allow-external-apps=true` in ~/.termux/termux.properties on the phone
 * (one-time manual step — see SETUP.md).
 */
object TermuxRunner {

    private const val TERMUX_BASH = "/data/data/com.termux/files/usr/bin/bash"
    private const val ACTION_RUN_COMMAND = "com.termux.RUN_COMMAND"
    private const val TERMUX_SERVICE_PACKAGE = "com.termux"
    private const val TERMUX_SERVICE_CLASS = "com.termux.app.RunCommandService"

    /** Builds the single shell command run on-device inside Termux. */
    fun buildCommand(chatName: String, planOnly: Boolean): String {
        val escapedChat = chatName.replace("\"", "\\\"")
        val planFlag = if (planOnly) " --plan-only" else ""
        return "cd ~/insta_reactor && " +
            "python -m insta_reactor chats add \"$escapedChat\" && " +
            "python -m insta_reactor run --json$planFlag"
    }

    fun run(context: Context, chatName: String, planOnly: Boolean) {
        val command = buildCommand(chatName, planOnly)

        val resultIntent = Intent(context, RunResultReceiver::class.java)
        val pendingIntent = PendingIntent.getBroadcast(
            context, 0, resultIntent,
            PendingIntent.FLAG_MUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val intent = Intent(ACTION_RUN_COMMAND)
        intent.setClassName(TERMUX_SERVICE_PACKAGE, TERMUX_SERVICE_CLASS)
        intent.putExtra("com.termux.RUN_COMMAND_PATH", TERMUX_BASH)
        intent.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arrayOf("-lc", command))
        intent.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
        intent.putExtra("com.termux.RUN_COMMAND_PENDING_INTENT", pendingIntent)
        context.startForegroundService(intent)
    }
}
