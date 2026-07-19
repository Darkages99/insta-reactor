package com.instareactor.v3

import com.chaquo.python.PyObject
import com.chaquo.python.Python

/**
 * The Kotlin → Python boundary. Everything the app does with the engine goes
 * through the single Python module `v3_entry` (which re-exports
 * insta_reactor.app_entry). Every call returns a JSON **string** and never
 * throws across the boundary — the Python side wraps failures as
 * {"ok": false, "error": ...} — so callers here can treat the result as data.
 *
 * `dataDir` is the app's private files dir (see [Store]); all engine state
 * lives there and nothing leaves the device.
 */
object EngineBridge {

    private fun entry(): PyObject =
        Python.getInstance().getModule("v3_entry")

    /** Point engine logging at <dataDir>/engine.log. Safe to call repeatedly. */
    fun initLogging(dataDir: String) {
        entry().callAttr("init_logging", dataDir)
    }

    /** Current config (or defaults) as a JSON string. */
    fun loadConfig(dataDir: String): String =
        entry().callAttr("load_config", dataDir).toString()

    /** Persist a config JSON object (matches AppConfig). Returns a JSON result. */
    fun saveConfig(dataDir: String, configJson: String): String =
        entry().callAttr("save_config", dataDir, configJson).toString()

    /** Pending manual-review items as JSON. */
    fun reviewQueue(dataDir: String): String =
        entry().callAttr("review_queue", dataDir).toString()

    /**
     * Run the engine once and return a JSON summary. MUST be called off the main
     * thread (the accessibility bridge's gestures block). `send=false` is a dry
     * run — decisions are made and queued, but no reply is ever dispatched.
     *
     * The `bridge` is the live accessibility service; the Python
     * `AccessibilityDevice` duck-types it as the `AccessibilityBridge`.
     */
    fun run(bridge: ReactorAccessibilityService, dataDir: String, send: Boolean): String =
        entry().callAttr("run", bridge, dataDir, send).toString()
}
