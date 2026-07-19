# INSTA REACTOR — V3 Plan (phone-only, PC-free, fully local)

Status: **planning — key decisions locked** · Branch: `v3` (off `master` @ V2) · Owner: Sarang

**Locked (2026-07-19):** engine = **Option B** (reuse the Python engine via
Chaquopy); distribution = **direct APK / sideload**. Full Kotlin rewrite is
deferred to **V4**; Play Store is deferred to **V5**. See §8 and §10.

V1 = throwaway. V2 = works but requires a PC (this document explains exactly
why). V3 = the same behaviour as V2, running **entirely on the phone**, with
**no PC anywhere in the loop**, **no user data leaving the device**, and
packaged so a normal person can **install an app and go**.

---

## 1. Non-negotiables (what "done" means)

1. **No PC in the loop** — not during setup, not at runtime. No ADB host, no
   laptop tether, no wireless-debugging pairing.
2. **All local** — comment text and profile never leave the phone. No backend
   server, no telemetry. (Optional push alerts are user-configured and carry no
   comment content.)
3. **Distributable** — a person installs an app, grants one permission, and it
   works. No Termux, no F-Droid side-quests, no developer mode.
4. **Feature parity with V2** — same decision behaviour: Rule 1/1b manual-review
   flags, min-comments gating, popular-comment verbatim echo (with the V2
   safety tightening), favourite-emoji reply selection, swipe-to-react flow,
   cross-run dedup, review queue, run summary.
5. **Privacy by construction** — because 1–2 hold, there is no collected data to
   govern, so GDPR/CCPA surface is essentially nil. This is a *design output*,
   not a policy we bolt on.

---

## 2. Why V2 needs a PC (the exact dependency)

The entire PC dependency is **one import**: `backends/android.py` builds a
`U2Device`, and `device/u2_device.py` drives the phone via **uiautomator2**,
which needs an **ADB host** to talk to the device. That ADB host is the PC.

Everything else in V2 is device-agnostic Python. The codebase already isolates
this behind the abstract `Device` interface (`device/base.py`) — its own
docstring says it's meant to keep navigation "portable across uiautomator2, raw
adb, or a future backend." V3 is that future backend.

```
runner → backend(android) → navigator ─┐
                            collector ──┼─► Device (abstract)  ◄── the seam
                            selectors ──┘        │
                                         U2Device (uiautomator2 → ADB → PC)   ← REPLACE
```

## 3. Architectural insight: replace the hands, keep the brain

V3 is **not a rewrite**. It is:

- **New `AccessibilityDevice`** implementing the existing `Device` ABC
  (`find`, `find_all`, `tap`, `long_press`, `swipe`, `input_text`, `press_back`,
  `current_package`, `window_size`, `dump_hierarchy`) using Android's
  **AccessibilityService** APIs instead of ADB.
- **New delivery shell** — a real Android app that hosts the engine and the
  accessibility service, replacing the Termux companion.
- **Everything above the `Device` seam reused** (see reuse table in the repo
  discussion / section 5).

Why AccessibilityService is the right primitive: it is Android's *first-class,
no-PC* API for exactly what uiautomator2 does over ADB — read the on-screen node
tree (`rootInActiveWindow` → `AccessibilityNodeInfo`) and dispatch input
(`performAction(ACTION_CLICK)`, `dispatchGesture` for taps/swipes,
`ACTION_SET_TEXT` for the composer). The field mapping is near 1:1:

| `UiNode` field | uiautomator2 source | AccessibilityNodeInfo source |
|---|---|---|
| `text` | `text` | `getText()` |
| `desc` | `contentDescription` | `getContentDescription()` |
| `resource_id` | `resourceName` | `getViewIdResourceName()` |
| `clazz` | `className` | `getClassName()` |
| `bounds` | `bounds` | `getBoundsInScreen()` |
| `clickable` | `clickable` | `isClickable()` |

So `navigator.py`, `selectors.py`, and `collector/comments.py` should port with
**no logic changes** — only re-verification of selector strings/geometry against
the live node tree (Instagram's resource-ids are obfuscated and shift across
app versions; this is already true in V2 and is why calibration exists).

---

## 4. Options considered

### Option A — Termux wrapper (the current `insta_reactor_android/` scaffold)
Android app fires a `RUN_COMMAND` intent → Termux runs the unchanged Python →
uiautomator2 connects to the phone via on-device wireless-debugging loopback
(`adb connect 127.0.0.1:<port>`).

- ✅ Reuses 100% of V2 Python immediately; already scaffolded; fastest to a
  personal demo.
- ❌ **Fails non-negotiable #3 hard.** Requires Termux + Termux:API from F-Droid,
  manual `allow-external-apps=true`, Developer Options, Wireless Debugging, and a
  fresh `adb connect` after every reboot (port rotates). The ATX agent must stay
  alive. No normal user will do this; not Play-Store-able.
- **Verdict:** keep only as a throwaway bench rig for testing the engine on real
  data while the real device layer is built. Not the product.

### Option B — Native app + AccessibilityService, engine stays Python (Chaquopy) ★ recommended
A real Android app embeds the Python engine via **Chaquopy** (Python-in-Android)
and hosts an **AccessibilityService**. `AccessibilityDevice` (Python) is a thin
wrapper over the Kotlin service via Chaquopy's Java interop, satisfying the
`Device` ABC. No ADB, no Termux, no dev mode.

- ✅ Meets all non-negotiables. Reuses the entire tested decision engine (the
  hard, subtle 4k lines + 61 tests) unchanged. One permission grant to run.
  Fully local; zero data egress.
- ⚠️ Chaquopy adds APK size and a build-toolchain learning curve. The optional
  transformers/torch model does **not** run under Chaquopy — the on-device model
  needs a mobile runtime (see §6). Rules-only parity is unaffected (model is an
  optional ensemble; rules stay authoritative).
- ⚠️ Accessibility automation carries Play Store policy risk (see §6).
- **Verdict:** the pragmatic bridge. Ships the proven brain on a distributable,
  PC-free shell fastest.

### Option C — Full native Kotlin rewrite (engine + device)
Port scoring / classifier / reply-select / reaction rules to Kotlin; model via
TFLite or ONNX Runtime Mobile; AccessibilityService for the device.

- ✅ Smallest, fastest APK; no Python runtime; cleanest Play Store story;
  best long-term product.
- ❌ Most work and highest risk: re-implement and re-test every nuanced decision
  rule (and the 61 tests) in Kotlin; lose fast Python iteration on the brain.
- **Verdict:** the eventual endgame *if* distribution economics demand a
  Python-free binary. Do it after B proves the behaviour on-device — port a
  frozen, well-tested spec, not a moving target.

### Decision matrix

| | No PC | All local | Distributable | V2 parity effort | Total effort |
|---|---|---|---|---|---|
| A Termux | ✅ (fragile) | ✅ | ❌ | none | Low |
| **B Chaquopy + a11y** | ✅ | ✅ | ✅ | low | **Medium** |
| C Kotlin rewrite | ✅ | ✅ | ✅✅ | high (re-impl) | High |

**Recommendation: build B now; keep C as a documented later migration.**
This is a fork worth an explicit sign-off before construction (see §8).

---

## 5. Target architecture (Option B)

```
┌───────────────────────── Android app (single APK) ─────────────────────────┐
│                                                                             │
│  UI (Kotlin/Compose)          AccessibilityService (Kotlin)                 │
│  • setup / profile            • rootInActiveWindow → node queries           │
│  • pick chats                 • dispatchGesture: tap / long-press / swipe    │
│  • Run button                 • ACTION_SET_TEXT for the composer            │
│  • results + review queue         ▲                                         │
│        │                          │ Java interop (Chaquopy)                 │
│        ▼                          │                                         │
│  ┌───────────── Python engine (Chaquopy, embedded) ──────────────────────┐  │
│  │ runner → backend(android) → navigator / collector / selectors         │  │
│  │                                    │                                   │  │
│  │                          AccessibilityDevice(Device)  ← NEW            │  │
│  │ engine/* · models · config · flags · seen_store · report  (REUSED)    │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  Local storage: config.json, review_queue.json, handled_reels.json          │
│  Optional: ntfy push for "needs review" (no comment text) — user opt-in     │
└─────────────────────────────────────────────────────────────────────────────┘
   No ADB · No Termux · No server · No PC
```

---

## 6. Hard parts & risks (address early, not late)

1. **Selector drift / obfuscated resource-ids.** IG ships obfuscated,
   version-varying view ids. V2 already leans on text/desc/geometry heuristics
   and a calibration step. Port that philosophy; expect `selectors.py` to need a
   fresh pass against the a11y node tree. **De-risk in the P0 spike.**
2. **Gesture fidelity & timing.** `dispatchGesture` is async and coordinate-based
   (like uiautomator2 swipes). The swipe-to-react flow and settle timings
   (`Navigator.settle`) must be retuned; a11y events can lag the animation.
3. **On-device model runtime.** transformers/torch won't run under Chaquopy.
   Options: (a) ship **rules-only** for V3.0 (full parity minus the optional
   model — acceptable, rules are authoritative), then (b) add a small classifier
   via **TFLite/ONNX Runtime Mobile** or **MediaPipe** as a `Model` implementation
   behind the existing `build_model` seam. Ties into the offline-brain direction.
4. **Play Store policy.** Accessibility-driven engagement automation is
   scrutinised and can be rejected/pulled. Decide distribution channel early
   (§8): direct APK / sideload sidesteps this; Play Store needs a defensible
   accessibility justification. This choice shapes UX copy and permissions.
5. **Instagram ToS / anti-automation.** Automating IG interaction risks the
   *user's* account regardless of platform. Keep V2's conservative posture:
   on-demand (not always-on), human-review flagging, dedup, and rate/scope
   limits. Make "dry run" the default until device-verified.
6. **Foreground service & battery.** A run must survive Doze and screen-off
   transitions; use a foreground service with a clear notification. No always-on
   monitoring (matches V2's on-demand design — avoids leaving people "on read").
7. **AccessibilityService can read a lot.** With great scope comes scrutiny; keep
   the service scoped to Instagram (`packageNames`) and do nothing off-app.

---

## 7. Phased roadmap

- **P0 — Spike / de-risk (proves the whole thesis).** Minimal Android app +
  AccessibilityService that dumps IG's live node tree and performs one scripted
  tap + one swipe on a real reel. Goal: confirm the `UiNode` field mapping and
  that swipe-to-react is reproducible with `dispatchGesture`. *Exit:* one reel
  reacted to, zero PC involved.
- **P1 — `AccessibilityDevice`.** Implement the full `Device` ABC over the
  service. Unit-test field mapping; wire it so `backends/android.py` can be
  constructed with it instead of `U2Device` (parameterise the device, don't
  hardcode the import).
- **P2 — Parity bring-up.** Run the reused engine end-to-end on-device against
  real chats. Re-verify/retune `selectors.py`, geometry zones, and settle
  timings. Port the calibration tool to an in-app flow. *Exit:* V2's behaviour
  reproduced on-device, dry-run.
- **P3 — App shell & packaging.** Compose UI for setup/profile, chat selection,
  Run, results, and the review queue (replaces `webui.py`). Foreground-service
  run lifecycle. Local storage. Single installable APK.
- **P4 — On-device model (optional brain upgrade).** Add a TFLite/ONNX `Model`
  behind `build_model`; keep rules authoritative. Ships after parity.
- **P5 — Distribution (sideload).** Signing, versioning, and an update channel
  (e.g. GitHub releases). Onboarding that honestly explains the "unknown
  sources" install and the one accessibility grant. **No Play Store in V3** —
  that's V5 (§10).

Sequencing rule: **P0 before anything else.** If swipe-to-react or node reads
don't behave under AccessibilityService, that changes the plan — find out first.

---

## 8. Decisions

**Locked:**
1. **Engine strategy — Option B (Chaquopy, reuse the Python engine).** Ship the
   proven brain unchanged inside a native shell now. A full Kotlin rewrite is
   *not* abandoned — it is deliberately deferred to **V4** (§10), to be done
   against a frozen, device-verified spec rather than a moving target.
2. **Distribution — direct APK / sideload.** Host the APK (e.g. GitHub
   releases); users enable "install from unknown sources" once and grant the
   accessibility permission. Sidesteps Play Store's accessibility-automation
   scrutiny while the app is proven with power users. Play Store is deferred to
   **V5** (§10).

**Still open (deferred, not blocking):**
3. **Model runtime for P4:** TFLite vs ONNX Runtime Mobile vs MediaPipe — decide
   at parity; the choice affects the classifier port. (torch won't run under
   Chaquopy, so V3.0 ships rules-only.)
4. **Always dry-run first?** Keep "dry run" the default on-device until P2
   sign-off (matches the scaffold's current stance).

---

## 9. Definition of done (V3.0)

- Fresh phone, no PC ever connected: install APK → grant accessibility → run
  setup → pick a chat → tap Run → reels reacted/flagged exactly as V2 would.
- No network egress containing comment text (verifiable by inspection).
- Review queue, dedup, and run summary all function on-device.
- Rules-only parity with V2 (model optional, arrives in P4).

---

## 10. Future versions (deferred on purpose)

These are committed *directions*, not V3 scope. They exist so V3's choices stay
honest bridges rather than dead ends.

### V4 — Full Kotlin rewrite (Python-free binary)
**Trigger:** V3's decision logic is device-verified and behaving perfectly, and
the Chaquopy runtime's size/startup cost is worth removing.
**What:** port the frozen, well-tested engine (scoring, classifier interface,
reply-select, reaction rules — and the 61 tests as a Kotlin parity spec) from
Python to Kotlin. `AccessibilityDevice` and the app shell from V3 carry over
mostly unchanged; only the brain is re-implemented.
**Why wait:** you translate a *frozen spec*, not a moving target — the risky
re-implementation happens once, against behaviour you already trust. Smaller,
faster APK; no embedded Python runtime.

### V5 — Play Store distribution
**Trigger:** V4 is a fully working native Kotlin app, stable with real users
from the V3 sideload phase.
**What:** submit to Google Play; execute the accessibility-automation policy
justification; enable one-tap install + automatic updates for mass reach.
**Why wait:** a native app with no Python runtime is the cleanest possible Play
Store submission, and by then the V3→V4 sideload phase has proven the behaviour
and surfaced whatever policy friction actually exists — so the store fight is
fought from a position of a stable, real-user-validated product.

**Version ladder at a glance:** V3 = Python-in-native-shell, sideload → V4 =
full Kotlin, sideload → V5 = full Kotlin, Play Store.
```
