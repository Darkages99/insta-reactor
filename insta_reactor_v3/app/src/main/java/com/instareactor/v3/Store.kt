package com.instareactor.v3

import android.content.Context

/**
 * The single on-device storage location. Everything the engine persists
 * (config.json, review_queue.json, handled_reels.json, engine.log) lives in the
 * app's private files dir — never leaves the phone, never touches shared storage.
 */
object Store {
    fun dataDir(context: Context): String = context.filesDir.absolutePath
}
