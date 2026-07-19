package com.instareactor.p0

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Context
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.Rect
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
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

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

    private fun toast(msg: String) {
        main.post { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
    }
}
