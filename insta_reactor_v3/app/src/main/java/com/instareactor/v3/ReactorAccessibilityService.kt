package com.instareactor.v3

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The V3 device layer — the no-PC replacement for uiautomator2/ADB.
 *
 * It exposes exactly the primitive surface the Python `AccessibilityDevice`
 * calls through Chaquopy (see device/accessibility_device.py → the
 * `AccessibilityBridge` protocol). All selector logic, geometry and
 * Device-shaped behaviour live in Python; this class only reads the raw node
 * list, fires one gesture, or sets text. Method names and the JSON node
 * contract are mirrored on the Python side — keep the two in sync.
 *
 * This is carried over unchanged in behaviour from the P0/P1 spike (which
 * verified it on a real device); the only thing dropped is P0's floating
 * overlay test-rig — in V3 the Compose UI drives runs, not a floating bar.
 *
 * THREADING: the gesture methods block until the gesture completes, so they
 * MUST be called off the main thread (the engine runs on RunService's
 * background thread). They post the dispatch to the main thread and await the
 * callback, so calling them ON the main thread would deadlock.
 */
class ReactorAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: ReactorAccessibilityService? = null
            private set

        const val IG_PACKAGE = "com.instagram.android"
    }

    private val main = Handler(Looper.getMainLooper())

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    // =====================================================================
    // DEVICE BRIDGE — the primitive-only surface Python calls via Chaquopy.
    // =====================================================================

    /** Foreground app package, or "" if nothing is readable. */
    fun currentPackage(): String =
        rootInActiveWindow?.packageName?.toString() ?: ""

    /** [width, height] of the active window in pixels. */
    fun windowSize(): IntArray {
        val (w, h) = screenSize()
        return intArrayOf(w, h)
    }

    /**
     * The full node tree of `rootInActiveWindow` as a JSON array, one flat
     * object per node in depth-first pre-order. Contract (mirrored in
     * device/accessibility_device.py → AccessibilityBridge):
     *   {cls, text, desc, id, bounds:[l,t,r,b], clickable, scrollable,
     *    longClickable, enabled}
     * Blank strings are "" (never null). Returns "[]" when nothing is readable.
     */
    fun nodesJson(): String {
        val arr = JSONArray()
        appendNodeJson(rootInActiveWindow, arr)
        return arr.toString()
    }

    private fun appendNodeJson(node: AccessibilityNodeInfo?, arr: JSONArray) {
        if (node == null) return
        val b = Rect().also { node.getBoundsInScreen(it) }
        val obj = JSONObject()
            .put("cls", node.className?.toString() ?: "")
            .put("text", node.text?.toString() ?: "")
            .put("desc", node.contentDescription?.toString() ?: "")
            .put("id", node.viewIdResourceName ?: "")
            .put("bounds", JSONArray().put(b.left).put(b.top).put(b.right).put(b.bottom))
            .put("clickable", node.isClickable)
            .put("scrollable", node.isScrollable)
            .put("longClickable", node.isLongClickable)
            .put("enabled", node.isEnabled)
        arr.put(obj)
        for (i in 0 until node.childCount) appendNodeJson(node.getChild(i), arr)
    }

    /** Single tap. Blocks until dispatched; returns whether it completed. */
    fun tap(x: Int, y: Int): Boolean {
        val p = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        return dispatchBlocking(GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(p, 0, 60)).build())
    }

    fun longPress(x: Int, y: Int, durationMs: Long): Boolean {
        val p = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        return dispatchBlocking(GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(p, 0, durationMs)).build())
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long): Boolean {
        val p = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat())
        }
        return dispatchBlocking(GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(p, 0, durationMs)).build())
    }

    /**
     * Replace the focused editable field's contents with `text` (matches
     * U2Device.input_text(clear=True)). Falls back to the first editable node in
     * the tree if nothing holds input focus. Returns whether the set succeeded.
     */
    fun inputText(text: String): Boolean {
        val target = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: firstEditable(rootInActiveWindow)
            ?: return false
        val args = Bundle().apply {
            putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    private fun firstEditable(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.isEditable) return node
        for (i in 0 until node.childCount) {
            firstEditable(node.getChild(i))?.let { return it }
        }
        return null
    }

    fun pressBack(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)

    fun pressHome(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)

    /** Launch an app by package name (used by the navigator to open Instagram). */
    fun startApp(packageName: String): Boolean {
        val intent = packageManager.getLaunchIntentForPackage(packageName)
            ?: return false
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return runCatching { startActivity(intent); true }.getOrDefault(false)
    }

    // ---------------------------------------------------------------------

    private fun screenSize(): Pair<Int, Int> {
        val root = rootInActiveWindow ?: return 1080 to 1920
        val r = Rect().also { root.getBoundsInScreen(it) }
        return r.width() to r.height()
    }

    /**
     * Dispatch a gesture and block the calling (background) thread until it
     * completes, cancels, or times out. The dispatch + callback run on the main
     * thread; the caller must NOT be the main thread (see THREADING note above).
     */
    private fun dispatchBlocking(gesture: GestureDescription, timeoutMs: Long = 5000): Boolean {
        val latch = CountDownLatch(1)
        val result = AtomicBoolean(false)
        val posted = main.post {
            val cb = object : GestureResultCallback() {
                override fun onCompleted(g: GestureDescription?) { result.set(true); latch.countDown() }
                override fun onCancelled(g: GestureDescription?) { result.set(false); latch.countDown() }
            }
            if (!dispatchGesture(gesture, cb, null)) { result.set(false); latch.countDown() }
        }
        if (!posted) return false
        return try {
            if (latch.await(timeoutMs, TimeUnit.MILLISECONDS)) result.get() else false
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }
    }
}
