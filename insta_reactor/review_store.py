"""Chat-centric review store — the persistent state behind the control panel.

The whole point of the tool is to *save you from scrolling spam*: instead of you
opening every chat, the bot goes over them and this store records, per chat:

  * how many reels it quietly reacted to for you (no action needed), and
  * the reels it could NOT safely handle — each with a thumbnail, a plain reason,
    and **where to find it** ("3rd reel from the bottom") so you can open your DM,
    scroll straight to it, and deal with it yourself.

Design:
  * Persists to ``data/review.json`` so opening the panel later shows the last
    state without re-running (you "see the chats the bot has gone over").
  * ``reacted`` is a *most-recent-run* snapshot (FYI — it's already handled).
  * ``flagged`` is your outstanding to-do: it MERGES across runs and only leaves
    the list when you mark it handled. This matters because the runner's
    de-dup skips re-flagging a reel it already flagged, so a naive
    replace-every-run would make un-handled reels vanish from the panel.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict

from .models import Action, RunSummary, CANON_EMOJI


DEFAULT_REVIEW_PATH = os.path.join("data", "review.json")


def _thumb_name(path) -> str:
    """Store only the basename — the web layer serves it from the thumbs dir."""
    return os.path.basename(path) if path else ""


@dataclass
class ReelReview:
    """One reel the bot handled or set aside, as shown in the panel."""

    key: str                       # stable dedup key within a chat
    reel_id: str = ""
    kind: str = ""                 # "reacted" | flag kind (no_consensus, ...)
    reason: str = ""               # plain-English why-flagged
    reply_text: str = ""           # what the bot sent (reacted only)
    emoji: str = ""                # winning emotion emoji, if any
    thumbnail: str = ""            # basename of the thumbnail screenshot
    position_from_bottom: int = 0  # 0 => unknown; else Nth reel from the bottom
    confidence: float = 0.0
    resolved: bool = False         # you marked it handled (flagged only)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReelReview":
        base = cls(key=d.get("key", ""))
        for k, v in (d or {}).items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base


@dataclass
class ChatReport:
    chat_name: str
    reacted: list[ReelReview] = field(default_factory=list)
    flagged: list[ReelReview] = field(default_factory=list)
    last_run_at: float = 0.0

    def pending(self) -> list[ReelReview]:
        return [r for r in self.flagged if not r.resolved]

    def to_dict(self) -> dict:
        return {
            "chat_name": self.chat_name,
            "reacted": [r.to_dict() for r in self.reacted],
            "flagged": [r.to_dict() for r in self.flagged],
            "last_run_at": self.last_run_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChatReport":
        return cls(
            chat_name=d.get("chat_name", ""),
            reacted=[ReelReview.from_dict(r) for r in d.get("reacted", [])],
            flagged=[ReelReview.from_dict(r) for r in d.get("flagged", [])],
            last_run_at=float(d.get("last_run_at", 0.0)),
        )


def _dedup_key(reel_id: str, reason: str) -> str:
    """A key that collapses identical repeats but keeps distinct items apart.

    Real reels have a stable shortcode id, so that alone is the key. Non-reel
    flags share placeholder ids ("text" for incoming text, "" for nav failures),
    so they fold the reason in — two different unanswered texts stay separate,
    while the same one flagged twice collapses to one entry.
    """
    rid = (reel_id or "").strip()
    if rid and rid not in ("text", "reel", "?"):
        return rid
    return f"{rid}:{reason}".strip(":")


class ReviewStore:
    def __init__(self, path: str = DEFAULT_REVIEW_PATH):
        self.path = path
        self.chats: dict[str, ChatReport] = {}
        self._load()

    # ---- persistence ----------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, rep in (data.get("chats") or {}).items():
                self.chats[name] = ChatReport.from_dict(rep)
        except (ValueError, OSError, TypeError):
            self.chats = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        payload = {"chats": {n: r.to_dict() for n, r in self.chats.items()}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # ---- recording a run ------------------------------------------------
    def record_run(self, summary: RunSummary,
                   chats_processed: list[str] | None = None) -> None:
        """Fold a finished run's summary into the persisted per-chat reports.

        ``chats_processed`` ensures chats the bot visited but found nothing in
        still show up as "gone over — all clear". Reacted reels replace the
        chat's most-recent-run snapshot; flagged reels are merged into the
        outstanding queue (preserving anything you've already handled).
        """
        now = time.time()
        touched: set[str] = set()

        # Ensure a report exists for every visited chat and stamp the run time.
        for name in (chats_processed or []):
            rep = self.chats.setdefault(name, ChatReport(chat_name=name))
            rep.last_run_at = now
            # A fresh run supersedes the previous run's "reacted" snapshot.
            rep.reacted = []
            touched.add(name)

        # Reacted reels: FYI snapshot of what the bot just handled for you.
        for d in summary.auto_replied:
            rep = self._report_for(d.chat_name, now)
            touched.add(d.chat_name)
            rep.reacted.append(ReelReview(
                key=_dedup_key(d.reel_id, d.reply_text or ""),
                reel_id=d.reel_id,
                kind="reacted",
                reply_text=d.reply_text or "",
                emoji=CANON_EMOJI.get(d.winning_emotion or "", ""),
                thumbnail=_thumb_name(d.thumbnail_path),
                position_from_bottom=int(d.position_from_bottom or 0),
                confidence=float(d.confidence or 0.0),
            ))

        # Flagged reels: merge into the outstanding to-do queue.
        for d in summary.flagged:
            rep = self._report_for(d.chat_name, now)
            touched.add(d.chat_name)
            kind = d.flag.kind if d.flag else "unknown"
            reason = d.flag.reason if d.flag else ""
            key = _dedup_key(d.reel_id, reason)
            self._upsert_flag(rep, ReelReview(
                key=key,
                reel_id=d.reel_id,
                kind=kind,
                reason=reason,
                thumbnail=_thumb_name(d.thumbnail_path),
                position_from_bottom=int(d.position_from_bottom or 0),
                confidence=float(d.confidence or 0.0),
            ))

        self.save()

    def _report_for(self, chat_name: str, now: float) -> ChatReport:
        rep = self.chats.setdefault(chat_name, ChatReport(chat_name=chat_name))
        if not rep.last_run_at:
            rep.last_run_at = now
        return rep

    @staticmethod
    def _upsert_flag(rep: ChatReport, item: ReelReview) -> None:
        for existing in rep.flagged:
            if existing.key != item.key:
                continue
            if existing.resolved:
                # You already handled this exact reel — don't resurrect it.
                return
            # Refresh the details (a newer scan may have a better thumbnail /
            # position) but keep its original place in the queue.
            existing.reel_id = item.reel_id or existing.reel_id
            existing.kind = item.kind or existing.kind
            existing.reason = item.reason or existing.reason
            existing.thumbnail = item.thumbnail or existing.thumbnail
            if item.position_from_bottom:
                existing.position_from_bottom = item.position_from_bottom
            existing.confidence = item.confidence
            return
        rep.flagged.append(item)

    # ---- queries + mutations used by the UI -----------------------------
    def ordered_chats(self) -> list[ChatReport]:
        """Chats gone over, most-pending first, then most recently run."""
        return sorted(
            self.chats.values(),
            key=lambda r: (len(r.pending()), r.last_run_at),
            reverse=True,
        )

    def total_pending(self) -> int:
        return sum(len(r.pending()) for r in self.chats.values())

    def resolve(self, chat_name: str, key: str) -> bool:
        rep = self.chats.get(chat_name)
        if not rep:
            return False
        for item in rep.flagged:
            if item.key == key and not item.resolved:
                item.resolved = True
                self.save()
                return True
        return False

    def clear(self) -> None:
        self.chats = {}
        self.save()
