"""Minimal phone-facing control surface: pick DM(s), tap Run, get notified.

Deliberately stdlib-only (http.server), matching the project's zero-runtime-
deps philosophy — this is a stopgap control panel to trigger runs from a
phone browser over wifi/Tailscale, not the eventual on-phone port. Point your
phone's browser at http://<this-pc-ip>:<port>/ while both are on the same
network (or connected via Tailscale/similar).

Errors and "unsure" flags (nav failures, unreadable comments, no consensus,
low confidence) are pushed to your phone via ntfy.sh (https://ntfy.sh) if
`ntfy_topic` is set in config.json — install the ntfy app and subscribe to
that topic to get them as real push notifications.
"""

from __future__ import annotations

import html
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .config import AppConfig, load_config, save_config
from .flags import FlagManager
from .notify import notify_from_summary
from .report import summarize
from .runner import Runner
from .seen_store import SeenStore

log = logging.getLogger("insta_reactor.webui")

_PAGE = """<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insta Reactor</title>
<style>
body {{ font-family: sans-serif; margin: 0; padding: 1.2em; background: #111; color: #eee; }}
h1 {{ font-size: 1.3em; }}
label {{ display: block; padding: 0.9em 0.6em; font-size: 1.1em; border-bottom: 1px solid #333; }}
button {{ width: 100%; padding: 1em; font-size: 1.2em; margin-top: 1em;
          background: #2d7; color: #111; border: none; border-radius: 8px; }}
pre {{ white-space: pre-wrap; background: #000; padding: 1em; border-radius: 8px; }}
</style></head>
<body>
<h1>Insta Reactor</h1>
<form method="post" action="/run">
{chat_rows}
<button type="submit">Run selected</button>
</form>
{result}
</body></html>
"""


def _run_selected(config: AppConfig, chat_names: list[str], queue_path: str,
                   seen_path: str):
    from .backends.android import AndroidBackend
    scoped = AppConfig(profile=config.profile, settings=config.settings,
                        enabled_chats=chat_names,
                        device_serial=config.device_serial,
                        ntfy_topic=config.ntfy_topic)
    backend = AndroidBackend(scoped)
    runner = Runner(backend, scoped, FlagManager(queue_path),
                    send=True, seen_store=SeenStore(seen_path))
    summary = runner.run()
    backend.close()
    return summary


class Handler(BaseHTTPRequestHandler):
    config_path: str = ""
    queue_path: str = ""
    seen_path: str = ""

    def _config(self) -> AppConfig:
        return load_config(self.config_path)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib-mandated name)
        if self.path != "/":
            self.send_error(404)
            return
        config = self._config()
        rows = "".join(
            f'<label><input type="checkbox" name="chat" value="{html.escape(c)}" '
            f'checked> {html.escape(c)}</label>'
            for c in config.enabled_chats
        ) or "<p>No chats enabled. Add one with: chats add \"&lt;name&gt;\"</p>"
        self._send_html(_PAGE.format(chat_rows=rows, result=""))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        selected = parse_qs(body).get("chat", [])

        config = self._config()
        rows = "".join(
            f'<label><input type="checkbox" name="chat" value="{html.escape(c)}" '
            f'{"checked" if c in selected else ""}> {html.escape(c)}</label>'
            for c in config.enabled_chats
        )

        if not selected:
            result = "<p>No chats selected.</p>"
        else:
            summary = _run_selected(config, selected, self.queue_path,
                                     self.seen_path)
            result = f"<pre>{html.escape(summarize(summary))}</pre>"
            notify_from_summary(config.ntfy_topic, summary)

        self._send_html(_PAGE.format(chat_rows=rows, result=result))

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        log.info("%s - %s", self.address_string(), fmt % args)


def run_server(config_path: str, queue_path: str, seen_path: str,
               port: int = 8765) -> None:
    Handler.config_path = config_path
    Handler.queue_path = queue_path
    Handler.seen_path = seen_path
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Insta Reactor web UI on http://0.0.0.0:%d/ "
             "(point your phone's browser at this PC's LAN/Tailscale IP)", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
