"""Persistent record of reels we've already reacted to — the revisit fix.

Reels have no stable Instagram id, and reading one doesn't add an incoming
bubble, so the old ordinal-from-bottom key (`newreel#0`) pointed at a *different*
reel every run — which is why the bot kept re-reacting to the same reels.

We instead key a reel by a **content signature**: a hash of the chat name plus
its set of comment texts. That set is stable across runs (the comments don't
change), so once we've reacted we can recognise the same reel next time and skip
it. Stored as a small JSON file so it's easy to inspect or clear.
"""

from __future__ import annotations

import hashlib
import json
import os

from .engine.comment_filter import reactable


DEFAULT_SEEN_PATH = os.path.join("data", "handled_reels.json")


def signature(chat_name: str, comments, top_n: int = 25) -> str | None:
    """Stable id for a reel from its comment set, or None if not enough signal.

    Uses the `top_n` reactable comments (sorted for order-independence). Returns
    None when there aren't enough comments to identify the reel reliably — such
    reels are never auto-skipped (they'd be flagged anyway).
    """
    texts = sorted({c.text.strip() for c in reactable(comments)})
    if len(texts) < 3:
        return None
    joined = "".join(texts[:top_n])
    digest = hashlib.sha1(f"{chat_name}{joined}".encode("utf-8")).hexdigest()
    return digest[:16]


class SeenStore:
    def __init__(self, path: str = DEFAULT_SEEN_PATH):
        self.path = path
        self._sigs: set[str] = set()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._sigs = set(json.load(f))
            except (ValueError, OSError):
                self._sigs = set()

    def is_reacted(self, sig: str | None) -> bool:
        return sig is not None and sig in self._sigs

    def mark(self, sig: str | None) -> None:
        if sig is not None:
            self._sigs.add(sig)

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(sorted(self._sigs), f, indent=2)
