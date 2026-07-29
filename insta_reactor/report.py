"""Human-readable rendering of decisions and run summaries."""

from __future__ import annotations

from .models import Decision, Action, RunSummary, FlagKind, CANON_EMOJI


FLAG_LABEL = {
    FlagKind.CONTEXT_TEXT: "text before reel",
    FlagKind.TOO_FEW_COMMENTS: "fewer than threshold comments",
    FlagKind.NO_CONSENSUS: "no clear consensus",
    FlagKind.UNABLE_TO_READ: "unable to read comments",
    FlagKind.LOW_CONFIDENCE: "low confidence",
    FlagKind.NAV_FAILED: "navigation failed",
    FlagKind.INCOMING_TEXT: "text message (needs a human reply)",
}


def explain(decision: Decision) -> str:
    """A verbose, single-decision explanation (the 'why')."""
    lines: list[str] = []
    head = f"[{decision.chat_name}] reel {decision.reel_id or '?'}"
    lines.append(head)
    if decision.action == Action.AUTO_REPLY:
        emoji = CANON_EMOJI.get(decision.winning_emotion or "", "")
        src = f", via={decision.reply_source}" if decision.reply_source else ""
        lines.append(f"  -> AUTO-REPLY: {decision.reply_text!r}  "
                     f"(emotion={decision.winning_emotion} {emoji}, "
                     f"confidence={decision.confidence:.0%}{src})")
    else:
        kind = decision.flag.kind if decision.flag else "?"
        reason = decision.flag.reason if decision.flag else ""
        lines.append(f"  -> FLAG [{FLAG_LABEL.get(kind, kind)}]: {reason}")

    b = decision.breakdown
    if b and b.final:
        def fmt(d: dict) -> str:
            return ", ".join(f"{k} {v:.0%}" for k, v in
                             sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:4])
        lines.append(f"     public : {fmt(b.public_norm)}")
        if b.personal_norm:
            lines.append(f"     you    : {fmt(b.personal_norm)}")
        lines.append(f"     final  : {fmt(b.final)}")
        lines.append(f"     n={b.n_comments}  top_share={b.top_share:.0%}  "
                     f"margin={b.margin:.0%}")
    return "\n".join(lines)


def to_dict(summary: RunSummary, plan_only: bool = False) -> dict:
    """Machine-readable run summary (for the phone app / --json output)."""
    auto_replied = [
        {
            "chat": d.chat_name,
            "reel_id": d.reel_id,
            "reply": d.reply_text,
            "emotion": d.winning_emotion,
            "confidence": d.confidence,
        }
        for d in summary.auto_replied
    ]
    flagged = [
        {
            "chat": d.chat_name,
            "reel_id": d.reel_id,
            "kind": d.flag.kind if d.flag else None,
            "label": FLAG_LABEL.get(d.flag.kind, d.flag.kind) if d.flag else None,
            "reason": d.flag.reason if d.flag else "",
        }
        for d in summary.flagged
    ]
    return {
        "plan_only": plan_only,
        "counts": {"auto_replied": len(auto_replied), "flagged": len(flagged)},
        "auto_replied": auto_replied,
        "flagged": flagged,
    }


def summarize(summary: RunSummary, verbose: bool = False, plan_only: bool = False) -> str:
    lines: list[str] = []
    lines.append("=" * 56)
    lines.append("RUN SUMMARY")
    lines.append("=" * 56)
    verb = "would be auto-replied (nothing sent)" if plan_only else "auto-replied"
    lines.append(f"  ✅ {len(summary.auto_replied)} reel(s) {verb}")
    counts = summary.counts_by_flag()
    total_flagged = len(summary.flagged)
    lines.append(f"  ⚠️  {total_flagged} reel(s) flagged for manual review")
    for kind, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"       - {n} ({FLAG_LABEL.get(kind, kind)})")
    lines.append("")

    if summary.auto_replied:
        lines.append("Would auto-reply (plan-only):" if plan_only else "Auto-replied:")
        for d in summary.auto_replied:
            emoji = CANON_EMOJI.get(d.winning_emotion or "", "")
            lines.append(f"  ✅ [{d.chat_name}] {d.reel_id}: {d.reply_text!r} "
                         f"({d.confidence:.0%})")
        lines.append("")

    if summary.flagged:
        lines.append("Flagged (needs you):")
        for d in summary.flagged:
            kind = d.flag.kind if d.flag else "?"
            reason = d.flag.reason if d.flag else ""
            lines.append(f"  ⚠️  [{d.chat_name}] {d.reel_id}: "
                         f"{FLAG_LABEL.get(kind, kind)} — {reason}")
        lines.append("")

    if verbose:
        lines.append("-" * 56)
        lines.append("DETAIL")
        lines.append("-" * 56)
        for d in summary.auto_replied + summary.flagged:
            lines.append(explain(d))
            lines.append("")

    return "\n".join(lines)
