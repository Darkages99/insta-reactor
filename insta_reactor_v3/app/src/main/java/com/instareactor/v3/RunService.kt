package com.instareactor.v3

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/**
 * Runs one engine pass as a foreground service so it survives screen-off / Doze
 * while it drives Instagram. The actual work happens on a background thread
 * (the accessibility bridge's gestures block, so it must not be the main
 * thread); progress is published to [RunState] for the UI to observe.
 *
 * Start it with EXTRA_SEND=false for a dry run (default) or true to actually
 * dispatch replies.
 */
class RunService : Service() {

    companion object {
        const val EXTRA_SEND = "send"
        private const val CHANNEL_ID = "run"
        private const val NOTIF_ID = 1

        fun start(context: Context, send: Boolean) {
            val intent = Intent(context, RunService::class.java)
                .putExtra(EXTRA_SEND, send)
            context.startForegroundService(intent)
        }
    }

    @Volatile
    private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val send = intent?.getBooleanExtra(EXTRA_SEND, false) ?: false
        enterForeground(NOTIF_ID, buildNotification(
            if (send) "Reacting to reels…" else "Dry run — deciding, not sending…"))

        // Guard: don't start a second run on top of a live one.
        if (RunState.isRunning) {
            stopSelf(startId)
            return START_NOT_STICKY
        }
        RunState.set(RunPhase.Running)

        worker = Thread {
            val result = runCatching { doRun(send) }
            result.onSuccess { RunState.set(RunPhase.Done(it)) }
                .onFailure { RunState.set(RunPhase.Failed(it.message ?: it.toString())) }
            stopSelf(startId)
        }.also { it.start() }

        return START_NOT_STICKY
    }

    /** Off the main thread: hand the live accessibility service to the engine. */
    private fun doRun(send: Boolean): String {
        val service = ReactorAccessibilityService.instance
            ?: return """{"ok":false,"error":"Accessibility service is not enabled."}"""
        val dataDir = Store.dataDir(this)
        EngineBridge.initLogging(dataDir)
        return EngineBridge.run(service, dataDir, send)
    }

    override fun onDestroy() {
        worker?.interrupt()
        worker = null
        super.onDestroy()
    }

    // ---- notification ----------------------------------------------------

    private fun buildNotification(text: String): Notification {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val existing = mgr.getNotificationChannel(CHANNEL_ID)
            if (existing == null) {
                mgr.createNotificationChannel(NotificationChannel(
                    CHANNEL_ID, "Reaction runs", NotificationManager.IMPORTANCE_LOW))
            }
        }
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("INSTA REACTOR")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()
    }

    private fun enterForeground(id: Int, notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(id, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(id, notification)
        }
    }
}
