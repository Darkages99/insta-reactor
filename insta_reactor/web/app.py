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
import threading
import time

from flask import Flask, request, redirect, send_file, jsonify, abort

from ..config import (load_config, save_config, config_exists, AppConfig,
                      DEFAULT_CONFIG_PATH)
from ..flags import FlagManager
from ..seen_store import SeenStore
from ..runner import Runner
from ..review_store import ReviewStore, DEFAULT_REVIEW_PATH

log = logging.getLogger("insta_reactor.web")

app = Flask(__name__)

_CONFIG_PATH = DEFAULT_CONFIG_PATH
_THUMBS_DIR = os.path.join("data", "thumbs")
_REVIEW_PATH = DEFAULT_REVIEW_PATH
# Each Instagram account gets its own Chromium user-data dir under here, named
# after the account itself (e.g. "sarangdrajgopaul", "Event Forge"), so each
# login is fully isolated and switching accounts never logs another one out.
_PROFILES_BASE = os.path.join("data", "browser_profiles")


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
    "dry_run": True,
    "finished_at": None,
    "account": "",
}


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
    "unknown": "Needs a look",
}


class _ListLogHandler(logging.Handler):
    """Tee runner log lines into _STATE['log'] so the UI can show live progress."""
    def emit(self, record):
        try:
            with _LOCK:
                _STATE["log"].append(record.getMessage())
                _STATE["log"] = _STATE["log"][-250:]
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
    with _LOCK:
        backend = _LIVE_BACKENDS.pop(profile_dir, None)
    if backend is not None:
        # Reuse the still-open browser from a prior "please log in" run
        # rather than launching a second Chromium on the same profile dir.
        backend.config = scoped
        backend.send_whitelist = whitelist
        backend.target_direction = target_direction
        backend.thumbs_dir = _THUMBS_DIR
    else:
        backend = BrowserBackend(scoped, send_whitelist=whitelist,
                                 target_direction=target_direction,
                                 thumbs_dir=_THUMBS_DIR,
                                 profile_dir=profile_dir)
    try:
        runner = Runner(backend, scoped, FlagManager(), send=not dry_run,
                        seen_store=SeenStore())
        summary = runner.run()
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
                          message="Run complete.", finished_at=time.time())
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


import re as _re
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

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insta Reactor</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background:#0c0c0e; color:#eee; }}
  header {{ padding: 16px 22px; border-bottom:1px solid #222; display:flex; align-items:center; gap:12px; }}
  header h1 {{ font-size:1.15rem; margin:0; }}
  .status {{ font-size:.8rem; padding:3px 10px; border-radius:20px; background:#222; }}
  .status.running {{ background:#3a2d00; color:#ffd54a; }}
  .status.done {{ background:#0f3d1e; color:#5be584; }}
  .status.error {{ background:#4d1414; color:#ff8a8a; }}
  main {{ max-width: 1000px; margin: 0 auto; padding: 22px; }}
  .overview {{ color:#bbb; font-size:.95rem; }}
  details.chat {{ background:#141417; border:1px solid #222; border-radius:12px; margin:12px 0; overflow:hidden; }}
  details.chat.attention {{ border-color:#5a3a12; }}
  details.chat > summary {{ cursor:pointer; list-style:none; padding:16px 18px; display:flex; align-items:center; gap:12px; }}
  details.chat > summary::-webkit-details-marker {{ display:none; }}
  details.chat > summary::before {{ content:"▸"; color:#666; transition:transform .15s; }}
  details.chat[open] > summary::before {{ transform:rotate(90deg); }}
  .chat-name {{ font-size:1.05rem; font-weight:600; }}
  .badge {{ font-size:.78rem; padding:3px 10px; border-radius:20px; }}
  .badge.need {{ background:#5a3a12; color:#ffcf7a; font-weight:600; }}
  .badge.clear {{ background:#12351f; color:#7fe0a0; }}
  .badge.did {{ background:#1a2540; color:#9fb6ff; }}
  .when {{ margin-left:auto; color:#777; font-size:.78rem; }}
  .chat-body {{ padding: 4px 18px 18px; }}
  .section-label {{ color:#ffcf7a; font-size:.82rem; margin:6px 0 10px; text-transform:uppercase; letter-spacing:.04em; }}
  .gallery {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:14px; }}
  .card {{ background:#0f0f12; border:1px solid #242428; border-radius:10px; overflow:hidden; display:flex; flex-direction:column; }}
  .card .thumb img, .card .noimg {{ width:100%; height:220px; object-fit:cover; display:block; background:#000; }}
  .card .thumb-link {{ display:block; position:relative; }}
  .card .thumb-link::after {{ content:"▶"; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#fff; font-size:2.2rem; text-shadow:0 2px 8px rgba(0,0,0,.7); opacity:.85; pointer-events:none; }}
  .card .thumb-link:hover::after {{ opacity:1; }}
  .card .noimg {{ display:flex; align-items:center; justify-content:center; color:#555; }}
  .card .body {{ padding:10px 12px 12px; }}
  .card.flag {{ border-color:#5a3a12; }}
  .open-reel {{ display:inline-block; margin-top:8px; color:#9fb6ff; font-size:.85rem; font-weight:600; text-decoration:none; }}
  .open-reel:hover {{ text-decoration:underline; }}
  .card .where {{ margin-top:8px; font-size:.9rem; font-weight:600; color:#ffd98a; }}
  .card .why {{ font-size:.86rem; color:#e6e6e6; font-weight:600; }}
  .card .reason {{ margin-top:4px; font-size:.76rem; color:#999; }}
  .card .quote {{ margin-top:6px; font-size:.85rem; color:#ddd; font-style:italic; border-left:2px solid #444; padding-left:8px; }}
  .resolve {{ margin-top:10px; }}
  .resolve button {{ background:#243a24; color:#9be59b; border:1px solid #2f5a2f; padding:6px 12px; border-radius:7px; font-size:.8rem; cursor:pointer; }}
  .resolve button:hover {{ background:#2f5a2f; color:#fff; }}
  .card.ok .reply {{ font-size:1.05rem; }}
  .card.ok .meta {{ margin-top:3px; color:#888; font-size:.75rem; }}
  details.reacted {{ margin-top:14px; }}
  details.reacted > summary {{ cursor:pointer; color:#9fb6ff; font-size:.85rem; }}
  details.reacted > .gallery {{ margin-top:12px; }}
  .empty, .hint p {{ color:#888; }}
  .hint {{ background:#141417; border:1px solid #222; border-radius:12px; padding:22px; }}
  .sub {{ color:#999; font-size:.85rem; }}
  form.run {{ background:#141417; border:1px solid #222; border-radius:12px; padding:18px; margin-top:26px; }}
  form.run h2 {{ margin:0 0 12px; font-size:1rem; }}
  .chats label {{ display:flex; align-items:center; gap:8px; padding:7px 4px; border-bottom:1px solid #1e1e22; }}
  .opts {{ display:flex; flex-wrap:wrap; gap:16px; margin:14px 0; font-size:.9rem; }}
  .opts label {{ display:flex; align-items:center; gap:6px; }}
  button.primary {{ background:#4f7cff; color:#fff; border:none; padding:12px 20px; border-radius:8px; font-size:1rem; cursor:pointer; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  .addchat {{ display:flex; gap:8px; margin-top:12px; }}
  .addchat input {{ flex:1; padding:9px; border-radius:6px; border:1px solid #333; background:#0c0c0e; color:#eee; }}
  .addchat button {{ background:#2a2a30; color:#eee; border:1px solid #3a3a42; padding:9px 14px; border-radius:6px; cursor:pointer; }}
  select {{ background:#0c0c0e; color:#eee; border:1px solid #333; border-radius:6px; padding:5px; }}
  pre.log {{ background:#000; border:1px solid #1e1e22; border-radius:8px; padding:12px; max-height:220px; overflow:auto; font-size:.75rem; color:#9fb; white-space:pre-wrap; }}
</style></head>
<body>
<header>
  <h1>📸 Insta Reactor <span style="color:#666;font-weight:400">· review</span></h1>
  <span class="status {status_cls}" id="status">{status_text}</span>
  <span style="margin-left:auto;color:#777;font-size:.8rem">{llm_status}</span>
</header>
<main>
  <section id="chats">{chats}</section>

  <form class="run" method="post" action="/run">
    <h2>Run the bot</h2>
    <div class="chats">{chat_rows}</div>
    <div class="addchat">
      <input name="new_chat" placeholder="Add a chat by its exact Instagram name…">
      <button formaction="/add_chat" formmethod="post" name="_add" value="1">Add</button>
    </div>
    <div class="opts">
      <label>Account:
        <select name="account">{account_options}</select>
      </label>
      <label><input type="checkbox" name="dry_run" {dry_checked}> Dry run (send nothing)</label>
      <label><input type="checkbox" name="restrict_send" checked> Only send to selected chats</label>
      <label>Target:
        <select name="target_direction">
          <option value="incoming">Incoming reels (real use)</option>
          <option value="any">Any reel (testing)</option>
        </select>
      </label>
    </div>
    <div class="addchat">
      <input name="new_account" placeholder="…or add a new account by name (opens a fresh login window)">
    </div>
    <p class="sub">Each account keeps its own separate Chromium login — picking
      one reuses its saved session; typing a new name in the box above starts
      a fresh one.</p>
    <button type="submit" class="primary" id="runbtn" {run_disabled}>▶ Run selected chats</button>
  </form>

  {log_block}
</main>
<script>
async function poll() {{
  try {{
    const r = await fetch('/status'); const s = await r.json();
    const el = document.getElementById('status');
    el.textContent = s.status_text; el.className = 'status ' + s.status;
    if (s.status === 'running') {{
      document.getElementById('runbtn').disabled = true;
      setTimeout(poll, 1500);
    }} else if (s.reload) {{
      location.reload();
    }}
  }} catch (e) {{ setTimeout(poll, 2500); }}
}}
if ({polling}) poll();
</script>
</body></html>
"""


def _render() -> str:
    cfg = load_config(_CONFIG_PATH) if config_exists(_CONFIG_PATH) else AppConfig()
    with _LOCK:
        status = _STATE["status"]
        message = _STATE["message"]
        logs = list(_STATE["log"])
    status_text = {"idle": "Ready", "running": "Running…",
                   "done": "Done ✓", "error": "Error"}.get(status, status)
    if status == "error":
        status_text = "Error: " + (message or "")
    chat_rows = "".join(
        f'<label><input type="checkbox" name="chat" value="{_esc(c)}" checked> {_esc(c)}</label>'
        for c in cfg.enabled_chats) or '<p class="empty">No chats yet — add one below.</p>'
    from ..secrets import get_secret
    llm_on = bool(getattr(cfg.settings, "use_llm", False)) and bool(get_secret("openrouter_api_key"))
    llm_status = ("🧠 AI synthesis ON" if llm_on
                  else "AI off (set use_llm + key for AI replies)")
    log_block = (f'<h2 style="font-size:1rem">Activity</h2><pre class="log">'
                 f'{_esc(chr(10).join(logs[-120:]))}</pre>'
                 if logs and status in ("running", "error") else "")
    accounts = _list_accounts()
    selected = _STATE.get("account") or (accounts[0] if accounts else "")
    account_options = "".join(
        f'<option value="{_esc(a)}"{" selected" if a == selected else ""}>{_esc(a)}</option>'
        for a in accounts) or '<option value="">No accounts yet — add one below</option>'
    return _PAGE.format(
        status_cls=status, status_text=_esc(status_text),
        llm_status=_esc(llm_status),
        chats=_chats_html(),
        chat_rows=chat_rows,
        dry_checked="checked" if _STATE["dry_run"] else "",
        run_disabled="disabled" if status == "running" else "",
        account_options=account_options,
        log_block=log_block,
        polling="true" if status == "running" else "false")


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
    with _LOCK:
        _STATE.update(status="running", message="", log=[],
                      dry_run=dry_run, finished_at=None, account=account)
    threading.Thread(target=_run_job,
                     args=(chats, dry_run, target, restrict,
                           _account_profile_dir(account)),
                     daemon=True).start()
    return redirect("/")


@app.post("/resolve")
def resolve():
    chat = (request.form.get("chat") or "").strip()
    key = (request.form.get("key") or "").strip()
    if chat and key:
        _review().resolve(chat, key)
    return redirect("/")


@app.get("/status")
def status():
    with _LOCK:
        s = _STATE["status"]
        msg = _STATE["message"]
    status_text = {"idle": "Ready", "running": "Running…",
                   "done": "Done ✓", "error": "Error: " + (msg or "")}.get(s, s)
    # Tell the poller to reload the page once a run finishes (to show results).
    return jsonify(status=s, status_text=status_text,
                   reload=s in ("done", "error"))


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
