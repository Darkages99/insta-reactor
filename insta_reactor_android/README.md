# Insta Reactor Companion (phone UI)

A minimal Android app: enter a chat/user name, tap Run, see what
[insta_reactor](../insta_reactor) reacted to or flagged — all on the phone,
via Termux (no laptop needed once set up).

**Open this folder directly in Android Studio** ("Open" → select
`insta_reactor_android`). Android Studio will generate the Gradle wrapper on
first sync since it isn't checked in here.

Before running: do the one-time device setup in [SETUP.md](SETUP.md).

## How it works
- `MainActivity` — single screen: chat name field, dry-run checkbox, Run
  button, results.
- `TermuxRunner` — sends Termux's `RUN_COMMAND` intent to run
  `python -m insta_reactor chats add "<name>" && python -m insta_reactor run --json [--plan-only]`
  inside `~/insta_reactor` on-device.
- `RunResultReceiver` / `RunResultBus` — receives Termux's stdout/exit-code
  callback and hands it to whichever Activity is alive.
- `RunSummaryParser` — parses the JSON line printed by `report.to_dict()`
  (Python side, see `../insta_reactor/report.py`) into `RunSummary`.

## Not in this pass
- No automated Termux setup (Termux requires the `allow-external-apps`
  opt-in to be done by hand — see SETUP.md).
- No live-send default — "Dry run" starts checked and should stay checked
  until the engine's nav-hardening work is further along.
