package com.instareactor.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Receives the RUN_COMMAND callback Termux sends back to the PendingIntent
 * we supplied in [TermuxRunner.run]. Extras follow Termux:API's documented
 * "result bundle" contract (stdout/stderr/exit code).
 */
class RunResultReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val bundle = intent.getBundleExtra("result") ?: intent.extras
        val stdout = bundle?.getString("stdout").orEmpty()
        val stderr = bundle?.getString("stderr").orEmpty()
        val exitCode = bundle?.getInt("exitCode", -1) ?: -1

        RunResultBus.post(RunOutcome(exitCode = exitCode, stdout = stdout, stderr = stderr))
    }
}

data class RunOutcome(val exitCode: Int, val stdout: String, val stderr: String)

/**
 * Tiny in-process pub/sub so the receiver (which may run detached from any
 * visible Activity) can hand results to MainActivity if it's alive, without
 * pulling in a DI framework for a one-screen app.
 */
object RunResultBus {
    private var listener: ((RunOutcome) -> Unit)? = null
    private var pending: RunOutcome? = null

    fun setListener(l: ((RunOutcome) -> Unit)?) {
        listener = l
        if (l != null) {
            pending?.let { l(it); pending = null }
        }
    }

    fun post(outcome: RunOutcome) {
        val l = listener
        if (l != null) l(outcome) else pending = outcome
    }
}
