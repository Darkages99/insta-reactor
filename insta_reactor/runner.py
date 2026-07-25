"""Process the enabled chats once, then stop.

This is the on-demand orchestrator: you point it at your approved chats, it
reacts to the obvious/high-confidence reels, queues everything else for you,
prints a summary, and exits. It does NOT stay online monitoring — so if you
were about to reply yourself, nobody gets left on read by a bot that jumped in.
"""

from __future__ import annotations

import logging

from .backends.base import Backend
from .config import AppConfig
from .engine.reaction import decide_reaction
from .engine.classifier import build_model
from .flags import FlagManager
from .seen_store import SeenStore, signature
from .models import (
    Decision, Action, Flag, FlagKind, RunSummary,
)

log = logging.getLogger("insta_reactor.runner")


class Runner:
    def __init__(self, backend: Backend, config: AppConfig,
                 flag_manager: FlagManager | None = None,
                 send: bool = True,
                 seen_store: SeenStore | None = None):
        self.backend = backend
        self.config = config
        self.flags = flag_manager or FlagManager()
        # send=False => "plan only": decide everything but never actually send.
        self.send = send
        # Persistent cross-run dedup so we never re-react to the same reel.
        self.seen = seen_store if seen_store is not None else SeenStore()
        # Offline emotion model (None unless enabled+installed => rules only).
        self.model = build_model(config.settings)

    def run(self) -> RunSummary:
        summary = RunSummary()
        self.backend.prepare()

        for chat_name in self.config.enabled_chats:
            try:
                self._process_chat(chat_name, summary)
            except Exception as exc:  # never let one chat kill the whole run
                log.exception("chat %r failed", chat_name)
                d = Decision(
                    action=Action.FLAG,
                    flag=Flag(FlagKind.NAV_FAILED,
                              f"Navigation failed for chat '{chat_name}': {exc}"),
                    chat_name=chat_name,
                )
                self.flags.enqueue(d)
                summary.add(d)
            finally:
                try:
                    self.backend.return_to_inbox()
                except Exception:
                    log.exception("failed returning to inbox after %r", chat_name)

        self.flags.save()
        self.seen.save()
        return summary

    def _process_chat(self, chat_name: str, summary: RunSummary) -> None:
        log.info("opening chat %r", chat_name)
        if not self.backend.open_chat(chat_name):
            d = Decision(
                action=Action.FLAG,
                flag=Flag(FlagKind.NAV_FAILED,
                          f"Could not open chat '{chat_name}' (not found?)."),
                chat_name=chat_name,
            )
            self.flags.enqueue(d)
            summary.add(d)
            return

        seen = 0
        for reel, ctx in self.backend.iter_reels():
            seen += 1

            # Dedup policy (see resend-reels-should-react memory):
            #   * REACTED reels are NOT remembered by content. The backend's
            #     run-start position watermark (iter_reels) already guarantees a
            #     reel we've replied to won't be re-yielded, while a genuine
            #     RE-SEND — a new bubble of the same content, newer than our last
            #     reply — IS yielded and must get a fresh reaction (people
            #     re-share the same reel repeatedly). Skipping by content here
            #     would wrongly drop those re-sends.
            #   * FLAGGED reels ARE remembered by content, because flagging posts
            #     no outgoing message, so the watermark never advances past them
            #     and iter_reels keeps re-yielding them. Without this we'd
            #     re-flag the same unactionable reel every run.
            sig = signature(chat_name, ctx.comments)
            if self.seen.is_reacted(sig):
                log.info("previously-flagged reel %s in %r — not re-flagging, "
                         "continuing the sweep", reel.reel_id, chat_name)
                self.backend.discard_reel(reel)
                continue

            decision = decide_reaction(
                ctx, self.config.profile, self.config.settings, model=self.model)
            # Make sure every decision carries identifying info so the review
            # queue and summary can name it (decide_reaction doesn't know the
            # chat, and some flag paths leave reel_id blank).
            decision.chat_name = decision.chat_name or chat_name
            decision.reel_id = decision.reel_id or reel.reel_id

            replied = False
            send_failed = False
            if decision.action == Action.AUTO_REPLY and self.send:
                if self.backend.send_reply(reel, decision.reply_text or ""):
                    replied = True
                else:
                    send_failed = True
                    decision = Decision(
                        action=Action.FLAG,
                        flag=Flag(FlagKind.NAV_FAILED,
                                  "Reply send failed; queued for manual review."),
                        chat_name=chat_name, reel_id=reel.reel_id,
                        confidence=decision.confidence,
                        breakdown=decision.breakdown,
                    )

            if not replied:
                # flagged, plan-only, or send failed: release the reel's UI so
                # the backend can advance to the next one.
                self.backend.discard_reel(reel)

            if decision.action != Action.AUTO_REPLY:
                self.flags.enqueue(decision)

            # Remember ONLY reels we FLAGGED (not ones we reacted to), so we
            # don't re-flag the same unactionable reel every run while STILL
            # re-reacting to genuine re-sends of reels we've reacted to before.
            # Deliberately NOT recorded: reacted reels (the watermark handles
            # those; content-memory would block re-sends), transient send
            # failures (retry next run), and anything in plan-only (must stay
            # side-effect-free). sig=None reels are never remembered — mark() and
            # is_reacted() no-op on None.
            flagged = decision.action != Action.AUTO_REPLY
            if self.send and not send_failed and flagged:
                self.seen.mark(sig)
            summary.add(decision)

        log.info("chat %r: processed %d reel(s)", chat_name, seen)
        self.backend.return_to_inbox()
