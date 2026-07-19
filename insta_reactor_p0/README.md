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

1. Open **InstaReactor P0**.
2. Tap **Enable service** → turn on **InstaReactor P0** under Accessibility →
   back out. Status should read **CONNECTED ✓**.
3. Open **Instagram**, go into a **DM that has a reel**, and put the reel on
   screen.
4. Switch back to InstaReactor P0 (recents) and:
   - **Dump tree** — prints every node of IG's current screen and saves a copy to
     `Android/data/com.instareactor.p0/files/iurtree-<timestamp>.txt`.
   - **Double-tap** — fires a double-tap at ~centre (IG's "like" gesture).
   - **Swipe up** — fires a vertical swipe (reel scroll / reaction reveal).

> Gestures fire at generic screen fractions in P0. Getting the exact
> swipe-to-react choreography right is P2 — here we only prove the gesture
> *dispatch* lands as a real touch.

## What to report back (P0 exit criteria)

- ✅/❌ Does **Dump tree** show IG nodes with usable `text` / `desc` / `id` /
  `bounds`? Paste a chunk of a reel-screen dump — that tells us how obfuscated
  the view-ids are and which fields we can actually select on.
- ✅/❌ Does **Double-tap** register as a like on the reel?
- ✅/❌ Does **Swipe up** move the reel / reveal reactions?

Once we have that, P1 turns these primitives into the `AccessibilityDevice`
that satisfies the `Device` ABC.
