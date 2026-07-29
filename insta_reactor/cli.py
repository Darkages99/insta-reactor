"""Command-line entry point.

Commands
--------
  setup                 first-launch wizard: capture your reaction profile
  chats add|remove|list manage which conversations are automation-enabled
  run                   process enabled chats on a real Android device (adb/u2)
  run --simulate FILE   process a JSON fixture instead of a phone (safe demo)
  run --plan-only       decide + summarize but never actually send
  queue                 show the pending manual-review queue
  explain --simulate F  print the full per-reel reasoning for a fixture

Run `python -m insta_reactor <command> -h` for details.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__
from .config import (
    AppConfig, load_config, save_config, config_exists, DEFAULT_CONFIG_PATH,
)
from .models import Profile, Settings, ReplyStyle
from .flags import FlagManager
from .runner import Runner
from .report import summarize, explain, to_dict
from .backends.simulated import SimulatedBackend


def _force_utf8() -> None:
    """Windows consoles default to cp1252 and choke on emojis. Fix stdout."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # py3.7+
        except Exception:
            pass


# --------------------------------------------------------------------------
# setup wizard
# --------------------------------------------------------------------------

def cmd_setup(args) -> int:
    print("Instagram Reel Auto-Reactor — setup\n")
    print("This builds the profile that makes replies sound like YOU.\n")

    def ask(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        val = input(f"{prompt}{suffix}: ").strip()
        return val or default

    print("Enter your reaction emojis, most-preferred first.")
    primary = ask("Primary emoji", "💀")
    secondary = ask("Secondary emoji", "😭")
    third = ask("Third emoji", "😂")
    emoji_prefs = [e for e in (primary, secondary, third) if e]

    print("\nEnter common replies you actually type (comma-separated).")
    print("Examples: bro 💀, nah 😭, LMAOO, 😭, 💀")
    replies_raw = ask("Common replies", "bro 💀, nah 😭, LMAOO, 😭, 💀")
    common_replies = [r.strip() for r in replies_raw.split(",") if r.strip()]

    print("\nPreferred reply style:")
    print("  1) single emoji        (💀)")
    print("  2) double emoji        (💀💀)")
    print("  3) short text + emoji  (bro 💀)")
    style_choice = ask("Choose 1/2/3", "1")
    style = {
        "1": ReplyStyle.SINGLE,
        "2": ReplyStyle.DOUBLE,
        "3": ReplyStyle.TEXT_EMOJI,
    }.get(style_choice, ReplyStyle.SINGLE)

    config = load_config(args.config) if config_exists(args.config) else AppConfig()
    config.profile = Profile(
        emoji_prefs=emoji_prefs,
        common_replies=common_replies,
        reply_style=style,
        extra_slang=config.profile.extra_slang,
    )
    save_config(config, args.config)
    print(f"\nSaved profile to {args.config}")
    print("Next: add the chats you want automated, e.g.")
    print('  python -m insta_reactor chats add "Best Friend"')
    return 0


# --------------------------------------------------------------------------
# chats management
# --------------------------------------------------------------------------

def cmd_chats(args) -> int:
    config = load_config(args.config)
    if args.action == "list":
        if not config.enabled_chats:
            print("No chats enabled. Add one with: chats add \"<name>\"")
        else:
            print("Automation-enabled chats:")
            for c in config.enabled_chats:
                print(f"  - {c}")
        return 0

    if args.action == "add":
        if args.name not in config.enabled_chats:
            config.enabled_chats.append(args.name)
            save_config(config, args.config)
            print(f"Enabled: {args.name!r}")
        else:
            print(f"Already enabled: {args.name!r}")
        return 0

    if args.action == "remove":
        if args.name in config.enabled_chats:
            config.enabled_chats.remove(args.name)
            save_config(config, args.config)
            print(f"Disabled: {args.name!r}")
        else:
            print(f"Not enabled: {args.name!r}")
        return 0
    return 1


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def _build_backend(args, config: AppConfig):
    if args.simulate:
        return SimulatedBackend.from_file(args.simulate)
    # Real device path. Imported lazily so the deterministic core never
    # requires uiautomator2 / adb to be installed.
    from .backends.android import AndroidBackend
    return AndroidBackend(config)


def cmd_run(args) -> int:
    if args.debug:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config(args.config)

    # In simulate mode we can synthesize enabled_chats from the fixture so a
    # brand-new user can see it work with zero setup.
    try:
        backend = _build_backend(args, config)
    except RuntimeError as e:
        # e.g. uiautomator2 not installed / device not reachable
        print(f"Cannot start device backend: {e}")
        print("Tip: use `run --simulate data/fixtures.example.json` to try the "
              "pipeline without a phone.")
        return 2
    if args.simulate and not config.enabled_chats:
        with open(args.simulate, "r", encoding="utf-8") as f:
            config.enabled_chats = list(json.load(f).get("chats", {}).keys())

    if not config.enabled_chats:
        print("No chats enabled. Add some with: chats add \"<name>\"")
        return 1

    if args.use_model:
        config.settings.use_model = True
    if config.settings.use_model:
        print("Offline emotion model: ENABLED "
              f"({config.settings.model_name}, weight "
              f"{config.settings.ensemble_model_weight})")

    from .seen_store import SeenStore
    runner = Runner(backend, config, FlagManager(args.queue),
                    send=not args.plan_only,
                    seen_store=SeenStore(args.seen))
    summary = runner.run()
    backend.close()

    if args.json:
        print(json.dumps(to_dict(summary, plan_only=args.plan_only)))
        return 0

    print(summarize(summary, verbose=args.verbose, plan_only=args.plan_only))
    if args.plan_only:
        print("(plan-only: nothing was actually sent)")
    if summary.flagged:
        print(f"\n{len(summary.flagged)} item(s) need your attention — "
              f"saved to {args.queue}")
        print("Review them anytime with:  python -m insta_reactor queue")

    # Push alerts to the phone (text messages, couldn't-respond, needs-review),
    # unless this was a dry run. Works the same whether launched from the PC or
    # the on-device Termux runner, so you get told without watching the PC.
    if not args.plan_only:
        from .notify import notify_from_summary
        notify_from_summary(config.ntfy_topic, summary)
        if not config.ntfy_topic and summary.flagged:
            print("\n(no ntfy_topic set — push notifications were skipped. "
                  "Set one with:  python -m insta_reactor webui --ntfy-topic "
                  "<your-topic>  then subscribe to it in the ntfy app.)")
    return 0


def cmd_webui(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config(args.config)
    if args.ntfy_topic:
        config.ntfy_topic = args.ntfy_topic
        save_config(config, args.config)

    from .webui import run_server
    run_server(args.config, args.queue, args.seen, port=args.port)
    return 0


def cmd_queue(args) -> int:
    fm = FlagManager(args.queue)
    pending = fm.pending()
    if not pending:
        print("Review queue is empty. 🎉")
        return 0
    print(f"{len(pending)} item(s) awaiting manual review:\n")
    for it in pending:
        print(f"  ⚠️  [{it.chat_name}] {it.reel_id}: {it.flag_kind}")
        print(f"       {it.reason}")
    return 0


def cmd_explain(args) -> int:
    config = load_config(args.config)
    backend = SimulatedBackend.from_file(args.simulate)
    backend.prepare()
    chats = list(json.load(open(args.simulate, encoding="utf-8")).get("chats", {}))
    from .engine.reaction import decide_reaction
    for chat in chats:
        backend.open_chat(chat)
        for reel in backend.find_unreacted_reels():
            ctx = backend.build_reel_context(reel)
            d = decide_reaction(ctx, config.profile, config.settings)
            print(explain(d))
            print()
    return 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="insta_reactor",
        description="On-demand Instagram reel auto-reactor (deterministic).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                   help="path to config.json")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("setup", help="first-launch profile wizard")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("chats", help="manage automation-enabled chats")
    sp.add_argument("action", choices=["add", "remove", "list"])
    sp.add_argument("name", nargs="?", default="")
    sp.set_defaults(func=cmd_chats)

    sp = sub.add_parser("run", help="process enabled chats once, then stop")
    sp.add_argument("--simulate", metavar="FIXTURE.json",
                    help="use a JSON fixture instead of a real device")
    sp.add_argument("--plan-only", action="store_true",
                    help="decide + summarize but never send")
    sp.add_argument("--verbose", action="store_true",
                    help="print full per-reel reasoning")
    sp.add_argument("--use-model", action="store_true",
                    help="enable the offline emotion model for this run "
                         "(requires: pip install transformers torch)")
    sp.add_argument("--debug", action="store_true",
                    help="print live navigation/state-machine logging "
                         "(what it's doing on-device, step by step)")
    sp.add_argument("--json", action="store_true",
                    help="print a single JSON summary line instead of human "
                         "text (for the phone app / scripting)")
    sp.add_argument("--queue", default=os.path.join("data", "review_queue.json"))
    sp.add_argument("--seen", default=os.path.join("data", "handled_reels.json"),
                    help="path to the persistent already-reacted store")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("webui", help="phone-facing control panel: pick chats, tap Run")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--ntfy-topic", default="",
                    help="ntfy.sh topic for error/unsure push notifications "
                         "(saved to config.json once set)")
    sp.add_argument("--queue", default=os.path.join("data", "review_queue.json"))
    sp.add_argument("--seen", default=os.path.join("data", "handled_reels.json"))
    sp.set_defaults(func=cmd_webui)

    sp = sub.add_parser("queue", help="show pending manual-review items")
    sp.add_argument("--queue", default=os.path.join("data", "review_queue.json"))
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("explain", help="print full reasoning for a fixture")
    sp.add_argument("--simulate", required=True, metavar="FIXTURE.json")
    sp.set_defaults(func=cmd_explain)

    return p


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
