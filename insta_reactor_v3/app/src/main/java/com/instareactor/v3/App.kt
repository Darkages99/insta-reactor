package com.instareactor.v3

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Starts the embedded CPython runtime once, for the whole process. Everything
 * that talks to the engine goes through [EngineBridge], which assumes this has
 * run.
 */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
    }
}
