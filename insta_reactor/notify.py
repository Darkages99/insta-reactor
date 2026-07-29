"""Push notifications via ntfy.sh — shared by the CLI `run`, the web UI, and
the on-device (Termux) runner.

Why a shared module: the whole point of these alerts is that you can be told
what happened *without watching the PC*. Putting the sender here (instead of
only in webui) means a plain `python -m insta_reactor run` — which is exactly
what the on-device Termux runner invokes — also pushes to your phone. Set
`ntfy_topic` in config.json, install the free ntfy app, and subscribe to that
topic to receive these as real push notifications. Empty topic => skipped
(logged instead), so nothing here can break a run.

Two things the user explicitly asked to be alerted about:
  1. ANY incoming text message in a chat (even one) — the bot only reacts to
     reels, so text needs a human. FlagKind.INCOMING_TEXT.
  2. Anything the bot could NOT respond to (nav/send/read failure).
"""

from __future__ import annotations

import logging
import urllib.request

from .models import FlagKind, RunSummary
from .report import FLAG_LABEL

log = logging.getLogger("insta_reactor.notify")

# The bot tried to act but physically couldn't (open/read/send failed).
ERROR_KINDS = {FlagKind.NAV_FAILED, FlagKind.UNABLE_TO_READ}
# A human judgement call is needed, but nothing errored.
NEEDS_YOU_KINDS = {FlagKind.NO_CONSENSUS, FlagKind.LOW_CONFIDENCE,
                   FlagKind.TOO_FEW_COMMENTS, FlagKind.CONTEXT_TEXT}


def push(topic: str, title: str, message: str, priority: str = "high") -> bool:
    """POST a single notification to ntfy.sh. Returns True if sent.

    Never raises — a notification failure must not abort a run.
    """
    if not topic:
        log.info("ntfy_topic not set; skipping notification: %s — %s",
                 title, message)
        return False
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            # Title must be latin-1-safe for an HTTP header; ntfy renders the
            # (utf-8) body fine, so keep emoji out of the title.
            headers={"Title": title.encode("ascii", "ignore").decode() or
                     "Insta Reactor",
                     "Priority": priority,
                     "Tags": "robot"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception:
        log.exception("failed to send ntfy notification")
        return False


def _snippets(decisions, limit: int = 4) -> str:
    """Join a few flag reasons into one readable body."""
    parts = [d.flag.reason for d in decisions[:limit] if d.flag and d.flag.reason]
    body = "\n".join(f"• {p}" for p in parts)
    extra = len(decisions) - limit
    if extra > 0:
        body += f"\n…and {extra} more"
    return body


def notify_from_summary(topic: str, summary: RunSummary) -> int:
    """Translate a finished run into targeted push notifications.

    Returns the number of notifications sent. Sends, in priority order:
      * INCOMING_TEXT  -> one high-priority push per chat ("new text messages")
      * ERROR_KINDS    -> one high-priority push ("couldn't respond")
      * NEEDS_YOU_KINDS-> one default-priority push ("need review")
    A clean run with only auto-replies sends nothing (no noise).
    """
    sent = 0
    flagged = summary.flagged

    texts = [d for d in flagged
             if d.flag and d.flag.kind == FlagKind.INCOMING_TEXT]
    if texts:
        # group by chat so you get one alert per conversation
        by_chat: dict[str, list] = {}
        for d in texts:
            by_chat.setdefault(d.chat_name or "(chat)", []).append(d)
        for chat, items in by_chat.items():
            n = len(items)
            title = (f"New text in {chat}" if n == 1
                     else f"{n} new texts in {chat}")
            if push(topic, title, _snippets(items), priority="high"):
                sent += 1

    errors = [d for d in flagged if d.flag and d.flag.kind in ERROR_KINDS]
    if errors:
        title = f"Couldn't respond to {len(errors)} reel(s)"
        if push(topic, title, _snippets(errors), priority="high"):
            sent += 1

    needs = [d for d in flagged if d.flag and d.flag.kind in NEEDS_YOU_KINDS]
    if needs:
        counts: dict[str, int] = {}
        for d in needs:
            counts[d.flag.kind] = counts.get(d.flag.kind, 0) + 1
        body = "\n".join(f"• {n}× {FLAG_LABEL.get(k, k)}"
                         for k, n in counts.items())
        title = f"{len(needs)} reel(s) need review"
        if push(topic, title, body, priority="default"):
            sent += 1

    return sent
