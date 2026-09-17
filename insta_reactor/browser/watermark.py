"""Per-chat "already reacted" record for the browser backend.

Why this exists (and why it's keyed by MEDIA ID, not a DOM index):

Instagram's DM message list is *virtualized* — only the two or three reel
bubbles nearest the viewport are ever mounted in the DOM at once, and their
`[aria-label="Clip"]` node positions shift as you scroll and bubbles
mount/unmount. An earlier version keyed the watermark on that positional index
plus the count of *currently-mounted* clips; both are unstable, so it would
routinely (a) miss most of a burst — only the two rendered reels were seen —
and (b) re-admit a reel it had already reacted to, giving it a second reaction.

The one stable, cheap-to-read identity a reel bubble exposes in the thread DOM
is its cover image's media id (the numeric pair in the fbcdn URL, e.g.
`808285020_1096052183318366`). Distinct reels get distinct media ids, and the
same reel keeps its id across scrolls and runs. So we remember, per chat, the
SET of media ids we've actually reacted/replied to. On the next run a reel is
"new" iff its media id isn't in that set — which naturally:
  * re-reads reels every run (cheap, no send) but reacts to each only once,
  * still reacts to a genuine RE-SEND — that's a brand-new bubble with a new
    media id (IG re-uploads the share), so it isn't in the set.
"""

from __future__ import annotations

import json
import os

DEFAULT_WATERMARK_PATH = os.path.join("data", "reel_watermark.json")


class ReelWatermarkStore:
    def __init__(self, path: str = DEFAULT_WATERMARK_PATH):
        self.path = path
        # {chat_name: set(media_id, ...)}
        self._data: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except (ValueError, OSError, TypeError):
            raw = {}
        for chat, entry in (raw.items() if isinstance(raw, dict) else []):
            # New format: {"handled": [mid, ...]}. Anything else (e.g. the old
            # {"index": n, "total": m} format) carries no usable media ids, so
            # it starts empty — the reel re-read on the next run repopulates it.
            if isinstance(entry, dict) and isinstance(entry.get("handled"), list):
                self._data[chat] = set(str(m) for m in entry["handled"] if m)

    def is_handled(self, chat_name: str, media_id: str | None) -> bool:
        """True if we've already reacted/replied to this reel in this chat.
        A missing/None media id is never considered handled (we can't dedup it,
        so the caller falls back to its own per-run guard)."""
        if not media_id:
            return False
        return media_id in self._data.get(chat_name, set())

    def mark(self, chat_name: str, media_id: str | None) -> None:
        """Record that we've reacted/replied to this reel. Write-through
        (no separate save() needed). No-ops on a missing media id."""
        if not media_id:
            return
        bucket = self._data.setdefault(chat_name, set())
        if media_id in bucket:
            return
        bucket.add(media_id)
        self._save()

    def handled_set(self, chat_name: str) -> set[str]:
        return set(self._data.get(chat_name, set()))

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            serializable = {chat: {"handled": sorted(mids)}
                            for chat, mids in self._data.items()}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
