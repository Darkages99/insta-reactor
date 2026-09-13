"""OpenAI-compatible chat client — the remote half of the hybrid brain.

Mirrors the design of engine/classifier.py: a small Protocol seam
(`ChatClient`), one concrete implementation, and a `build_llm(settings)` factory
that returns None when the LLM is disabled or unconfigured, so every caller can
treat "no client" as "fall back to the deterministic engine" and nothing ever
crashes on a missing key or a network blip.

Transport is stdlib `urllib` only (same as notify.py) — no `openai`/`requests`
dependency — because the core engine is deliberately dependency-free and must
keep running on-device (Chaquopy) where pip installs are painful. The endpoint
is the standard `/chat/completions` shape, so this works against OpenRouter,
OpenAI, or any compatible gateway just by changing base_url + model.

`complete()` NEVER raises: on any error (no key, timeout, bad status, malformed
body) it logs and returns None. The reply layer maps None -> "flag for a human",
which is exactly the safe default.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Optional, Protocol

from ..secrets import get_secret

log = logging.getLogger("insta_reactor.llm")


class ChatClient(Protocol):
    def complete(self, system: str, user: str,
                 *, temperature: float = 0.7,
                 max_tokens: int = 120) -> Optional[str]:
        """Return the assistant's text, or None on any failure."""
        ...


@dataclass
class OpenAICompatClient:
    """Talks to an OpenAI-compatible /chat/completions endpoint over urllib."""

    api_key: str
    model: str = "openai/gpt-4o-mini"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 20.0

    def complete(self, system: str, user: str,
                 *, temperature: float = 0.7,
                 max_tokens: int = 120) -> Optional[str]:
        if not self.api_key:
            return None
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter asks for these for attribution; harmless elsewhere.
            "HTTP-Referer": "https://github.com/insta-reactor",
            "X-Title": "Insta Reactor",
        }
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception:
            log.exception("LLM request failed")
            return None
        try:
            content = body["choices"][0]["message"]["content"]
            return content.strip() if isinstance(content, str) else None
        except (KeyError, IndexError, TypeError):
            log.warning("unexpected LLM response shape: %r", body)
            return None


def build_llm(settings) -> Optional[ChatClient]:
    """Return a ChatClient if the LLM is enabled AND a key is available, else None.

    Enabled means `settings.use_llm` is truthy and `settings.llm_mode != "off"`.
    The key comes from secrets.get_secret (env or data/secrets.json). No key =>
    None => the pipeline stays fully deterministic. Nothing here raises.
    """
    if not getattr(settings, "use_llm", False):
        return None
    if getattr(settings, "llm_mode", "assist") == "off":
        return None
    key = get_secret("openrouter_api_key")
    if not key:
        log.info("use_llm is on but no API key found; staying deterministic")
        return None
    return OpenAICompatClient(
        api_key=key,
        model=getattr(settings, "llm_model", "openai/gpt-4o-mini"),
        base_url=getattr(settings, "llm_base_url",
                         "https://openrouter.ai/api/v1"),
        timeout=float(getattr(settings, "llm_timeout", 20.0)),
    )
