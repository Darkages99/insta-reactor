"""Flask control-panel: a chat-centric review of what the bot went over.

The point of the tool is to spare you from scrolling spam. So the panel is built
around your CHATS, not a flat wall of reels:

  * You open it and see every chat the bot has gone over.
  * A chat with reels it couldn't safely handle wears a badge ("2 need you").
  * You click that chat open and, for each flagged reel, see a thumbnail, a
    plain reason, and exactly WHERE it is — "3rd reel from the bottom" — so you
    can jump to it in your DM and handle it. A "Mark handled" button clears it.
  * Chats it fully handled show "all clear".

Design notes:
  * The reactor run happens in a BACKGROUND THREAD (Playwright sync must own its
    thread, and the browser can take a while), while the page polls /status.
  * Results persist to data/review.json (ReviewStore), so re-opening the panel
    later still shows the last state without re-running.
  * SAFETY: when "live send" is on, sends are hard-restricted to the selected
    chats (send_whitelist), on top of the runner only visiting those chats.
    Dry-run (send nothing) is the default.
"""

from __future__ import annotations

import logging
import os
import re as _re
import threading
import time

from flask import Flask, request, redirect, send_file, jsonify, abort

from ..config import (load_config, save_config, config_exists, AppConfig,
                      DEFAULT_CONFIG_PATH)
from ..flags import FlagManager
from ..seen_store import SeenStore
from ..runner import Runner
from ..review_store import ReviewStore, DEFAULT_REVIEW_PATH
from ..paths import data_path

log = logging.getLogger("insta_reactor.web")

app = Flask(__name__)

_CONFIG_PATH = DEFAULT_CONFIG_PATH
_THUMBS_DIR = data_path("thumbs")
_REVIEW_PATH = DEFAULT_REVIEW_PATH
# The live "watch the bot work" frame. The backend overwrites this PNG at every
# scroll/open/send; the /live.png route serves it and the page refreshes it.
_LIVE_FRAME = data_path("live", "frame.png")
# Each Instagram account gets its own Chromium user-data dir under here, named
# after the account itself (e.g. "sarangdrajgopaul", "Event Forge"), so each
# login is fully isolated and switching accounts never logs another one out.
_PROFILES_BASE = data_path("browser_profiles")


def _list_accounts() -> list[str]:
    if not os.path.isdir(_PROFILES_BASE):
        return []
    return sorted(
        n for n in os.listdir(_PROFILES_BASE)
        if os.path.isdir(os.path.join(_PROFILES_BASE, n)))


def _account_profile_dir(account: str) -> str:
    account = (account or "").strip()
    return os.path.join(_PROFILES_BASE, account)

# A browser left open waiting for a manual login (BrowserBackend.prepare()
# raises "not logged in" but deliberately leaves the window open) is cached
# here per profile_dir so the NEXT run reuses that same live browser instead
# of launching a second Chromium against a profile dir the first one still
# owns — that collision is what previously produced Playwright's confusing
# "Opening in existing browser session" failure.
_LIVE_BACKENDS: dict = {}

# Shared run state (single active run at a time — this is a personal tool).
_LOCK = threading.Lock()
_STATE: dict = {
    "status": "idle",          # idle | running | done | error
    "message": "",
    "log": [],
    "events": [],              # structured narration for the live feed
    "event_seq": 0,            # monotonic id so the poller fetches only new ones
    "stats": {"chats": 0, "reacted": 0, "read": 0},
    "dry_run": False,
    "started_at": None,
    "finished_at": None,
    "account": "",
    # Kill switch: the Stop button sets this; the run's should_stop callable
    # reads it and the runner/backend abort at the next reel boundary.
    "stop_requested": False,
    # Why the last run ended early (you pressed Stop, or the bot saw you step in),
    # shown as a banner so a cut-short run never looks like a clean one.
    "stop_reason": "",
}


def _emit_event(icon: str, text: str, kind: str = "info") -> None:
    """Append one narration event (assumes _LOCK held)."""
    _STATE["event_seq"] += 1
    _STATE["events"].append({
        "id": _STATE["event_seq"], "t": time.strftime("%H:%M:%S"),
        "icon": icon, "text": text, "kind": kind,
    })
    _STATE["events"] = _STATE["events"][-200:]


# Log line -> friendly narration. Each entry: compiled regex -> builder(match)
# returning (icon, text, kind, stat_key_or_None). Ordered; first match wins.
# We narrate off the runner/backend's existing INFO logs so there's one source
# of truth and the raw console stays available underneath for debugging.
_EVENT_RULES = [
    (_re.compile(r"^browser ready"),
     lambda m: ("🌐", "Signed in — browser ready", "system", None)),
    (_re.compile(r"^opening chat '(.+?)'"),
     lambda m: ("📂", f"Opening chat · {m.group(1)}", "chat", None)),
    (_re.compile(r"^replied '(.*?)' to reel \S+ in '(.+?)'"),
     lambda m: ("💬", f"Replied “{m.group(1)}” in {m.group(2)}", "reply", "reacted")),
    (_re.compile(r"^reacted (\S+) to reel \S+ in '(.+?)'"),
     lambda m: (m.group(1), f"Reacted {m.group(1)} in {m.group(2)}", "react", "reacted")),
    (_re.compile(r"LLM (?:synthesised|rescued) reel \S+ -> '(.*?)'"),
     lambda m: ("🧠", f"AI composed “{m.group(1)}”", "ai", None)),
    (_re.compile(r"tone guard rejected"),
     lambda m: ("🛡️", "Tone guard held a reply back — handing to you", "guard", None)),
    (_re.compile(r"^chat '(.+?)': processed (\d+) reel"),
     lambda m: ("✅", f"Finished {m.group(1)} · {m.group(2)} reel(s) read", "done", "chat")),
    (_re.compile(r"^SEND BLOCKED"),
     lambda m: ("🛑", "Send blocked — chat not whitelisted", "guard", None)),
    (_re.compile(r"^stop button pressed"),
     lambda m: ("⏹️", "Stop pressed — winding down", "guard", None)),
    (_re.compile(r"new reels are over; stopping the sweep"),
     lambda m: ("🔚", "Reached reels you've already seen — moving on", "done", None)),
    (_re.compile(r"^incoming-text scan: ([1-9]\d*) message"),
     lambda m: ("✉️", f"Flagged {m.group(1)} text message(s) for you", "guard", None)),
]


def _narrate(msg: str) -> None:
    """Turn a runner/backend log line into a live event + update counters
    (assumes _LOCK held). Unmatched lines are ignored for the feed."""
    for rx, build in _EVENT_RULES:
        m = rx.search(msg)
        if not m:
            continue
        icon, text, kind, stat = build(m)
        _emit_event(icon, text, kind)
        if stat == "reacted":
            _STATE["stats"]["reacted"] += 1
        elif stat == "chat":
            _STATE["stats"]["chats"] += 1
        return


def _review() -> ReviewStore:
    """Fresh view of the persisted store (cheap; picks up background writes)."""
    return ReviewStore(_REVIEW_PATH)


_FLAG_LABEL = {
    "context_text": "Has a text before it (maybe an inside joke)",
    "too_few_comments": "Too few comments to judge the crowd",
    "no_consensus": "Crowd was split — no clear reaction",
    "unable_to_read": "Couldn't read the comments",
    "low_confidence": "Not confident enough to react",
    "nav_failed": "Something went wrong opening/sending",
    "incoming_text": "A text message you should answer yourself",
    "llm_declined": "No safe short reaction fit — reply yourself",
    "sensitive_content": "Looks sensitive/harmful — reply yourself",
    "user_active": "You stopped the bot",
    "unknown": "Needs a look",
}


class _ListLogHandler(logging.Handler):
    """Tee runner log lines into _STATE['log'] so the UI can show live progress."""
    def emit(self, record):
        try:
            msg = record.getMessage()
            with _LOCK:
                _STATE["log"].append(msg)
                _STATE["log"] = _STATE["log"][-250:]
                _narrate(msg)
        except Exception:
            pass


def _run_job(chat_names, dry_run, target_direction, restrict_send, profile_dir):
    from ..browser.backend import BrowserBackend
    handler = _ListLogHandler()
    root = logging.getLogger("insta_reactor")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    cfg = load_config(_CONFIG_PATH) if config_exists(_CONFIG_PATH) else AppConfig()
    scoped = AppConfig(profile=cfg.profile, settings=cfg.settings,
                       enabled_chats=chat_names, ntfy_topic=cfg.ntfy_topic)
    whitelist = set(chat_names) if restrict_send else None

    def _should_stop() -> bool:
        with _LOCK:
            return bool(_STATE.get("stop_requested"))

    with _LOCK:
        backend = _LIVE_BACKENDS.pop(profile_dir, None)
    if backend is not None:
        # Reuse the still-open browser from a prior "please log in" run
        # rather than launching a second Chromium on the same profile dir.
        backend.config = scoped
        backend.send_whitelist = whitelist
        backend.target_direction = target_direction
        backend.thumbs_dir = _THUMBS_DIR
        backend.live_frame_path = _LIVE_FRAME
        backend.should_stop = _should_stop
        backend.stop_reason = None
    else:
        backend = BrowserBackend(scoped, send_whitelist=whitelist,
                                 target_direction=target_direction,
                                 thumbs_dir=_THUMBS_DIR,
                                 profile_dir=profile_dir,
                                 live_frame_path=_LIVE_FRAME,
                                 should_stop=_should_stop)
    try:
        runner = Runner(backend, scoped, FlagManager(), send=not dry_run,
                        seen_store=SeenStore(), should_stop=_should_stop)
        summary = runner.run()
        stop_reason = getattr(backend, "stop_reason", None) or (
            "You stopped the bot." if _should_stop() else "")
        try:
            backend.close()
        except Exception:
            pass
        # Persist the chat-centric report so the panel shows it (now and later).
        try:
            ReviewStore(_REVIEW_PATH).record_run(summary, chat_names)
        except Exception:
            log.exception("recording review store failed")
        with _LOCK:
            _STATE.update(status="done",
                          message=(stop_reason or "Run complete."),
                          stop_reason=stop_reason, finished_at=time.time())
    except Exception as exc:
        log.exception("browser run failed")
        if "not logged in" in str(exc):
            # Deliberately leave the window open — the human logs in there —
            # and cache the live backend so the next Run reuses this same
            # browser instead of colliding with it.
            with _LOCK:
                _LIVE_BACKENDS[profile_dir] = backend
        else:
            try:
                backend.close()
            except Exception:
                pass
        with _LOCK:
            _STATE.update(status="error", message=str(exc),
                          finished_at=time.time())
    finally:
        root.removeHandler(handler)


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------

def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _position_label(pos: int) -> str:
    """Human 'where to look' string — the whole point of the review view."""
    if not pos or pos < 1:
        return "position unknown — scroll up to find it"
    if pos == 1:
        return "the most recent reel (1st from the bottom)"
    return f"{_ordinal(pos)} reel from the bottom"


def _ago(ts: float) -> str:
    if not ts:
        return ""
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hr ago"
    return f"{hrs // 24} d ago"


# A real reel's id IS its Instagram shortcode, so we can deep-link straight to
# it — no counting. Placeholder ids ("text", "reel#3", "") have no link.
_SHORTCODE_RE = _re.compile(r"^[A-Za-z0-9_-]{5,}$")


def _reel_url(reel_id: str) -> str | None:
    rid = (reel_id or "").strip()
    if not rid or rid.startswith("reel#") or rid in ("text", "reel", "?"):
        return None
    if not _SHORTCODE_RE.match(rid):
        return None
    return f"https://www.instagram.com/reel/{rid}/"


def _thumb_tag(name: str, alt: str, href: str | None = None) -> str:
    if name:
        img = f'<img src="/thumb/{_esc(name)}" alt="{_esc(alt)}" loading="lazy">'
    else:
        img = '<div class="noimg">no preview</div>'
    if href:
        # Clicking the picture opens the exact reel on Instagram in a new tab.
        return (f'<a class="thumb-link" href="{_esc(href)}" target="_blank" '
                f'rel="noopener" title="Open this reel on Instagram">{img}</a>')
    return img


def _flag_card(chat_name: str, item) -> str:
    label = _FLAG_LABEL.get(item.kind, _FLAG_LABEL["unknown"])
    url = _reel_url(item.reel_id)
    # For an incoming text the reason IS the message; show it as a quote.
    detail = ""
    if item.kind == "incoming_text" and item.reason:
        detail = f'<div class="quote">“{_esc(item.reason)}”</div>'
    elif item.reason and item.reason != label:
        detail = f'<div class="reason">{_esc(item.reason)}</div>'
    # Primary way to find the reel: open it directly. Only when we have no stable
    # link (e.g. a reel that failed to open, or an incoming text) do we fall back
    # to "Nth from the bottom" so you can still locate it by scrolling.
    if url:
        locate = (f'<a class="open-reel" href="{_esc(url)}" target="_blank" '
                  f'rel="noopener">▶ Open this reel</a>')
    else:
        locate = f'<div class="where">📍 {_esc(_position_label(item.position_from_bottom))}</div>'
    resolve = (
        f'<form method="post" action="/resolve" class="resolve">'
        f'<input type="hidden" name="chat" value="{_esc(chat_name)}">'
        f'<input type="hidden" name="key" value="{_esc(item.key)}">'
        f'<button type="submit">Mark handled ✓</button></form>')
    return (
        f'<div class="card flag">'
        f'<div class="thumb">{_thumb_tag(item.thumbnail, "flagged reel", url)}</div>'
        f'<div class="body">'
        f'<div class="why">{_esc(label)}</div>'
        f'{detail}{locate}{resolve}</div></div>')


def _reacted_card(item) -> str:
    meta = f'{item.emoji} {item.confidence:.0%}'.strip()
    return (
        f'<div class="card ok">'
        f'<div class="thumb">{_thumb_tag(item.thumbnail, "reacted reel")}</div>'
        f'<div class="body">'
        f'<div class="reply">{_esc(item.reply_text)}</div>'
        f'<div class="meta">{_esc(meta)}</div></div></div>')


def _chat_block(rep) -> str:
    pending = rep.pending()
    n_flag = len(pending)
    n_react = len(rep.reacted)
    if n_flag:
        badge = f'<span class="badge need">🙋 {n_flag} need you</span>'
    else:
        badge = '<span class="badge clear">✓ all clear</span>'
    react_note = (f'<span class="badge did">✅ {n_react} reacted</span>'
                  if n_react else "")
    when = _ago(rep.last_run_at)
    when_html = f'<span class="when">{_esc(when)}</span>' if when else ""

    flag_cards = "".join(_flag_card(rep.chat_name, it) for it in pending)
    flags_html = (f'<div class="section-label">Reels that need you</div>'
                  f'<div class="gallery">{flag_cards}</div>') if pending else ""

    react_cards = "".join(_reacted_card(it) for it in rep.reacted)
    react_html = (f'<details class="reacted"><summary>Show {n_react} the bot '
                  f'handled for you</summary><div class="gallery">'
                  f'{react_cards}</div></details>') if rep.reacted else ""

    if not pending and not rep.reacted:
        body = '<p class="empty">Nothing to show — the bot found no new reels here.</p>'
    else:
        body = flags_html + react_html

    # Chats needing attention start expanded; all-clear chats stay collapsed.
    open_attr = " open" if n_flag else ""
    return (
        f'<details class="chat{" attention" if n_flag else ""}"{open_attr}>'
        f'<summary><span class="chat-name">{_esc(rep.chat_name)}</span>'
        f'{badge}{react_note}{when_html}</summary>'
        f'<div class="chat-body">{body}</div></details>')


def _chats_html() -> str:
    store = _review()
    reports = store.ordered_chats()
    if not reports:
        return ('<div class="hint"><p>No chats gone over yet.</p>'
                '<p class="sub">Pick your chats below and hit Run — the bot will '
                'read each one and only surface the reels that need you.</p></div>')
    total = store.total_pending()
    head = (f'<p class="overview">{len(reports)} chat(s) gone over · '
            f'<b>{total}</b> reel(s) need you.</p>')
    return head + "".join(_chat_block(r) for r in reports)


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

# Presenter console. Concept: mission control for an autonomous social agent —
# a live "stage" (the bot's actual browser) narrated by a flight-log feed, over
# a deep neutral with the Instagram gradient used only on the signature marks.
# CSS/JS are raw strings (no str.format) so there's no brace-escaping to babysit.
_CSS = r"""
  :root{
    color-scheme:dark;
    --bg:#0e0d13; --panel:#17151f; --panel2:#1d1a26; --line:#2b2836;
    --ink:#efecf6; --muted:#9c96ac; --faint:#6f6980;
    --ok:#57d9a3; --attn:#f2b35e; --err:#ff8a8a; --link:#b9a6ff;
    --ig:linear-gradient(120deg,#f9ce34 0%,#ee2a7b 52%,#6228d7 100%);
    --ui:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
    --mono:ui-monospace,"Cascadia Code","Cascadia Mono",Consolas,"SF Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{font-family:var(--ui);margin:0;background:var(--bg);color:var(--ink);
    -webkit-font-smoothing:antialiased}
  a{color:var(--link)}
  .wrap{max-width:1180px;margin:0 auto;padding:22px}

  /* top bar */
  header{display:flex;align-items:center;gap:14px;padding:16px 22px;
    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;
    background:rgba(14,13,19,.86);backdrop-filter:blur(8px)}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;
    letter-spacing:.14em;font-size:.92rem;text-transform:uppercase}
  .brand .dot{width:13px;height:13px;border-radius:50%;background:var(--ig);
    box-shadow:0 0 0 3px rgba(238,42,123,.14)}
  .brand small{color:var(--faint);font-weight:500;letter-spacing:.04em;
    text-transform:none}
  .pill{font-family:var(--mono);font-size:.74rem;letter-spacing:.02em;
    padding:4px 11px;border-radius:999px;border:1px solid var(--line);
    background:var(--panel2);color:var(--muted)}
  .pill.status.running{color:#ffd86b;border-color:#5a4a12;background:#241d07}
  .pill.status.done{color:var(--ok);border-color:#1f5a3c;background:#0d2b1e}
  .pill.status.error{color:var(--err);border-color:#5a1f1f;background:#2b0e0e}
  .pill.status .live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;
    background:currentColor;margin-right:6px;vertical-align:middle}
  .spacer{margin-left:auto}
  .ai{font-family:var(--mono);font-size:.74rem;color:var(--muted)}
  .ai b{color:#c9b8ff;font-weight:600}
  /* kill switch — always reachable while a run is live */
  .killbtn{display:none;align-items:center;gap:6px;font-family:var(--ui);
    font-size:.82rem;font-weight:700;letter-spacing:.02em;cursor:pointer;
    color:#fff;background:linear-gradient(120deg,#ff5b6b,#d61f3a);
    border:1px solid #ff8a8a;padding:8px 15px;border-radius:9px;
    box-shadow:0 6px 18px -8px rgba(214,31,58,.7)}
  .killbtn:hover{filter:brightness(1.07)}
  .killbtn:disabled{opacity:.6;cursor:default;filter:none}
  body.is-running .killbtn{display:inline-flex}
  /* stopped/attention banner */
  .banner{display:flex;align-items:flex-start;gap:12px;margin:18px 0 4px;
    padding:14px 16px;border-radius:12px;border:1px solid #5a4412;
    background:#241d07;color:#ffd489}
  .banner .bi{font-size:1.2rem;line-height:1.2}
  .banner b{color:#ffe6b0}

  /* stage: live monitor + flight log */
  .stage{display:grid;grid-template-columns:1.55fr 1fr;gap:18px;margin-top:22px}
  @media(max-width:900px){.stage{grid-template-columns:1fr}}
  .monitor{background:#000;border:1px solid var(--line);border-radius:16px;
    overflow:hidden;position:relative;aspect-ratio:16/10;
    box-shadow:0 24px 60px -30px rgba(0,0,0,.8)}
  .monitor::before{content:"";position:absolute;inset-inline:0;top:0;height:3px;
    background:var(--ig);z-index:3}
  .monitor img{width:100%;height:100%;object-fit:cover;object-position:top center;
    display:none}
  .stage.has-frame .monitor img{display:block}
  .stage.has-frame .mon-empty{display:none}
  .mon-empty{position:absolute;inset:0;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:10px;text-align:center;padding:24px;
    color:var(--muted);background:
      radial-gradient(120% 90% at 50% 0%,#1a1826 0%,#0b0a10 70%)}
  .mon-empty .big{font-size:1.05rem;color:var(--ink);font-weight:600}
  .mon-empty .sub{font-size:.85rem;max-width:34ch;color:var(--muted)}
  .live-tag{position:absolute;top:12px;left:12px;z-index:4;display:none;
    align-items:center;gap:6px;font-family:var(--mono);font-size:.68rem;
    letter-spacing:.14em;color:#fff;background:rgba(0,0,0,.55);
    border:1px solid rgba(255,255,255,.18);padding:4px 9px;border-radius:6px}
  .stage.has-frame.running .live-tag{display:inline-flex}
  .live-tag i{width:7px;height:7px;border-radius:50%;background:#ff3b6b}
  .mon-corner{position:absolute;width:14px;height:14px;border:2px solid rgba(255,255,255,.12);z-index:3}
  .mon-corner.tl{top:8px;left:8px;border-right:0;border-bottom:0}
  .mon-corner.tr{top:8px;right:8px;border-left:0;border-bottom:0}
  .mon-corner.bl{bottom:8px;left:8px;border-right:0;border-top:0}
  .mon-corner.br{bottom:8px;right:8px;border-left:0;border-top:0}

  /* flight log side */
  .side{display:flex;flex-direction:column;gap:14px;min-height:0}
  .counters{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:12px 14px}
  .tile .v{font-family:var(--mono);font-size:1.7rem;font-weight:600;
    font-variant-numeric:tabular-nums;line-height:1;background:var(--ig);
    -webkit-background-clip:text;background-clip:text;color:transparent}
  .tile .k{margin-top:6px;font-size:.68rem;letter-spacing:.1em;
    text-transform:uppercase;color:var(--faint)}
  .feed-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    display:flex;flex-direction:column;overflow:hidden;flex:1;min-height:260px}
  .feed-card h3{margin:0;padding:12px 15px;font-size:.72rem;letter-spacing:.12em;
    text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:8px}
  .feed{list-style:none;margin:0;padding:8px 6px;overflow:auto;flex:1;
    font-family:var(--mono);font-size:.8rem;scroll-behavior:smooth}
  .feed:empty::after{content:"Waiting for the bot to start…";color:var(--faint);
    display:block;padding:14px}
  .ev{display:grid;grid-template-columns:auto auto 1fr;gap:8px;align-items:baseline;
    padding:5px 9px;border-radius:7px}
  .ev+.ev{margin-top:1px}
  .ev-t{color:var(--faint);font-size:.72rem}
  .ev-i{font-family:var(--ui)}
  .ev-x{color:var(--ink);word-break:break-word}
  .ev-react .ev-x,.ev-reply .ev-x{color:#efe4ff}
  .ev-react{background:rgba(238,42,123,.08)}
  .ev-reply{background:rgba(98,40,215,.10)}
  .ev-done .ev-x{color:var(--ok)}
  .ev-guard .ev-x{color:var(--attn)}
  .ev-ai .ev-x{color:#c9b8ff}
  @media(prefers-reduced-motion:no-preference){
    .ev{animation:evin .28s ease}
    @keyframes evin{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
    .live-tag i{animation:blink 1.1s steps(2,start) infinite}
    @keyframes blink{50%{opacity:.25}}
    .pill.status.running .live-dot{animation:blink 1.1s steps(2,start) infinite}
  }

  /* section headings */
  .lede{margin:30px 0 6px;font-size:1.15rem;font-weight:700;letter-spacing:-.01em}
  .lede .accent{background:var(--ig);-webkit-background-clip:text;background-clip:text;
    color:transparent}
  .overview{color:var(--muted);font-size:.92rem;margin:2px 0 4px}

  /* review (kept, restyled) */
  details.chat{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    margin:12px 0;overflow:hidden}
  details.chat.attention{border-color:#5a4412}
  details.chat>summary{cursor:pointer;list-style:none;padding:16px 18px;display:flex;
    align-items:center;gap:12px}
  details.chat>summary::-webkit-details-marker{display:none}
  details.chat>summary::before{content:"▸";color:var(--faint);transition:transform .15s}
  details.chat[open]>summary::before{transform:rotate(90deg)}
  .chat-name{font-size:1.05rem;font-weight:600}
  .badge{font-size:.74rem;padding:3px 10px;border-radius:999px}
  .badge.need{background:#4a3712;color:#ffcf7a;font-weight:600}
  .badge.clear{background:#123521;color:#7fe0a0}
  .badge.did{background:#221b40;color:#c9b8ff}
  .when{margin-left:auto;color:var(--faint);font-size:.76rem}
  .chat-body{padding:4px 18px 18px}
  .section-label{color:var(--attn);font-size:.72rem;margin:6px 0 10px;
    text-transform:uppercase;letter-spacing:.1em}
  .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
  .card{background:#100e16;border:1px solid var(--line);border-radius:12px;
    overflow:hidden;display:flex;flex-direction:column}
  .card .thumb img,.card .noimg{width:100%;height:220px;object-fit:cover;display:block;
    background:#000}
  .card .thumb-link{display:block;position:relative}
  .card .thumb-link::after{content:"▶";position:absolute;inset:0;display:flex;
    align-items:center;justify-content:center;color:#fff;font-size:2.2rem;
    text-shadow:0 2px 8px rgba(0,0,0,.7);opacity:.85;pointer-events:none}
  .card .thumb-link:hover::after{opacity:1}
  .card .noimg{display:flex;align-items:center;justify-content:center;color:var(--faint)}
  .card .body{padding:10px 12px 12px}
  .card.flag{border-color:#5a4412}
  .open-reel{display:inline-block;margin-top:8px;color:var(--link);font-size:.85rem;
    font-weight:600;text-decoration:none}
  .open-reel:hover{text-decoration:underline}
  .card .where{margin-top:8px;font-size:.9rem;font-weight:600;color:var(--attn)}
  .card .why{font-size:.86rem;color:var(--ink);font-weight:600}
  .card .reason{margin-top:4px;font-size:.76rem;color:var(--muted)}
  .card .quote{margin-top:6px;font-size:.85rem;color:#ddd;font-style:italic;
    border-left:2px solid var(--line);padding-left:8px}
  .resolve{margin-top:10px}
  .resolve button{background:#173524;color:#9be5b0;border:1px solid #285a34;
    padding:6px 12px;border-radius:8px;font-size:.8rem;cursor:pointer}
  .resolve button:hover{background:#285a34;color:#fff}
  .card.ok .reply{font-size:1.05rem}
  .card.ok .meta{margin-top:3px;color:var(--faint);font-size:.75rem}
  details.reacted{margin-top:14px}
  details.reacted>summary{cursor:pointer;color:var(--link);font-size:.85rem}
  details.reacted>.gallery{margin-top:12px}
  .empty,.hint p{color:var(--muted)}
  .hint{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px}
  .sub{color:var(--muted);font-size:.85rem}

  /* control deck */
  details.deck{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    margin-top:26px;overflow:hidden}
  details.deck>summary{cursor:pointer;list-style:none;padding:16px 18px;font-weight:600;
    display:flex;align-items:center;gap:10px}
  details.deck>summary::-webkit-details-marker{display:none}
  details.deck>summary::before{content:"⚙";color:var(--muted)}
  .deck-body{padding:0 18px 18px}
  .chats label{display:flex;align-items:center;gap:8px;padding:7px 4px;
    border-bottom:1px solid var(--line)}
  .row{display:flex;gap:8px;margin-top:12px}
  .row input{flex:1;padding:9px;border-radius:8px;border:1px solid var(--line);
    background:var(--bg);color:var(--ink)}
  .row button{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    padding:9px 14px;border-radius:8px;cursor:pointer}
  label.field{display:flex;align-items:center;gap:8px;margin:14px 0;font-size:.92rem}
  select{background:var(--bg);color:var(--ink);border:1px solid var(--line);
    border-radius:8px;padding:7px}
  details.adv{margin:8px 0 4px;border-top:1px solid var(--line);padding-top:10px}
  details.adv>summary{cursor:pointer;color:var(--muted);font-size:.85rem;list-style:none}
  details.adv>summary::-webkit-details-marker{display:none}
  details.adv>summary::before{content:"▸ ";color:var(--faint)}
  details.adv[open]>summary::before{content:"▾ "}
  .opts{display:flex;flex-wrap:wrap;gap:16px;margin:12px 0;font-size:.88rem;color:var(--muted)}
  .opts label{display:flex;align-items:center;gap:6px}
  button.primary{margin-top:14px;background:var(--ig);color:#fff;border:none;
    padding:13px 22px;border-radius:10px;font-size:1rem;font-weight:600;cursor:pointer;
    box-shadow:0 8px 24px -10px rgba(238,42,123,.6)}
  button.primary:hover{filter:brightness(1.06)}
  button:disabled{opacity:.5;cursor:default;filter:none}
  pre.log{background:#08070c;border:1px solid var(--line);border-radius:10px;padding:12px;
    max-height:200px;overflow:auto;font-size:.72rem;color:#8fd7b8;white-space:pre-wrap;
    font-family:var(--mono);margin-top:18px}
"""

_JS = r"""
  const S = window.__reactor;
  const $ = (id) => document.getElementById(id);
  function clock(s){const m=Math.floor(s/60),ss=String(Math.max(0,s%60)).padStart(2,'0');return m+':'+ss;}
  async function stopBot(){
    const b=$('stopbtn'); if(b){b.disabled=true; b.textContent='⏹ Stopping…';}
    try{await fetch('/stop',{method:'POST',headers:{'X-Requested-With':'fetch'}});}
    catch(e){}
  }
  function addEvents(evs){
    const feed=$('feed'); if(!feed) return;
    for(const e of evs){
      const li=document.createElement('li'); li.className='ev ev-'+(e.kind||'info');
      const t=document.createElement('span'); t.className='ev-t'; t.textContent=e.t;
      const i=document.createElement('span'); i.className='ev-i'; i.textContent=e.icon;
      const x=document.createElement('span'); x.className='ev-x'; x.textContent=e.text;
      li.append(t,i,x); feed.appendChild(li);
    }
    feed.scrollTop=feed.scrollHeight;
  }
  async function poll(){
    try{
      const r=await fetch('/status?since='+S.seq); const s=await r.json();
      const el=$('status');
      el.className='pill status '+s.status;
      el.innerHTML=(s.status==='running'?'<span class="live-dot"></span>':'')+s.status_text;
      if(s.events&&s.events.length){addEvents(s.events); S.seq=s.seq;}
      if(s.stats){$('c-chats').textContent=s.stats.chats; $('c-react').textContent=s.stats.reacted;}
      $('c-time').textContent=clock(s.elapsed||0);
      if(s.frame_v && s.frame_v!==S.frameV){
        S.frameV=s.frame_v; const img=$('live'); if(img){img.src='/live.png?v='+s.frame_v;}
        $('stage').classList.add('has-frame');
      }
      const stage=$('stage');
      if(s.status==='running'){stage.classList.add('running'); document.body.classList.add('is-running'); const b=$('runbtn'); if(b)b.disabled=true; setTimeout(poll,1200);}
      else if(s.reload){location.reload();}
      else{stage.classList.remove('running'); document.body.classList.remove('is-running');}
    }catch(e){setTimeout(poll,2500);}
  }
  if(S.poll) poll();
"""


def _stage_placeholder(status: str) -> str:
    if status == "running":
        return ('<div class="big">Waking the browser…</div>'
                '<div class="sub">The live view appears the moment the bot opens '
                'Instagram.</div>')
    if status == "error":
        return ('<div class="big">The run stopped early</div>'
                '<div class="sub">See the activity log below for what happened, '
                'then run again.</div>')
    return ('<div class="big">Ready when you are</div>'
            '<div class="sub">Pick an account and chats in the control deck, then '
            'press Run — you\'ll watch the bot work right here.</div>')


def _feed_items(events) -> str:
    out = []
    for e in events:
        out.append(
            f'<li class="ev ev-{_esc(e["kind"])}">'
            f'<span class="ev-t">{_esc(e["t"])}</span>'
            f'<span class="ev-i">{_esc(e["icon"])}</span>'
            f'<span class="ev-x">{_esc(e["text"])}</span></li>')
    return "".join(out)


def _render() -> str:
    cfg = load_config(_CONFIG_PATH) if config_exists(_CONFIG_PATH) else AppConfig()
    with _LOCK:
        status = _STATE["status"]
        message = _STATE["message"]
        logs = list(_STATE["log"])
        events = list(_STATE["events"])
        stats = dict(_STATE["stats"])
        seq = _STATE["event_seq"]
        started = _STATE.get("started_at")
    status_text = {"idle": "Ready", "running": "Running…",
                   "done": "Done", "error": "Error"}.get(status, status)
    if status == "error":
        status_text = "Error: " + (message or "")
    elapsed = 0
    if started:
        end = time.time() if status == "running" else (_STATE.get("finished_at") or started)
        elapsed = int(end - started)
    has_frame = os.path.exists(_LIVE_FRAME)
    try:
        frame_v = int(os.path.getmtime(_LIVE_FRAME)) if has_frame else 0
    except OSError:
        frame_v = 0

    chat_rows = "".join(
        f'<label><input type="checkbox" name="chat" value="{_esc(c)}"> {_esc(c)}</label>'
        for c in cfg.enabled_chats) or '<p class="empty">No chats yet — add one below.</p>'
    from ..secrets import get_secret
    llm_on = bool(getattr(cfg.settings, "use_llm", False)) and bool(get_secret("openrouter_api_key"))
    llm_status = ("AI synthesis <b>ON</b>" if llm_on
                  else "AI <b>off</b> — replies are rule-based")
    accounts = _list_accounts()
    selected = _STATE.get("account") or (accounts[0] if accounts else "")
    account_options = "".join(
        f'<option value="{_esc(a)}"{" selected" if a == selected else ""}>{_esc(a)}</option>'
        for a in accounts) or '<option value="">No accounts yet — add one below</option>'
    log_block = (f'<pre class="log">{_esc(chr(10).join(logs[-120:]))}</pre>'
                 if logs and status in ("running", "error") else "")

    stage_cls = "stage"
    if has_frame:
        stage_cls += " has-frame"
    if status == "running":
        stage_cls += " running"
    live_img = (f'<img id="live" src="/live.png?v={frame_v}" alt="Live view of the bot">'
                if has_frame else '<img id="live" alt="Live view of the bot">')
    status_inner = (('<span class="live-dot"></span>' if status == "running" else "")
                    + _esc(status_text))
    deck_open = " open" if status != "running" else ""
    boot = ("{seq: %d, frameV: %d, poll: %s}"
            % (seq, frame_v, "true" if status == "running" else "false"))
    body_cls = "is-running" if status == "running" else ""
    # Banner explaining a run that ended because you stepped in / hit Stop.
    stop_reason = _STATE.get("stop_reason") or ""
    banner = (f'<div class="banner"><span class="bi">⏹️</span><div>'
              f'<b>The bot was stopped.</b><br>{_esc(stop_reason)}'
              f'</div></div>') if stop_reason and status == "done" else ""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reactor · live</title>
<style>{_CSS}</style></head>
<body class="{body_cls}">
<header>
  <span class="brand"><span class="dot"></span>Reactor <small>· autonomous reel agent</small></span>
  <span class="pill status {status}" id="status">{status_inner}</span>
  <span class="spacer"></span>
  <span class="ai">{llm_status}</span>
  <button id="stopbtn" class="killbtn" type="button" onclick="stopBot()"
    title="Stop the bot right now">⏹ Stop the bot</button>
</header>
<div class="wrap">
  {banner}

  <section class="{stage_cls}" id="stage">
    <div class="monitor">
      {live_img}
      <div class="mon-empty">{_stage_placeholder(status)}</div>
      <span class="live-tag"><i></i>LIVE</span>
      <span class="mon-corner tl"></span><span class="mon-corner tr"></span>
      <span class="mon-corner bl"></span><span class="mon-corner br"></span>
    </div>
    <aside class="side">
      <div class="counters">
        <div class="tile"><div class="v" id="c-chats">{stats.get("chats", 0)}</div><div class="k">Chats</div></div>
        <div class="tile"><div class="v" id="c-react">{stats.get("reacted", 0)}</div><div class="k">Reactions</div></div>
        <div class="tile"><div class="v" id="c-time">{elapsed // 60}:{elapsed % 60:02d}</div><div class="k">Elapsed</div></div>
      </div>
      <div class="feed-card">
        <h3>Flight log</h3>
        <ol class="feed" id="feed">{_feed_items(events)}</ol>
      </div>
    </aside>
  </section>

  <h2 class="lede">What <span class="accent">needs you</span></h2>
  <section id="chats">{_chats_html()}</section>

  <details class="deck"{deck_open}>
    <summary>Setup &amp; controls</summary>
    <div class="deck-body">
      <form method="post" action="/run">
        <label class="field">Account:
          <select name="account">{account_options}</select>
        </label>
        <div class="chats">{chat_rows}</div>
        <div class="row">
          <input name="new_chat" placeholder="Add a chat by its exact Instagram name…">
          <button formaction="/add_chat" formmethod="post" name="_add" value="1">Add</button>
        </div>
        <div class="row">
          <input name="new_account" placeholder="…or add a new account (opens a fresh login window)">
        </div>
        <details class="adv">
          <summary>Advanced (testing)</summary>
          <div class="opts">
            <label><input type="checkbox" name="dry_run" {"checked" if _STATE["dry_run"] else ""}> Dry run (send nothing)</label>
            <label><input type="checkbox" name="restrict_send" checked> Only send to selected chats</label>
            <label>Target:
              <select name="target_direction">
                <option value="incoming">Incoming reels (real use)</option>
                <option value="any">Any reel (testing)</option>
              </select>
            </label>
          </div>
        </details>
        <button type="submit" class="primary" id="runbtn" {"disabled" if status == "running" else ""}>▶ Run the bot</button>
      </form>
      {log_block}
    </div>
  </details>
</div>
<script>window.__reactor = {boot};</script>
<script>{_JS}</script>
</body></html>"""


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def home():
    return _render()


@app.post("/add_chat")
def add_chat():
    name = (request.form.get("new_chat") or "").strip()
    if name:
        cfg = load_config(_CONFIG_PATH) if config_exists(_CONFIG_PATH) else AppConfig()
        if name not in cfg.enabled_chats:
            cfg.enabled_chats.append(name)
            save_config(cfg, _CONFIG_PATH)
    return redirect("/")


@app.post("/run")
def run():
    with _LOCK:
        if _STATE["status"] == "running":
            return redirect("/")
    chats = request.form.getlist("chat")
    dry_run = request.form.get("dry_run") is not None
    restrict = request.form.get("restrict_send") is not None
    target = request.form.get("target_direction", "incoming")
    account = ((request.form.get("new_account") or "").strip()
               or (request.form.get("account") or "").strip())
    if not chats:
        with _LOCK:
            _STATE.update(status="error", message="No chats selected.")
        return redirect("/")
    if not account:
        with _LOCK:
            _STATE.update(status="error", message="No account selected.")
        return redirect("/")
    # Drop the previous run's live frame so the stage doesn't flash a stale shot.
    try:
        if os.path.exists(_LIVE_FRAME):
            os.remove(_LIVE_FRAME)
    except Exception:
        pass
    with _LOCK:
        _STATE.update(status="running", message="", log=[], events=[],
                      stats={"chats": 0, "reacted": 0, "read": 0},
                      dry_run=dry_run, started_at=time.time(),
                      finished_at=None, account=account,
                      stop_requested=False, stop_reason="")
    threading.Thread(target=_run_job,
                     args=(chats, dry_run, target, restrict,
                           _account_profile_dir(account)),
                     daemon=True).start()
    return redirect("/")


@app.post("/stop")
def stop():
    """Kill switch. Sets the flag the run's should_stop callable reads; the
    runner/backend then abort at the next reel boundary and the browser closes
    cleanly (no hard kill mid-send). Idempotent — safe to hit repeatedly."""
    with _LOCK:
        if _STATE["status"] == "running":
            _STATE["stop_requested"] = True
            _emit_event("⏹️", "Stop requested — finishing the current step and "
                        "shutting down…", "guard")
    return ("", 204) if request.headers.get(
        "X-Requested-With") == "fetch" else redirect("/")


@app.post("/resolve")
def resolve():
    chat = (request.form.get("chat") or "").strip()
    key = (request.form.get("key") or "").strip()
    if chat and key:
        _review().resolve(chat, key)
    return redirect("/")


@app.get("/status")
def status():
    since = request.args.get("since", type=int) or 0
    with _LOCK:
        s = _STATE["status"]
        msg = _STATE["message"]
        stats = dict(_STATE["stats"])
        started = _STATE.get("started_at")
        new_events = [e for e in _STATE["events"] if e["id"] > since]
        seq = _STATE["event_seq"]
    status_text = {"idle": "Ready", "running": "Running…",
                   "done": "Done", "error": "Error: " + (msg or "")}.get(s, s)
    elapsed = int(time.time() - started) if started and s == "running" else (
        int((_STATE.get("finished_at") or 0) - started) if started else 0)
    # frame_v: mtime of the live PNG, so the client only refetches a new frame.
    try:
        frame_v = int(os.path.getmtime(_LIVE_FRAME))
    except OSError:
        frame_v = 0
    # Tell the poller to reload the page once a run finishes (to show results).
    return jsonify(status=s, status_text=status_text,
                   reload=s in ("done", "error"),
                   events=new_events, seq=seq, stats=stats,
                   elapsed=elapsed, frame_v=frame_v)


@app.get("/live.png")
def live():
    if not os.path.exists(_LIVE_FRAME):
        abort(404)
    resp = send_file(_LIVE_FRAME, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/thumb/<path:name>")
def thumb(name: str):
    # Only serve files from the thumbs dir (no traversal).
    safe = os.path.basename(name)
    full = os.path.abspath(os.path.join(_THUMBS_DIR, safe))
    if not full.startswith(os.path.abspath(_THUMBS_DIR)) or not os.path.exists(full):
        abort(404)
    return send_file(full, mimetype="image/png")


def run_server(config_path: str = DEFAULT_CONFIG_PATH,
               thumbs_dir: str = _THUMBS_DIR, port: int = 8770,
               review_path: str = DEFAULT_REVIEW_PATH) -> None:
    global _CONFIG_PATH, _THUMBS_DIR, _REVIEW_PATH
    _CONFIG_PATH = config_path
    _THUMBS_DIR = thumbs_dir
    _REVIEW_PATH = review_path
    os.makedirs(_THUMBS_DIR, exist_ok=True)
    log.info("Insta Reactor (browser) UI on http://127.0.0.1:%d/", port)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
