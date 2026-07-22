"""On-device entry point — the single seam the V3 Android app calls.

The V3 app has no PC, no CLI, and no `webui.py`. Instead, Kotlin (via Chaquopy)
hands this module two things and calls one function:

  * `bridge`   — the live `ReactorAccessibilityService` instance. It duck-types
                 the `AccessibilityBridge` protocol, so `build_device(config,
                 bridge=bridge)` turns it into an `AccessibilityDevice` and the
                 whole reused engine drives Instagram through it (no ADB, no PC).
  * `data_dir` — the app's private files dir. All state (config, review queue,
                 dedup store) lives here, so nothing leaves the phone.

Contract with the Kotlin side (so the JNI boundary is boring and safe):
  * every function returns a **JSON string**, never a Python object;
  * every function **catches everything** — a failure comes back as
    `{"ok": false, "error": ...}`, so Chaquopy never sees a Python exception
    propagate across the boundary.

This is the file `EngineBridge.kt` calls. It is pure Python and fully testable
without an Android device: the parity suite drives it through a `ReplayBridge`
(see tests/test_p3_app_entry.py).
"""

from __future__ import annotations

import json
import logging
import os
import traceback

from .config import AppConfig, load_config as _load_config, save_config as _save_config
from .backends.android import AndroidBackend
from .device.factory import build_device
from .flags import FlagManager
from .seen_store import SeenStore
from .runner import Runner
from .models import Decision, RunSummary

log = logging.getLogger("insta_reactor.app_entry")

# No PC / no CLI means no one ever calls logging.basicConfig() on this process
# — without a handler, the package's log.info(...) calls (reel sweep progress,
# nav state, etc.) are silently dropped before Chaquopy's stdout redirect ever
# sees them. Configure once at import time so `adb logcat` shows them.
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")


# --- storage layout ----------------------------------------------------------
# One directory (the app's files dir on Android) holds everything. Mirrors the
# V2 `data/` layout so the reused modules keep their filenames.

def _paths(data_dir: str) -> tuple[str, str, str]:
    return (
        os.path.join(data_dir, "config.json"),
        os.path.join(data_dir, "review_queue.json"),
        os.path.join(data_dir, "handled_reels.json"),
    )


def _err(exc: BaseException) -> str:
    """A never-raises failure envelope the Kotlin side can always parse."""
    return json.dumps(
        {"ok": False, "error": f"{type(exc).__name__}: {exc}",
         "trace": traceback.format_exc()},
        ensure_ascii=False,
    )


# --- config read/write (the setup UI calls these) ----------------------------

def load_config(data_dir: str) -> str:
    """Return the saved config (or defaults if none yet) as JSON."""
    try:
        cfg_path, _, _ = _paths(data_dir)
        cfg = _load_config(cfg_path)
        return json.dumps({"ok": True, "config": cfg.to_dict()}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — boundary must not raise
        log.exception("load_config failed")
        return _err(exc)


def save_config(data_dir: str, config_json: str) -> str:
    """Persist a config supplied by the UI (a JSON object matching AppConfig)."""
    try:
        cfg_path, _, _ = _paths(data_dir)
        cfg = AppConfig.from_dict(json.loads(config_json))
        _save_config(cfg, cfg_path)
        return json.dumps({"ok": True}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.exception("save_config failed")
        return _err(exc)


def review_queue(data_dir: str) -> str:
    """Return the pending (unresolved) manual-review items as JSON."""
    try:
        _, q_path, _ = _paths(data_dir)
        fm = FlagManager(q_path)
        return json.dumps(
            {"ok": True, "items": [it.to_dict() for it in fm.pending()]},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("review_queue failed")
        return _err(exc)


# --- the run (the Run button calls this on a background thread) --------------

def run(bridge, data_dir: str, send: bool = False) -> str:
    """Process the enabled chats once and return a JSON run summary.

    `send=False` (the default) is a **dry run**: every decision is made and the
    review queue is written, but no reply is ever dispatched. This matches the
    V3 plan's "dry run is the default until device-verified" posture
    (docs/V3_PLAN.md §8.4). The UI flips `send=True` only after the user opts in.

    MUST be called off the main thread — the bridge's gesture calls block until
    the gesture completes (see ReactorAccessibilityService threading note).
    """
    try:
        cfg_path, q_path, seen_path = _paths(data_dir)
        config = _load_config(cfg_path)
        device = build_device(config, bridge=bridge)
        backend = AndroidBackend(config, device=device)
        flags = FlagManager(q_path)
        seen = SeenStore(seen_path)
        summary = Runner(
            backend, config, flag_manager=flags, send=send, seen_store=seen,
        ).run()
        return json.dumps(
            {"ok": True, "send": send, **_summary_dict(summary)},
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001 — boundary must not raise
        log.exception("run failed")
        return _err(exc)


def _summary_dict(summary: RunSummary) -> dict:
    return {
        "auto_replied": [_decision_dict(d) for d in summary.auto_replied],
        "flagged": [_decision_dict(d) for d in summary.flagged],
        "counts": {
            "auto_replied": len(summary.auto_replied),
            "flagged": len(summary.flagged),
            "by_flag": summary.counts_by_flag(),
        },
    }


def _decision_dict(d: Decision) -> dict:
    """A flat, UI-friendly view of a Decision (drops nested breakdown)."""
    return {
        "chat_name": d.chat_name,
        "reel_id": d.reel_id,
        "action": d.action,
        "reply_text": d.reply_text,
        "winning_emotion": d.winning_emotion,
        "confidence": round(d.confidence, 3),
        "reply_source": d.reply_source,
        "flag_kind": d.flag.kind if d.flag else None,
        "reason": d.flag.reason if d.flag else None,
    }
