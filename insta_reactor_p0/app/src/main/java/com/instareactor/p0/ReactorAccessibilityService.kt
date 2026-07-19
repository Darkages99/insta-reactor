package com.instareactor.p0

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * P0 spike service.
 *
 * This is the no-PC replacement for uiautomator2/ADB. It proves the two
 * primitives every V3 `Device` method is built on:
 *
 *   1. READ  — walk `rootInActiveWindow` and expose each node's
 *              text / desc / view-id / class / bounds / clickable. This is the
 *              exact data the V2 `UiNode` carries, so it validates the field
 *              mapping in docs/V3_PLAN.md §3.
 *   2. WRITE — dispatch a real tap / swipe via `dispatchGesture`. This is what
 *              V3's `tap()` / `swipe()` / swipe-to-react will call.
 *
 * The Activity talks to the live service through [instance]. An
 * AccessibilityService is a singleton the system owns; we just publish a handle
 * to it while it's connected.
 */
class ReactorAccessibilityService : AccessibilityService() {

    companion object {
        /** Non-null only while the service is connected (user enabled it). */
        @Volatile
        var instance: ReactorAccessibilityService? = null
            private set

        const val IG_PACKAGE = "com.instagram.android"
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    // We don't react to events in P0 — the Activity drives everything manually.
    // (V3 will likely stay pull-based too: read on demand, not event-driven.)
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    // ---------------------------------------------------------------------
    // READ: dump the live node tree
    // ---------------------------------------------------------------------

    /**
     * Returns a human-readable dump of the current foreground window's node
     * tree. Each line is one node: indentation = depth, followed by the fields
     * that map 1:1 onto V2's `UiNode`.
     */
    fun dumpTree(): String {
        val root = rootInActiveWindow
            ?: return "rootInActiveWindow is null.\n\n" +
                "Nothing to read. Make sure Instagram is in the foreground and " +
                "the service is enabled, then try again."

        val sb = StringBuilder()
        val pkg = root.packageName ?: "?"
        sb.append("package: ").append(pkg).append('\n')
        if (pkg != IG_PACKAGE) {
            sb.append("(note: foreground app is not Instagram — the service is ")
                .append("scoped to IG, so open a reel in a DM first)\n")
        }
        sb.append("nodes:\n")
        var count = 0
        count = walk(root, 0, sb, count)
        sb.append("\n").append(count).append(" nodes.")
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

    /** Screen size, so the Activity can script gestures at sensible fractions. */
    fun screenSize(): Pair<Int, Int> {
        val root = rootInActiveWindow ?: return 1080 to 1920
        val r = Rect().also { root.getBoundsInScreen(it) }
        return r.width() to r.height()
    }

    // ---------------------------------------------------------------------
    // WRITE: dispatch gestures
    // ---------------------------------------------------------------------

    /** A tap at an absolute screen coordinate. */
    fun tap(x: Float, y: Float, onDone: (Boolean) -> Unit) {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, 60)
        dispatch(GestureDescription.Builder().addStroke(stroke).build(), onDone)
    }

    /**
     * A double-tap at an absolute coordinate — Instagram's classic "like".
     * Two quick strokes at the same point.
     */
    fun doubleTap(x: Float, y: Float, onDone: (Boolean) -> Unit) {
        val p = Path().apply { moveTo(x, y) }
        val first = GestureDescription.StrokeDescription(p, 0, 50)
        val second = GestureDescription.StrokeDescription(p, 120, 50)
        dispatch(
            GestureDescription.Builder().addStroke(first).addStroke(second).build(),
            onDone,
        )
    }

    /** A swipe from one absolute point to another over [durationMs]. */
    fun swipe(
        x1: Float, y1: Float, x2: Float, y2: Float,
        durationMs: Long, onDone: (Boolean) -> Unit,
    ) {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        dispatch(GestureDescription.Builder().addStroke(stroke).build(), onDone)
    }

    private fun dispatch(gesture: GestureDescription, onDone: (Boolean) -> Unit) {
        val callback = object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) = onDone(true)
            override fun onCancelled(g: GestureDescription?) = onDone(false)
        }
        val ok = dispatchGesture(gesture, callback, null)
        if (!ok) onDone(false)
    }
}
