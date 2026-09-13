"""Eval harness — grade the engine against your own logged behaviour.

Two evaluations, both driven off the interaction log (interaction_log.jsonl):

  1. BEHAVIOURAL (always available) — from the decision + override records:
     how often the bot auto-replied vs flagged, the flag breakdown, which reply
     sources it used, and the agreement rate (how often you did NOT correct its
     proposals). This is the headline "is it saving me time / how often do I have
     to step in" number.

  2. REPLAY (when the log was captured with log_raw_text=True, so the raw
     comments are present) — re-run the CURRENT deterministic engine over each
     past reel and check:
       * `replay_reproduced`      — does it still produce the reply it logged?
         (a regression test grounded in real usage — catches when tuning
         slang_map / settings silently changes past decisions)
       * `replay_matches_override`— does it now produce what you actually sent
         after correcting it? (did a profile/settings change move the engine
         toward your true preference?)

Run:  python -m insta_reactor.tools.eval_harness [--log PATH] [--replay]
`--replay` uses the profile+settings from data/config.json.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, asdict

from ..models import Comment, ReelContext, Action
from ..engine.reaction import decide_reaction
from ..interaction_log import read_all, DEFAULT_LOG_PATH
from ..learning.corrections import agreement_rate


@dataclass
class EvalReport:
    n_decisions: int = 0
    n_auto: int = 0
    n_flag: int = 0
    n_sent: int = 0
    n_overrides: int = 0
    agreement: float | None = None
    flag_counts: dict = field(default_factory=dict)
    source_counts: dict = field(default_factory=dict)
    # replay (only populated when profile+settings given AND raw comments logged)
    n_replayable: int = 0
    replay_reproduced: int = 0
    replay_matches_override: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        lines = [
            "Interaction eval",
            "----------------",
            f"decisions logged : {self.n_decisions}"
            f"  (auto {self.n_auto}, flag {self.n_flag}, sent {self.n_sent})",
            f"overrides (you)  : {self.n_overrides}",
            f"agreement rate   : "
            + ("n/a" if self.agreement is None else f"{self.agreement:.0%}"),
        ]
        if self.flag_counts:
            lines.append("flag breakdown   : " + ", ".join(
                f"{k} {v}" for k, v in sorted(self.flag_counts.items())))
        if self.source_counts:
            lines.append("reply sources    : " + ", ".join(
                f"{k} {v}" for k, v in sorted(self.source_counts.items())))
        if self.n_replayable:
            lines += [
                f"replayable reels : {self.n_replayable}",
                f"  reproduced now : {self.replay_reproduced}"
                f" ({self.replay_reproduced / self.n_replayable:.0%})",
                f"  now matches you: {self.replay_matches_override}",
            ]
        return "\n".join(lines)


def _ctx_from_record(rec: dict) -> ReelContext | None:
    raw = rec.get("comments_raw")
    if not raw:
        return None
    comments = [Comment(text=c.get("text", ""), likes=int(c.get("likes", 0)))
                for c in raw]
    return ReelContext(chat_name=rec.get("chat", ""),
                       reel_id=rec.get("reel_id", ""), comments=comments)


def evaluate(records, profile=None, settings=None) -> EvalReport:
    """Compute an EvalReport. Pass profile+settings to enable replay metrics."""
    records = list(records)
    rep = EvalReport()

    overrides_by_sig: dict[str, dict] = {}
    for r in records:
        if r.get("kind") == "override":
            rep.n_overrides += 1
            overrides_by_sig[r.get("comment_sig", "")] = r

    for r in records:
        if r.get("kind") != "decision":
            continue
        rep.n_decisions += 1
        if r.get("sent"):
            rep.n_sent += 1
        if r.get("action") == Action.AUTO_REPLY:
            rep.n_auto += 1
            src = r.get("reply_source") or "unknown"
            rep.source_counts[src] = rep.source_counts.get(src, 0) + 1
        else:
            rep.n_flag += 1
            fk = r.get("flag_kind") or "unknown"
            rep.flag_counts[fk] = rep.flag_counts.get(fk, 0) + 1

        if profile is not None and settings is not None:
            ctx = _ctx_from_record(r)
            if ctx is not None:
                rep.n_replayable += 1
                d = decide_reaction(ctx, profile, settings)
                new_reply = d.reply_text if d.action == Action.AUTO_REPLY else None
                if new_reply is not None and new_reply == r.get("reply_text"):
                    rep.replay_reproduced += 1
                ov = overrides_by_sig.get(r.get("comment_sig", ""))
                if ov and new_reply is not None and new_reply == ov.get("user_reply"):
                    rep.replay_matches_override += 1

    rep.agreement = agreement_rate(records)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval_harness",
                                 description="Grade the engine vs your logged behaviour.")
    ap.add_argument("--log", default=DEFAULT_LOG_PATH)
    ap.add_argument("--replay", action="store_true",
                    help="also replay past reels through the current engine "
                         "(needs raw comments in the log + data/config.json)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    profile = settings = None
    if args.replay:
        from ..config import load_config
        cfg = load_config()
        profile, settings = cfg.profile, cfg.settings

    rep = evaluate(read_all(args.log), profile, settings)
    print(json.dumps(rep.to_dict()) if args.json else rep.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
