"""Flag Manager: the manual-review queue.

Keeps a persistent, append-only record of every reel that was *not* auto-
replied, with a plain-English reason. This is what you read after a run to see
what still needs your personal attention.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict

from .models import Decision


DEFAULT_QUEUE_PATH = os.path.join("data", "review_queue.json")


@dataclass
class ReviewItem:
    chat_name: str
    reel_id: str
    flag_kind: str
    reason: str
    confidence: float = 0.0
    created_at: float = field(default_factory=time.time)
    resolved: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class FlagManager:
    def __init__(self, path: str = DEFAULT_QUEUE_PATH):
        self.path = path
        self.items: list[ReviewItem] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.items = [ReviewItem(**it) for it in data]
            except (ValueError, OSError, TypeError):
                self.items = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([it.to_dict() for it in self.items], f,
                      ensure_ascii=False, indent=2)

    def enqueue(self, decision: Decision) -> ReviewItem:
        item = ReviewItem(
            chat_name=decision.chat_name,
            reel_id=decision.reel_id,
            flag_kind=decision.flag.kind if decision.flag else "unknown",
            reason=decision.flag.reason if decision.flag else "",
            confidence=decision.confidence,
        )
        self.items.append(item)
        return item

    def pending(self) -> list[ReviewItem]:
        return [it for it in self.items if not it.resolved]
