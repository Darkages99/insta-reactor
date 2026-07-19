# INSTA REACTOR — V3 app shell (P3)

The distributable, **PC-free** app. It embeds the reused Python engine via
**Chaquopy** and drives Instagram through the on-device **AccessibilityService**
(the V3 device layer proven in P0/P1). This module replaces V2's `webui.py`:
same behaviour, now on the phone with no ADB, no Termux, no browser.

Package: `com.instareactor.v3`. Toolchain: AGP 8.5.2 / Kotlin 1.9.24 /
Gradle 8.14.5 / Chaquopy 16 / Compose (BOM 2024.09).

## What's here

| Piece | File | Role |
|---|---|---|
| Python runtime start | `App.kt` | `Python.start(AndroidPlatform)` once per process |
| Kotlin→Python seam | `EngineBridge.kt` → `python/v3_entry.py` → `insta_reactor/app_entry.py` | JSON in, JSON out; never throws across JNI |
| Device layer | `ReactorAccessibilityService.kt` | the P1 bridge (node reads + gestures), carried over from P0 |
| Run lifecycle | `RunService.kt` (foreground) + `RunState.kt` | runs the engine off the main thread, publishes progress |
| UI | `MainActivity.kt` + `ui/AppUi.kt` + `MainViewModel.kt` | Compose: setup / run / results / review |
| Storage | `Store.kt` → app files dir | `config.json`, `review_queue.json`, `handled_reels.json`, `engine.log` — nothing leaves the phone |

## How the engine gets in

`app/build.gradle.kts` → `python { pip { install(<repo root>) } }` installs the
`insta_reactor` package straight from the repo's `pyproject.toml`
(`dependencies = []`, so the pure-stdlib core needs no third-party downloads).
Kotlin then calls `v3_entry` (which re-exports `insta_reactor.app_entry`).

The entry seam is covered by `tests/test_p3_app_entry.py` (6 tests) and runs the
*real* `AccessibilityDevice` path against captured trees via the P2 replay
harness — so the Kotlin↔Python contract is verified with no phone.

## Build

```
cd insta_reactor_v3
./gradlew :app:assembleDebug     # JDK 17+ (Android Studio JBR works)
```

Note: the first build downloads the Chaquopy runtime and the Compose/AndroidX
dependencies, and pip-builds the engine wheel (PEP 517), so it needs network
once. Output: `app/build/outputs/apk/debug/app-debug.apk`.

## Use (on a fresh phone, no PC)

1. Install the APK (allow "unknown sources" once).
2. **Setup** — favourite emojis, common replies, reply style, and the exact DM
   titles to process. Save.
3. **Permission** — enable "INSTA REACTOR" under Settings ▸ Accessibility.
4. **Dry run** first — decisions are made and queued but nothing is sent; check
   the **review queue**. Then **Run & send** when it looks right.

`send=false` (dry run) is the default until on-device parity is signed off
(`docs/V3_PLAN.md` §8.4).

## Status / what's verified

- Python seam (`app_entry`): **verified** off-device (106 tests green).
- Kotlin/Compose/Chaquopy shell: complete; the Gradle build has **not** been
  run to a signed APK in this environment (no device, first-build downloads).
  Remaining on-device work (gesture fidelity, settle timings, a fresh DM-thread
  capture) is the P2 sign-off that P3 unblocks — see the plan.
