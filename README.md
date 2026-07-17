# Instagram Reel Auto-Reactor

An **on-demand** assistant that reacts to Reels friends send you in DMs — but
only for chats you explicitly enable, only for the obvious high-confidence
cases, and **never by staying online monitoring**. You point it at your
approved chats, it reacts to the easy ones, queues everything ambiguous for
you, prints a summary, and exits. If you were about to reply yourself, nobody
gets left on read by a bot that jumped in.

The reaction "brain" is **fully deterministic** (no LLMs) and has **zero
third-party dependencies**, so it runs and is testable anywhere Python 3.10+
runs. Third-party libraries are only needed to drive a real phone.

---

## ⚠️ Read this first (honest risk)

Automating your personal Instagram account (sending DMs / reactions via a bot)
**violates Meta's Terms of Service** and can get your account **rate-limited,
checkpointed, or banned.** This tool is deliberately conservative — human-like,
low-volume, on-demand, high-confidence-only, human-in-the-loop — which reduces
but **does not eliminate** that risk. You are responsible for how you use it.
Consider a secondary/less-critical account and modest volumes.

There is also an ethical dimension: replies are designed to look like *you*.
That's the point of the profile — but it means friends may believe you
personally reacted. Only you can decide if that's fine for your relationships.

---

## Quick start — run it **now**, no phone, no installs

```bash
# from the project root (E:\INSTA REACTOR)
python -m insta_reactor run --simulate data/fixtures.example.json --plan-only
```

You'll see the full pipeline decide on a set of demo reels and print a summary.
`--plan-only` guarantees nothing is ever "sent" (it's a simulation anyway).

See the exact reasoning behind every decision:

```bash
python -m insta_reactor --config data/config.example.json explain \
    --simulate data/fixtures.example.json
```

Run the test suite (pure stdlib, no pytest required):

```bash
python -m unittest discover -s tests
```

---

## How a decision is made

For each received reel in an enabled chat, in this order:

| Check | Outcome |
|---|---|
| **Rule 1** — text message immediately before the reel | 🚩 flag `context_text` (an inside joke can't be inferred from comments) |
| Comments couldn't be read | 🚩 flag `unable_to_read` |
| **Rule 2** — fewer than `min_comments` (default 20) | 🚩 flag `too_few_comments` |
| No recognizable reactions / crowd too split | 🚩 flag `no_consensus` |
| Confidence below threshold | 🚩 flag `low_confidence` |
| Otherwise | ✅ auto-reply in *your* style |

### The reaction engine (deterministic)

1. **Normalize** each comment → canonical emotions (`DEAD 💀`, `CRYING 😭`,
   `LAUGH 😂`, `LOVE ❤️`, `FIRE 🔥`, `SHOCK 😱`, `ANGRY 😤`). Handles emoji
   variants, elongation (`deaaad → dead`, `lmaooo`), and multi-word slang
   (`goes hard`, `bury me`) without double-counting overlapping spans.
2. **Weighted vote** across comments. More-liked comments weigh more
   (`1 + log1p(likes)` by default); one comment is capped at 3 votes per
   emotion so spam can't dominate.
3. **Personal bias** — the spec's formula, tunable:

   ```
   final = public_score × 0.4  +  your_preference × 0.6
   ```

   Your ordered emoji preferences and typed replies (`bro 💀`, `nah 😭`) define
   `your_preference`, so ties break toward *your* historical style.
4. **Confidence** from how clearly the *crowd* agrees (top share, winning
   margin) and sample volume. Low confidence → defer to you, never guess.
5. **Reply** is generated in your configured style (single `💀`, double `💀💀`,
   or text+emoji `bro 💀`), reusing your actual common replies where possible.

Every decision carries a full score breakdown (`explain` shows it), satisfying
the "every decision is explainable" principle.

---

## Architecture (independently replaceable modules)

```
runner.py ──────────────► orchestrates: enabled chats → decide → send/flag → summary
   │
   ├── backends/            the ONE seam the runner talks to (Backend interface)
   │     ├── simulated.py   fixture-driven (dev/test/demo, no device)
   │     └── android.py     real device: composes the modules below
   │
   ├── automation/          NAVIGATION (deterministic state machine)
   │     ├── states.py      explicit UI states: INBOX→CHAT→REEL→COMMENTS→…
   │     ├── navigator.py   goals in, verified transitions out, retry + recover
   │     └── selectors.py   ★ THE ONE FILE YOU CALIBRATE per IG version
   │
   ├── collector/comments.py  COMMENT COLLECTOR (scroll, read, like-counts)
   │
   ├── engine/              REACTION ENGINE (pure, no deps, no LLM)
   │     ├── slang_map.py   emoji/slang → emotion data
   │     ├── normalize.py   comment → weighted emotion signals
   │     ├── scoring.py     weighted vote, personal bias, confidence, reply text
   │     └── reaction.py    the rules + final Decision
   │
   ├── device/              low-level "hands & eyes" (adb/uiautomator2 wrapper)
   ├── vision/templates.py  optional CV fallback: find an icon by appearance
   └── flags.py             FLAG MANAGER (persistent manual-review queue)
```

The runner depends only on the `Backend` interface, and navigation depends only
on the `Device` interface — so any layer can be swapped without touching the
others, exactly as the spec requires.

---

## Running against a real Android device

The recommended workflow: **develop on an emulator**, then **deploy to a
dedicated phone** kept logged into your account.

### 1. Install tooling

- **Android platform-tools** (`adb`): https://developer.android.com/tools/releases/platform-tools
  — add it to your `PATH`, then `adb devices` should list your device.
- **uiautomator2**:
  ```bash
  pip install -r requirements.txt      # or: pip install uiautomator2
  python -m uiautomator2 init          # installs the on-device agent (one-time)
  ```
- Enable **USB debugging** on the phone (Developer Options). For an emulator,
  Android Studio's AVD works out of the box.

### 2. Calibrate selectors (once per major IG version)

Instagram's UI has no stable API. This is the only fragile part, and it's
isolated to `insta_reactor/automation/selectors.py`. Dump a live screen:

```bash
# open Instagram to the screen you want to inspect, then:
python -m insta_reactor.tools.calibrate --out data/calib
```

That writes `screen.png`, `hierarchy.xml`, and a flattened `elements.txt`. Read
off the durable `content-desc` / `text` / `resource-id` of each control (the
comment button, send button, composer, etc.) and paste them into
`selectors.py`. Prefer accessibility labels over obfuscated resource-ids.

If some control has no reliable label, drop a small PNG crop of its icon under
`templates/` and use the `vision/` template-matcher as a fallback
(`pip install opencv-python numpy`).

### 3. Set up your profile and chats

```bash
python -m insta_reactor setup                     # interactive wizard
python -m insta_reactor chats add "Best Friend"   # exact inbox name
python -m insta_reactor chats add "Group Chat"
python -m insta_reactor chats list
```

### 4. Run

```bash
python -m insta_reactor run --plan-only   # decide + summarize, send NOTHING (dry run)
python -m insta_reactor run               # for real
python -m insta_reactor queue             # review what got flagged
```

Always start with `--plan-only` after any calibration change.

---

## Command reference

| Command | Purpose |
|---|---|
| `setup` | First-launch wizard: capture your reaction profile |
| `chats add\|remove\|list [name]` | Manage automation-enabled conversations |
| `run` | Process enabled chats on a real device, then stop |
| `run --simulate FIXTURE.json` | Same, but from a JSON fixture (no phone) |
| `run --plan-only` | Decide + summarize but never actually send |
| `run --verbose` | Include full per-reel reasoning in the summary |
| `queue` | Show the pending manual-review queue |
| `explain --simulate FIXTURE.json` | Print full reasoning for every reel in a fixture |

Config lives in `data/config.json` (see `data/config.example.json`). Every
threshold is tunable there under `settings`.

---

## Known limitations

- **IG UI changes** will eventually break navigation. By design that's a
  `selectors.py` edit, not a rewrite. Auto-reply fails *safe*: unreadable →
  flagged, never a wrong guess.
- **Idempotency is per-run.** Reel bubbles have no stable id, so re-running over
  the same chat *could* re-react. Run once per batch — the intended on-demand
  flow. (The simulated backend is fully idempotent for testing.)
- **Preceding-text (Rule 1) detection** on-device is a bounds heuristic (an
  incoming text bubble directly above the reel). Calibrate and verify with
  `--plan-only` before trusting it.
- **Like-count weighting** is best-effort; if IG doesn't expose counts, comments
  weigh equally.
- This does not, and should not, defeat Instagram's automation defenses.

---

## Design principles (from the spec)

- Reliability over speed; deterministic over AI; automate only high-confidence,
  low-risk cases; defer to the human when unsure; every decision explainable.
- On-demand, not always-on monitoring — so you're never "seenzoned" by your own
  bot when you meant to reply yourself.
