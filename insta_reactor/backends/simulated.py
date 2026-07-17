"""A fixture-driven backend for development, testing, and demos.

Reads a JSON file describing chats -> reels -> {preceding_text, comments} and
plays the exact same runner/engine path a real phone would, minus Instagram.
This is how you watch the end-to-end flow and the summary output without any
device, ADB, or ToS risk.

Fixture format (see data/fixtures.example.json):

    {
      "chats": {
        "Best Friend": {
          "reels": [
            {
              "id": "r1",
              "preceding_text": null,
              "comment_count": 240,
              "comments": [{"text": "LMAOO 💀💀", "likes": 120}, ...]
            }
          ]
        }
      }
    }
"""

from __future__ import annotations

import json

from .base import Backend, ReelHandle
from ..models import ReelContext, Comment


class SimulatedBackend(Backend):
    def __init__(self, fixtures: dict):
        self._chats = fixtures.get("chats", {})
        self._current_chat: str | None = None
        # remember what we "replied" to so re-runs are idempotent
        self._reacted: set[tuple[str, str]] = set()
        self.sent: list[tuple[str, str, str]] = []  # (chat, reel_id, text)

    # ---- construction helpers -------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> "SimulatedBackend":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    # ---- Backend interface ----------------------------------------------
    def prepare(self) -> None:
        self._current_chat = None

    def open_chat(self, chat_name: str) -> bool:
        if chat_name in self._chats:
            self._current_chat = chat_name
            return True
        return False

    def find_unreacted_reels(self) -> list[ReelHandle]:
        chat = self._chats.get(self._current_chat, {})
        out: list[ReelHandle] = []
        for reel in chat.get("reels", []):
            rid = str(reel.get("id"))
            if (self._current_chat, rid) in self._reacted:
                continue
            out.append(ReelHandle(reel_id=rid, locator=reel))
        return out

    def build_reel_context(self, reel: ReelHandle) -> ReelContext:
        data = reel.locator or {}
        raw_comments = data.get("comments")
        comments = None
        if raw_comments is not None:
            comments = [
                Comment(text=c.get("text", ""), likes=int(c.get("likes", 0)))
                for c in raw_comments
            ]
        preceding = data.get("preceding_text")
        return ReelContext(
            chat_name=self._current_chat or "",
            reel_id=reel.reel_id,
            has_preceding_text=bool(preceding),
            preceding_text=preceding,
            comments=comments,
            comment_count=data.get("comment_count"),
            read_error=bool(data.get("read_error", False)),
        )

    def send_reply(self, reel: ReelHandle, text: str) -> bool:
        self._reacted.add((self._current_chat or "", reel.reel_id))
        self.sent.append((self._current_chat or "", reel.reel_id, text))
        return True

    def return_to_inbox(self) -> None:
        self._current_chat = None
