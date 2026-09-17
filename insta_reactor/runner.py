"""Process the enabled chats once, then stop.

This is the on-demand orchestrator: you point it at your approved chats, it
reacts to the obvious/high-confidence reels, queues everything else for you,
prints a summary, and exits. It does NOT stay online monitoring — so if you
were about to reply yourself, nobody gets left on read by a bot that jumped in.
"""

from __future__ import annotations

import logging
import uuid

from .backends.base import Backend
from .config import AppConfig
from .engine.reaction import decide_reaction
from .engine import safety
from .engine.reply_select import diversify_reply
from .engine.classifier import build_model
from .engine.llm_reply import suggest_reply
from .engine.normalize import extract_emojis
from .llm.client import build_llm
from .rag.store import RagStore
from . import interaction_log
from .flags import FlagManager
from .seen_store import SeenStore, signature
from .models import (
    Decision, Action, Flag, FlagKind, RunSummary, CANON_EMOJI,
)

log = logging.getLogger("insta_reactor.runner")


def _is_emoji_only(text: str) -> bool:
    """True if `text` is one or more emoji and nothing else — the only shape
    the native reaction sheet can send (Backend.send_reaction)."""
    return bool(extract_emojis(text)) and not any(ch.isalnum() for ch in text)

# Flags the LLM assist is allowed to try to rescue: the crowd was unreadable to
# the *rules*, but a language model may still find a safe, in-style reaction.
# Deliberately excludes CONTEXT_TEXT / INCOMING_TEXT (need a human), UNABLE_TO_READ
# and TOO_FEW_COMMENTS (no signal to work with).
_LLM_RESCUABLE = {FlagKind.NO_CONSENSUS, FlagKind.LOW_CONFIDENCE}


class Runner:
    def __init__(self, backend: Backend, config: AppConfig,
                 flag_manager: FlagManager | None = None,
                 send: bool = True,
                 seen_store: SeenStore | None = None,
                 log_path: str | None = None):
        self.backend = backend
        self.config = config
        self.flags = flag_manager or FlagManager()
        # send=False => "plan only": decide everything but never actually send.
        self.send = send
        # Persistent cross-run dedup so we never re-react to the same reel.
        self.seen = seen_store if seen_store is not None else SeenStore()
        # Offline emotion model (None unless enabled+installed => rules only).
        self.model = build_model(config.settings)
        # Remote LLM (None unless use_llm + a key => deterministic only) and the
        # RAG memory of your past reactions that grounds its suggestions.
        self.llm = build_llm(config.settings)
        # Where interaction records are appended (injectable so tests stay
        # hermetic; production uses data/interaction_log.jsonl).
        self._log_path = log_path or interaction_log.DEFAULT_LOG_PATH
        self.rag = (RagStore.load(self._log_path,
                                  approved_phrases=config.profile.approved_phrases)
                    if self.llm is not None else RagStore())
        # Correlates every logged decision (and any later override) to this run.
        self._run_id = uuid.uuid4().hex[:12]
        # Replies actually chosen this run, in order — lets diversify_reply keep
        # us from sending the same emoji reaction 3+ times in a row.
        self._recent_replies: list[str] = []
        # Comment-signatures of reels we've actually REACTED to during THIS run.
        # Safety net against the backend's newest-first enumeration re-selecting
        # a reel it already handled (its positional "skip" is scroll-based and
        # can land on the same physical reel twice after a reply mutates the
        # thread — observed live: artby_arco reel got two different replies).
        # Within a single run, a second yield with an identical comment
        # signature is that same physical reel, not a genuine re-send (re-sends
        # are handled across runs by the watermark, deliberately NOT by content
        # — see resend-reels-should-react). Reset per run in run().
        self._reacted_sigs: set = set()

    def run(self) -> RunSummary:
        summary = RunSummary()
        self._reacted_sigs = set()   # fresh per run (see field doc)
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

    def _grounding(self, decision: Decision) -> str:
        """One-line description of the deterministic crowd analysis, fed to the
        LLM so its synthesis builds on a real signal, not a blind guess."""
        emo = decision.winning_emotion
        if not emo:
            return ""
        rep = CANON_EMOJI.get(emo, "")
        return (f"- strongest crowd emotion: {emo} {rep}\n"
                f"- consensus strength: {decision.confidence:.2f} (0=none, 1=total)")

    def _maybe_llm(self, ctx, decision: Decision) -> Decision:
        """AI-native reply layer — let the LLM compose the reaction.

        Two triggers, both no-ops unless the LLM is enabled (use_llm + a key):
          * RESCUE  — the rules FLAGGED an ambiguous reel (no_consensus/
            low_confidence); the model may still find a safe, in-style reply,
            UPGRADING the flag to an auto-reply.
          * SYNTHESISE — with settings.llm_mode == "always", the model composes
            the reply for EVERY auto-reply candidate, grounded by the crowd
            analysis + your style + RAG. This is the "AI-native" path: the model
            intelligently synthesises the reaction from the comments.

        The model can only ever produce a safe, in-style, length-checked reply;
        on any failure/refusal the deterministic decision is kept untouched, so
        the AI can improve a reply but never make things worse. Never raises.
        """
        if self.llm is None:
            return decision
        mode = getattr(self.config.settings, "llm_mode", "assist")
        is_rescue = (decision.action == Action.FLAG and decision.flag
                     and decision.flag.kind in _LLM_RESCUABLE)
        is_synth = decision.action == Action.AUTO_REPLY and mode == "always"
        if not (is_rescue or is_synth):
            return decision
        try:
            examples = self.rag.format_examples(
                self.rag.retrieve(ctx, self.config.settings.rag_top_k))
            sugg = suggest_reply(ctx, self.config.profile, self.config.settings,
                                 self.llm, examples, self._grounding(decision))
        except Exception:
            log.exception("LLM reply failed for reel %s", decision.reel_id)
            return decision
        # Deterministic tone net: even a parsed, in-style reply can be tone-deaf
        # (a small model still "lmao"s a reunion or an explainer sometimes). If
        # the reply laughs at / hypes clearly heartfelt or educational content,
        # don't send it — treat it exactly like a decline and hand to a human.
        if sugg is not None:
            clash = safety.tone_conflict(
                getattr(ctx, "caption", "") or "", getattr(ctx, "comments", None),
                sugg.reply_text)
            if clash:
                log.info("tone guard rejected %r for reel %s (%s)",
                         sugg.reply_text, decision.reel_id, clash)
                sugg = None
        if sugg is None:
            if is_synth:
                # always-mode: the LLM is the author. If it declined
                # (should_reply=false) or failed, do NOT fall back to the
                # context-blind deterministic reply — that's exactly how a hype
                # 'lmao'/🔥 lands on a somber or wholesome reel. Hand to a human.
                return Decision(
                    action=Action.FLAG,
                    flag=Flag(FlagKind.LLM_DECLINED,
                              "The reel didn't fit a safe short reaction "
                              "(model declined). Read and reply manually."),
                    winning_emotion=decision.winning_emotion,
                    confidence=decision.confidence,
                    breakdown=decision.breakdown,
                    chat_name=decision.chat_name,
                    reel_id=decision.reel_id,
                )
            return decision   # rescue: keep the original flag
        log.info("LLM %s reel %s -> %r (conf %.2f)",
                 "rescued" if is_rescue else "synthesised",
                 decision.reel_id, sugg.reply_text, sugg.confidence)
        return Decision(
            action=Action.AUTO_REPLY,
            reply_text=sugg.reply_text,
            reply_source=sugg.source,           # "llm"
            winning_emotion=decision.winning_emotion,
            # synth keeps the (already-passing) deterministic confidence as a
            # floor; rescue takes the model's, which had to clear llm_min_confidence.
            confidence=max(decision.confidence, sugg.confidence) if is_synth
            else sugg.confidence,
            breakdown=decision.breakdown,
            chat_name=decision.chat_name,
            reel_id=decision.reel_id,
        )

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

        # Surface plain text messages (even one). The bot only reacts to reels,
        # so any incoming text needs a human — we flag it and push a notification
        # so nothing gets silently left on read. Done before the reel sweep so
        # the watermark is read pre-reply. Never let this abort the chat.
        try:
            for txt in self.backend.unanswered_incoming_texts():
                d = Decision(
                    action=Action.FLAG,
                    flag=Flag(FlagKind.INCOMING_TEXT, txt),
                    chat_name=chat_name,
                    reel_id="text",
                )
                self.flags.enqueue(d)
                summary.add(d)
        except Exception:
            log.exception("incoming-text scan failed for %r", chat_name)

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
            if sig is not None and sig in self._reacted_sigs:
                # Same physical reel the enumeration already handed us (and we
                # reacted to) earlier in THIS run — don't reply to it twice.
                log.info("reel %s in %r has the same comments as one already "
                         "reacted to this run — skipping to avoid a duplicate "
                         "reply", reel.reel_id, chat_name)
                self.backend.discard_reel(reel)
                continue

            decision = decide_reaction(
                ctx, self.config.profile, self.config.settings, model=self.model)
            # Make sure every decision carries identifying info so the review
            # queue and summary can name it (decide_reaction doesn't know the
            # chat, and some flag paths leave reel_id blank).
            decision.chat_name = decision.chat_name or chat_name
            decision.reel_id = decision.reel_id or reel.reel_id

            # AI-native reply: let the (RAG-grounded, crowd-grounded) model
            # compose the reaction — rescuing an ambiguous flag, or synthesising
            # every reply when llm_mode="always". No-op unless use_llm + a key.
            decision = self._maybe_llm(ctx, decision)

            # Carry the reel's thumbnail + thread position onto the decision so
            # the review UI can show a picture of it AND tell the user exactly
            # where to look ("Nth reel from the bottom") — esp. for reels we
            # could NOT react to and are handing back to the human.
            decision.thumbnail_path = getattr(ctx, "thumbnail_path", None)
            decision.position_from_bottom = getattr(ctx, "position_from_bottom", None)

            # Reply variety: vary the emoji count / blend in a common non-favourite
            # emoji, and never send the same reaction 3x in a row. Only touches
            # pure-emoji replies (leaves echoed popular comments & text replies as
            # they are). Done here (not in decide_reaction) because it depends on
            # what we've already sent this run.
            if decision.action == Action.AUTO_REPLY:
                decision.reply_text = diversify_reply(
                    decision.reply_text or "", decision.reply_source or "",
                    ctx.comments or [], self.config.profile,
                    self.config.settings, self._recent_replies)
                self._recent_replies.append(decision.reply_text or "")

            replied = False
            send_failed = False
            if decision.action == Action.AUTO_REPLY and self.send:
                text = decision.reply_text or ""
                sent = False
                # Bare emoji replies get a real long-press-style reaction
                # first; anything that isn't a clean emoji-only string (or
                # that fails the native gesture) falls back to the typed
                # reply path, unchanged from before.
                if (_is_emoji_only(text) and self.config.settings.native_reaction_enabled):
                    sent = self.backend.send_reaction(reel, text)
                if not sent:
                    sent = self.backend.send_reply(reel, text)
                if sent:
                    replied = True
                    if sig is not None:
                        self._reacted_sigs.add(sig)
                else:
                    send_failed = True
                    decision = Decision(
                        action=Action.FLAG,
                        flag=Flag(FlagKind.NAV_FAILED,
                                  "Reply send failed; queued for manual review."),
                        chat_name=chat_name, reel_id=reel.reel_id,
                        confidence=decision.confidence,
                        breakdown=decision.breakdown,
                        thumbnail_path=getattr(ctx, "thumbnail_path", None),
                        position_from_bottom=getattr(ctx, "position_from_bottom", None),
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

            # Append to the interaction log — the corpus RAG, correction
            # learning, and the eval harness all read from. Never aborts a run.
            if self.config.settings.log_interactions:
                interaction_log.log_decision(
                    ctx, decision, sent=replied, run_id=self._run_id,
                    path=self._log_path,
                    log_raw_text=self.config.settings.log_raw_text)

        log.info("chat %r: processed %d reel(s)", chat_name, seen)
        self.backend.return_to_inbox()
