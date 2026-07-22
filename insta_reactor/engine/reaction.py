"""The decision layer: ReelContext + Profile + Settings -> Decision.

This is the whole "should I reply, and with what?" brain. It is completely
deterministic and device-agnostic, so it is exercised end-to-end by the unit
tests without any Instagram involved.

Order of checks mirrors the spec exactly:
  Rule 1  text before reel      -> flag (context)
          could not read        -> flag (unable to read)
  Rule 2  < min_comments        -> flag (too few comments)
          no public signal      -> flag (no consensus)
          weak consensus        -> flag (no consensus)
          low confidence        -> flag (low confidence)
          else                  -> auto reply
"""

from __future__ import annotations

from ..models import (
    ReelContext, Profile, Settings, Decision, Action, Flag, FlagKind,
    ScoreBreakdown,
)
from . import scoring
from .reply_select import select_reply


def _flag(ctx: ReelContext, kind: str, reason: str,
          breakdown: ScoreBreakdown | None = None,
          confidence: float = 0.0) -> Decision:
    return Decision(
        action=Action.FLAG,
        flag=Flag(kind=kind, reason=reason),
        breakdown=breakdown,
        confidence=confidence,
        chat_name=ctx.chat_name,
        reel_id=ctx.reel_id,
    )


def decide_reaction(ctx: ReelContext, profile: Profile, settings: Settings,
                    model: "object | None" = None) -> Decision:
    # --- Rule 1: contextual text immediately before the reel ---------------
    if ctx.has_preceding_text:
        preview = (ctx.preceding_text or "").strip()
        extra = f' ("{preview}")' if preview else ""
        return _flag(
            ctx, FlagKind.CONTEXT_TEXT,
            f"This reel has contextual text{extra}. Read and reply manually.",
        )

    # --- Rule 1b: sender's own follow-up text right after the reel ---------
    # Usually means an inside joke or specific comment about the reel that
    # needs a human, not a generic crowd-matched reaction.
    if ctx.has_following_text:
        preview = (ctx.following_text or "").strip()
        extra = f' ("{preview}")' if preview else ""
        return _flag(
            ctx, FlagKind.CONTEXT_TEXT,
            f"Sender followed this reel with a message{extra}. "
            f"Read and reply manually.",
        )

    # --- Could we even read the comments? ---------------------------------
    if ctx.read_error or ctx.comments is None:
        return _flag(
            ctx, FlagKind.UNABLE_TO_READ,
            "Unable to read comments. Read and reply manually.",
        )

    # --- Rule 2: minimum comment threshold --------------------------------
    n = ctx.effective_count()
    if n < settings.min_comments:
        return _flag(
            ctx, FlagKind.TOO_FEW_COMMENTS,
            f"Reel has fewer than {settings.min_comments} comments "
            f"({n}). Read and reply manually.",
        )

    # --- Score the crowd (rules + optional offline model ensemble) ---------
    public_raw = scoring.public_scores(
        ctx.comments, settings, profile.extra_slang, model=model)
    public_norm = scoring._normalize(public_raw)

    if not public_norm:
        return _flag(
            ctx, FlagKind.NO_CONSENSUS,
            "No recognizable reactions found in the comments. "
            "Read and reply manually.",
            breakdown=ScoreBreakdown(n_comments=n),
        )

    public_winner, top_share, margin = scoring.consensus_metrics(public_norm)

    # --- Apply personal bias to decide *what* to send ---------------------
    personal_norm = scoring._normalize(scoring.personal_scores(profile))
    final = scoring.combine(public_norm, personal_norm, settings)
    winner = max(final.items(), key=lambda kv: kv[1])[0] if final else public_winner

    confidence = scoring.confidence_score(top_share, margin, n, settings)

    # --- Personal echo: your own emoji/common replies literally show up ----
    # enough times in these comments on their own, regardless of whether the
    # wider crowd agrees with itself.
    echo_counts = scoring.personal_echo_counts(ctx.comments, profile)
    echo_hits = echo_counts.get(winner, 0)
    personal_echo = echo_hits >= settings.personal_echo_min_matches

    breakdown = ScoreBreakdown(
        public_raw={k: round(v, 4) for k, v in public_raw.items()},
        public_norm={k: round(v, 4) for k, v in public_norm.items()},
        personal_norm={k: round(v, 4) for k, v in personal_norm.items()},
        final={k: round(v, 4) for k, v in final.items()},
        top_share=round(top_share, 4),
        margin=round(margin, 4),
        n_comments=n,
        winner=winner,
        public_winner=public_winner,
    )

    # --- Consensus gate: is the crowd clear enough? -----------------------
    # Skipped when your own style is directly echoed often enough in the
    # comments — that's stronger evidence than crowd agreement.
    if not personal_echo and (top_share < settings.min_top_share or margin < settings.min_margin):
        return _flag(
            ctx, FlagKind.NO_CONSENSUS,
            f"No clear reaction consensus (top share {top_share:.0%}, "
            f"margin {margin:.0%}). Read and reply manually.",
            breakdown=breakdown, confidence=confidence,
        )

    # --- Confidence gate --------------------------------------------------
    if confidence < settings.auto_reply_min_confidence:
        return _flag(
            ctx, FlagKind.LOW_CONFIDENCE,
            f"Automation confidence too low ({confidence:.0%}). "
            "Read and reply manually.",
            breakdown=breakdown, confidence=confidence,
        )

    # --- High confidence: choose the reaction to send ---------------------
    # Prefer echoing a very-popular comment, else a favourite emoji the crowd
    # is using, else an emotion-styled reply. See engine/reply_select.py.
    reply_text, reply_source = select_reply(ctx, winner, profile, settings)
    return Decision(
        action=Action.AUTO_REPLY,
        reply_text=reply_text,
        reply_source=reply_source,
        winning_emotion=winner,
        confidence=confidence,
        breakdown=breakdown,
        chat_name=ctx.chat_name,
        reel_id=ctx.reel_id,
    )
