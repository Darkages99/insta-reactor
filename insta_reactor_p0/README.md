# InstaReactor — P0 spike (no-PC AccessibilityService)

This is the **P0** step from `docs/V3_PLAN.md §7`. It is a throwaway spike whose
only job is to **de-risk the whole V3 thesis** before we build anything on top of
it. It answers two questions, with **zero PC** in the loop:

1. **Can we read Instagram's live node tree** the way uiautomator2 did over ADB —
   and does each node actually carry the fields V2's `UiNode` needs
   (`text`, `desc`, `resource_id`/view-id, `class`, `bounds`, `clickable`)?
   → maps directly onto the table in `docs/V3_PLAN.md §3`.
2. **Can we dispatch a real tap / swipe** onto a reel via `dispatchGesture`
   (the primitive behind V3's `tap()` / `swipe()` / swipe-to-react)?

If both work, the V3 plan is sound and P1 (`AccessibilityDevice`) is just filling
in the `Device` ABC. If swipe-to-react or node reads misbehave, we find out now.

This app does **not** embed the Python engine, Chaquopy, or any decision logic —
that's P1+. It is engine-agnostic on purpose.

## Build

Needs Android Studio's SDK (path already in `local.properties`) and JDK 17.

```
cd "D:\reactor BETA\insta_reactor_p0"
gradlew.bat assembleDebug        # or: Open in Android Studio > Run
```

The APK lands in `app/build/outputs/apk/debug/app-debug.apk`. Install it on the
phone (Android Studio "Run", or `adb install` once — after that the app is
self-sufficient and needs no PC).

## Use (on the phone, no PC)

The controls live in a **floating bar that sits on top of Instagram** — you
can't tap buttons inside this app *and* have IG in the foreground at the same
time (whichever app you're touching is the "active window" the service reads).
The overlay solves that: it floats over IG without stealing focus, so IG stays
the window we read and act on.

1. Open **InstaReactor P0**.
2. Tap **Enable service** → turn on **InstaReactor P0** under Accessibility →
   back out. Status should read **CONNECTED ✓** and a small black **floating
   bar** appears (drag it by the `⠿` handle so it never covers the reel).
3. Open **Instagram**, go into a **DM that has a reel**, and put the reel on
   screen. The floating bar stays visible on top.
4. Use the floating bar (IG stays in front the whole time):
   - **Dump** — reads IG's current node tree, saves it to
     `Android/data/com.instareactor.p0/files/iurtree-<timestamp>.txt`, and
     toasts the node count. Re-open this app → **Reload latest dump** to read it.
   - **2×Tap** — double-tap at ~centre (IG's "like" gesture).
   - **Swipe** — a vertical swipe (reel scroll / reaction reveal).

> Gestures fire at generic screen fractions in P0. Getting the exact
> swipe-to-react choreography right is P2 — here we only prove the gesture
> *dispatch* lands as a real touch, and that we can read IG's tree while IG is
> in front.

If the floating bar doesn't appear, toggle it with **Show floating controls**
in the app.

## What to report back (P0 exit criteria)

- ✅/❌ Does **Dump tree** show IG nodes with usable `text` / `desc` / `id` /
  `bounds`? Paste a chunk of a reel-screen dump — that tells us how obfuscated
  the view-ids are and which fields we can actually select on.
- ✅/❌ Does **Double-tap** register as a like on the reel?
- ✅/❌ Does **Swipe up** move the reel / reveal reactions?

Once we have that, P1 turns these primitives into the `AccessibilityDevice`
that satisfies the `Device` ABC.

## P1 device bridge (done)

`ReactorAccessibilityService` now also exposes a **synchronous, primitive-only
bridge** that the Python `AccessibilityDevice`
(`insta_reactor/device/accessibility_device.py`) calls via Chaquopy to satisfy
the `Device` ABC: `currentPackage()`, `windowSize()`, `nodesJson()`, `tap()`,
`longPress()`, `swipe()`, `inputText()`, `pressBack()`, `pressHome()`,
`startApp()`. The node contract is a JSON array (one flat object per node in
depth-first pre-order) mirrored on both sides.

Gesture methods **block until the gesture completes** (they post the dispatch to
the main thread and await a `CountDownLatch`), so Python's blocking `Device` API
works — therefore they **must be called off the main thread**. Under Chaquopy
the engine runs on a background thread, so that holds. The P0 overlay/dump path
is unchanged. Actual Chaquopy embedding of the Python engine is P3 (the app
shell) — this module just provides the Kotlin surface P3 will bind to.
