"""Flask control-panel: pick chats -> Run -> results + un-reacted thumbnail wall.

Design notes:
  * The reactor run happens in a BACKGROUND THREAD (Playwright sync must own its
    thread, and the browser can take a while), while the page polls /status.
  * SAFETY: when "live send" is on, sends are hard-restricted to the selected
    chats (send_whitelist), on top of the runner only visiting those chats.
    Dry-run (send nothing) is the default.
  * Un-reacted reels are shown as a GALLERY of thumbnail screenshots with a
    plain-language reason each, so "which reels need me?" is answered visually.
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
from ..models import Action, CANON_EMOJI

log = logging.getLogger("insta_reactor.web")

app = Flask(__name__)

_CONFIG_PATH = DEFAULT_CONFIG_PATH
_THUMBS_DIR = os.path.join("data", "thumbs")

# Shared run state (single active run at a time — this is a personal tool).
_LOCK = threading.Lock()
_STATE: dict = {
    "status": "idle",          # idle | running | done | error
    "message": "",
    "log": [],
    "summary": None,
    "dry_run": True,
    "finished_at": None,
}

_FLAG_LABEL = {
    "context_text": "Has a text before it (maybe an inside joke)",
    "too_few_comments": "Too few comments to judge the crowd",
    "no_consensus": "Crowd was split — no clear reaction",
    "unable_to_read": "Couldn't read the comments",
    "low_confidence": "Not confident enough to react",
    "nav_failed": "Something went wrong opening/sending",
    "incoming_text": "A text message you should answer yourself",
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


def _run_job(chat_names, dry_run, target_direction, restrict_send):
    from ..browser.backend import BrowserBackend
    handler = _ListLogHandler()
    root = logging.getLogger("insta_reactor")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        cfg = load_config(_CONFIG_PATH) if config_exists(_CONFIG_PATH) else AppConfig()
        scoped = AppConfig(profile=cfg.profile, settings=cfg.settings,
                           enabled_chats=chat_names, ntfy_topic=cfg.ntfy_topic)
        whitelist = set(chat_names) if restrict_send else None
        backend = BrowserBackend(scoped, send_whitelist=whitelist,
                                 target_direction=target_direction,
                                 thumbs_dir=_THUMBS_DIR)
        runner = Runner(backend, scoped, FlagManager(), send=not dry_run,
                        seen_store=SeenStore())
        summary = runner.run()
        try:
            backend.close()
        except Exception:
            pass
        with _LOCK:
            _STATE.update(status="done", summary=summary,
                          message="Run complete.", finished_at=time.time())
    except Exception as exc:
        log.exception("browser run failed")
        with _LOCK:
            _STATE.update(status="error", message=str(exc),
                          finished_at=time.time())
    finally:
        root.removeHandler(handler)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _esc(s: str) -> str:
    import html
    return html.escape(str(s or ""))


def _thumb_url(path: str | None) -> str | None:
    if not path:
        return None
    return "/thumb/" + os.path.basename(path)


def _results_html() -> str:
    summary = _STATE.get("summary")
    if summary is None:
        return ""
    auto = summary.auto_replied
    flagged = summary.flagged

    # Auto-replied cards
    auto_cards = []
    for d in auto:
        thumb = _thumb_url(d.thumbnail_path)
        img = (f'<img src="{thumb}" alt="reel">' if thumb
               else '<div class="noimg">reel</div>')
        emo = CANON_EMOJI.get(d.winning_emotion or "", "")
        src = " · AI" if d.reply_source == "llm" else ""
        auto_cards.append(
            f'<div class="card ok"><div class="thumb">{img}</div>'
            f'<div class="reply">{_esc(d.reply_text)}</div>'
            f'<div class="meta">{emo} {d.confidence:.0%}{src}</div></div>')

    # Un-reacted (flagged) gallery
    flag_cards = []
    for d in flagged:
        thumb = _thumb_url(d.thumbnail_path)
        img = (f'<img src="{thumb}" alt="reel">' if thumb
               else '<div class="noimg">no preview</div>')
        kind = d.flag.kind if d.flag else "?"
        label = _FLAG_LABEL.get(kind, kind)
        reason = _esc(d.flag.reason) if d.flag else ""
        flag_cards.append(
            f'<div class="card flag"><div class="thumb">{img}</div>'
            f'<div class="why">{_esc(label)}</div>'
            f'<div class="chat">{_esc(d.chat_name)}</div></div>')

    return f"""
    <h2>Reacted ✅ <span class="count">{len(auto)}</span></h2>
    <div class="gallery">{''.join(auto_cards) or '<p class="empty">Nothing auto-reacted.</p>'}</div>
    <h2>Needs you 🙋 <span class="count">{len(flagged)}</span></h2>
    <p class="sub">These reels were <b>not</b> reacted to — open Instagram and handle them yourself.</p>
    <div class="gallery">{''.join(flag_cards) or '<p class="empty">Nothing left for you — all clear!</p>'}</div>
    """


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
  form.run {{ background:#141417; border:1px solid #222; border-radius:12px; padding:18px; }}
  .chats label {{ display:flex; align-items:center; gap:8px; padding:7px 4px; border-bottom:1px solid #1e1e22; }}
  .opts {{ display:flex; flex-wrap:wrap; gap:16px; margin:14px 0; font-size:.9rem; }}
  .opts label {{ display:flex; align-items:center; gap:6px; }}
  button {{ background:#4f7cff; color:#fff; border:none; padding:12px 20px; border-radius:8px; font-size:1rem; cursor:pointer; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  .addchat {{ display:flex; gap:8px; margin-top:12px; }}
  .addchat input {{ flex:1; padding:9px; border-radius:6px; border:1px solid #333; background:#0c0c0e; color:#eee; }}
  h2 {{ margin-top:30px; font-size:1.05rem; }}
  h2 .count {{ background:#222; border-radius:20px; padding:1px 10px; font-size:.8rem; vertical-align:middle; }}
  .sub {{ color:#999; font-size:.85rem; margin-top:-4px; }}
  .gallery {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:14px; }}
  .card {{ background:#141417; border:1px solid #222; border-radius:10px; overflow:hidden; }}
  .card .thumb img, .card .noimg {{ width:100%; height:150px; object-fit:cover; display:block; }}
  .card .noimg {{ display:flex; align-items:center; justify-content:center; color:#555; background:#0c0c0e; }}
  .card .reply {{ padding:8px 10px 2px; font-size:1.05rem; }}
  .card .meta {{ padding:0 10px 10px; color:#888; font-size:.75rem; }}
  .card.flag {{ border-color:#5a3a12; }}
  .card .why {{ padding:8px 10px 2px; font-size:.82rem; color:#ffcf7a; }}
  .card .chat {{ padding:0 10px 10px; color:#777; font-size:.72rem; }}
  .empty {{ color:#777; }}
  pre.log {{ background:#000; border:1px solid #1e1e22; border-radius:8px; padding:12px; max-height:220px; overflow:auto; font-size:.75rem; color:#9fb; white-space:pre-wrap; }}
</style></head>
<body>
<header>
  <h1>📸 Insta Reactor <span style="color:#666;font-weight:400">· browser</span></h1>
  <span class="status {status_cls}" id="status">{status_text}</span>
  <span style="margin-left:auto;color:#777;font-size:.8rem">{llm_status}</span>
</header>
<main>
  <form class="run" method="post" action="/run">
    <div class="chats">{chat_rows}</div>
    <div class="addchat">
      <input name="new_chat" placeholder="Add a chat by its exact Instagram name…">
      <button formaction="/add_chat" formmethod="post" name="_add" value="1">Add</button>
    </div>
    <div class="opts">
      <label><input type="checkbox" name="dry_run" {dry_checked}> Dry run (send nothing)</label>
      <label><input type="checkbox" name="restrict_send" checked> Only send to selected chats</label>
      <label>Target:
        <select name="target_direction">
          <option value="incoming">Incoming reels (real use)</option>
          <option value="any">Any reel (testing)</option>
        </select>
      </label>
    </div>
    <button type="submit" id="runbtn" {run_disabled}>▶ Run selected chats</button>
  </form>

  {results}

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


def _render(status_reload: bool = False) -> str:
    cfg = load_config(_CONFIG_PATH) if config_exists(_CONFIG_PATH) else AppConfig()
    with _LOCK:
        status = _STATE["status"]
        message = _STATE["message"]
        logs = list(_STATE["log"])
        has_summary = _STATE["summary"] is not None
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
    results = _results_html() if has_summary else ""
    log_block = (f'<h2>Activity</h2><pre class="log">{_esc(chr(10).join(logs[-120:]))}</pre>'
                 if logs else "")
    return _PAGE.format(
        status_cls=status, status_text=_esc(status_text),
        llm_status=_esc(llm_status),
        chat_rows=chat_rows,
        dry_checked="checked" if _STATE["dry_run"] else "",
        run_disabled="disabled" if status == "running" else "",
        results=results, log_block=log_block,
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
    if not chats:
        with _LOCK:
            _STATE.update(status="error", message="No chats selected.")
        return redirect("/")
    with _LOCK:
        _STATE.update(status="running", message="", log=[], summary=None,
                      dry_run=dry_run, finished_at=None)
    threading.Thread(target=_run_job,
                     args=(chats, dry_run, target, restrict),
                     daemon=True).start()
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
               thumbs_dir: str = _THUMBS_DIR, port: int = 8770) -> None:
    global _CONFIG_PATH, _THUMBS_DIR
    _CONFIG_PATH = config_path
    _THUMBS_DIR = thumbs_dir
    os.makedirs(_THUMBS_DIR, exist_ok=True)
    log.info("Insta Reactor (browser) UI on http://127.0.0.1:%d/", port)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
