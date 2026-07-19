package com.instareactor.p0

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.Button
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * P0 spike service.
 *
 * This is the no-PC replacement for uiautomator2/ADB. It proves the two
 * primitives every V3 `Device` method is built on:
 *
 *   1. READ  — walk `rootInActiveWindow` and expose each node's
 *              text / desc / view-id / class / bounds / clickable (the exact
 *              data V2's `UiNode` carries — docs/V3_PLAN.md §3).
 *   2. WRITE — dispatch a real tap / swipe via `dispatchGesture`
 *              (V3's `tap()` / `swipe()` / swipe-to-react).
 *
 * The catch a plain Activity hits: to read/act on Instagram, IG must be the
 * foreground (active) window — but tapping a button inside our own Activity
 * makes *us* the foreground window instead. So the controls live in a
 * **floating overlay** (`TYPE_ACCESSIBILITY_OVERLAY`) drawn on top of IG. The
 * overlay is non-focusable, so Instagram stays the active window while you tap.
 */
class ReactorAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: ReactorAccessibilityService? = null
            private set

        const val IG_PACKAGE = "com.instagram.android"
    }

    private val main = Handler(Looper.getMainLooper())
    private var overlay: View? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        showOverlay()
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        hideOverlay()
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        hideOverlay()
        instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    // ---------------------------------------------------------------------
    // Floating overlay: the control bar that sits on top of Instagram
    // ---------------------------------------------------------------------

    fun overlayShown(): Boolean = overlay != null

    fun showOverlay() {
        if (overlay != null) return
        val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val view = LayoutInflater.from(this).inflate(R.layout.overlay_controls, null)

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            // NOT_FOCUSABLE keeps Instagram the active window we read/act on.
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 0
            y = 40
        }

        view.findViewById<Button>(R.id.ovDump).setOnClickListener { dumpTreeToFile() }
        view.findViewById<Button>(R.id.ovDoubleTap).setOnClickListener { scriptedDoubleTap() }
        view.findViewById<Button>(R.id.ovSwipe).setOnClickListener { scriptedSwipeUp() }
        makeDraggable(view.findViewById(R.id.ovHandle), view, params, wm)

        wm.addView(view, params)
        overlay = view
    }

    fun hideOverlay() {
        val v = overlay ?: return
        val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        runCatching { wm.removeView(v) }
        overlay = null
    }

    /** Drag the whole bar by its handle so it never covers the reel. */
    private fun makeDraggable(
        handle: View,
        root: View,
        params: WindowManager.LayoutParams,
        wm: WindowManager,
    ) {
        var startX = 0
        var startY = 0
        var touchX = 0f
        var touchY = 0f
        handle.setOnTouchListener { _, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = params.x; startY = params.y
                    touchX = e.rawX; touchY = e.rawY
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    params.x = startX + (e.rawX - touchX).toInt()
                    params.y = startY + (e.rawY - touchY).toInt()
                    wm.updateViewLayout(root, params)
                    true
                }
                else -> false
            }
        }
    }

    // ---------------------------------------------------------------------
    // READ: dump the live node tree
    // ---------------------------------------------------------------------

    /** Builds the dump, writes it to the app files dir, and toasts the result. */
    fun dumpTreeToFile() {
        val dump = dumpTree()
        val nodes = dump.substringAfterLast('\n').trim()
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val file = File(getExternalFilesDir(null), "iurtree-$stamp.txt")
        val msg = runCatching { file.writeText(dump); "$nodes → ${file.name}" }
            .getOrElse { "dump built but save failed: ${it.message}" }
        toast(msg)
    }

    /**
     * Human-readable dump of the current foreground window's node tree. Each
     * line = one node: indentation is depth, fields map 1:1 onto V2's `UiNode`.
     */
    fun dumpTree(): String {
        val root = rootInActiveWindow
            ?: return "rootInActiveWindow is null.\nOpen a reel in an IG DM, then Dump.\n0 nodes."

        val sb = StringBuilder()
        val pkg = root.packageName?.toString() ?: "?"
        sb.append("package: ").append(pkg).append('\n')
        if (pkg != IG_PACKAGE) {
            sb.append("(foreground app is not Instagram — open a reel in a DM first)\n")
        }
        sb.append("nodes:\n")
        val count = walk(root, 0, sb, 0)
        sb.append('\n').append(count).append(" nodes")
        return sb.toString()
    }

    private fun walk(
        node: AccessibilityNodeInfo?,
        depth: Int,
        sb: StringBuilder,
        countSoFar: Int,
    ): Int {
        if (node == null) return countSoFar
        var count = countSoFar + 1

        val indent = "  ".repeat(depth)
        val bounds = Rect().also { node.getBoundsInScreen(it) }
        val text = node.text?.toString()?.takeIf { it.isNotBlank() }
        val desc = node.contentDescription?.toString()?.takeIf { it.isNotBlank() }
        val viewId = node.viewIdResourceName?.takeIf { it.isNotBlank() }
        val clazz = node.className?.toString()

        sb.append(indent)
        sb.append(clazz?.substringAfterLast('.') ?: "?")
        sb.append(" [").append(bounds.left).append(',').append(bounds.top)
            .append("][").append(bounds.right).append(',').append(bounds.bottom).append(']')
        if (node.isClickable) sb.append(" CLICKABLE")
        if (node.isScrollable) sb.append(" SCROLLABLE")
        if (text != null) sb.append("  text=\"").append(text).append('"')
        if (desc != null) sb.append("  desc=\"").append(desc).append('"')
        if (viewId != null) sb.append("  id=").append(viewId.substringAfterLast('/'))
        sb.append('\n')

        for (i in 0 until node.childCount) {
            count = walk(node.getChild(i), depth + 1, sb, count)
        }
        return count
    }

    private fun screenSize(): Pair<Int, Int> {
        val root = rootInActiveWindow ?: return 1080 to 1920
        val r = Rect().also { root.getBoundsInScreen(it) }
        return r.width() to r.height()
    }

    // ---------------------------------------------------------------------
    // WRITE: dispatch gestures
    // ---------------------------------------------------------------------

    private fun scriptedDoubleTap() {
        val (w, h) = screenSize()
        doubleTap(w / 2f, h * 0.45f) { ok ->
            toast(if (ok) "Double-tap dispatched ✓ (did IG like it?)" else "Double-tap FAILED")
        }
    }

    private fun scriptedSwipeUp() {
        val (w, h) = screenSize()
        val x = w / 2f
        swipe(x, h * 0.70f, x, h * 0.30f, 250) { ok ->
            toast(if (ok) "Swipe-up dispatched ✓ (did the reel move?)" else "Swipe FAILED")
        }
    }

    fun tap(x: Float, y: Float, onDone: (Boolean) -> Unit) {
        val path = Path().apply { moveTo(x, y) }
        dispatch(GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 60)).build(), onDone)
    }

    fun doubleTap(x: Float, y: Float, onDone: (Boolean) -> Unit) {
        val p = Path().apply { moveTo(x, y) }
        dispatch(
            GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(p, 0, 50))
                .addStroke(GestureDescription.StrokeDescription(p, 120, 50))
                .build(),
            onDone,
        )
    }

    fun swipe(
        x1: Float, y1: Float, x2: Float, y2: Float,
        durationMs: Long, onDone: (Boolean) -> Unit,
    ) {
        val path = Path().apply { moveTo(x1, y1); lineTo(x2, y2) }
        dispatch(GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs)).build(), onDone)
    }

    private fun dispatch(gesture: GestureDescription, onDone: (Boolean) -> Unit) {
        val callback = object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) = onDone(true)
            override fun onCancelled(g: GestureDescription?) = onDone(false)
        }
        if (!dispatchGesture(gesture, callback, null)) onDone(false)
    }

    // =====================================================================
    // P1 DEVICE BRIDGE
    //
    // The synchronous, primitive-only surface the Python `AccessibilityDevice`
    // calls (via Chaquopy) to satisfy the `Device` ABC. Everything here is a
    // raw primitive — read the node list, fire one gesture, set text. All
    // selector logic / geometry / Device-shaped behaviour lives in Python
    // (device/accessibility_device.py). Method names & the JSON node contract
    // are mirrored there; keep the two in sync.
    //
    // THREADING: the gesture methods block until the gesture completes, so they
    // MUST be called off the main thread (the engine runs on a background
    // thread under Chaquopy). They post the actual dispatch to the main thread
    // and await the callback, so calling them ON the main thread would deadlock.
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

    private fun toast(msg: String) {
        main.post { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
    }
}
