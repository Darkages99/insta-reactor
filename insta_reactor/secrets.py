"""Secret loading — API keys that must never land in git or config.json.

Deliberately separate from config.py: `data/config.json` holds tunable,
inspectable, shareable knobs; secrets do not belong there. Resolution order
(first hit wins):

  1. an explicit environment variable (e.g. OPENROUTER_API_KEY) — best for CI
     and for the on-device runner, which can inject it without a file;
  2. `data/secrets.json` — a small gitignored JSON blob for local dev.

Returns "" when nothing is set, so callers can treat "no key" as "LLM disabled"
(build_llm() does exactly that) rather than crashing. Nothing here ever raises.
"""

from __future__ import annotations

import json
import logging
import os

from .paths import data_path

log = logging.getLogger("insta_reactor.secrets")

DEFAULT_SECRETS_PATH = data_path("secrets.json")

# Logical secret name -> the env var checked first for it.
_ENV_FOR = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
}


def get_secret(name: str, path: str = DEFAULT_SECRETS_PATH) -> str:
    """Return the secret `name` (env first, then secrets.json), or "" if unset."""
    env_var = _ENV_FOR.get(name, name.upper())
    val = os.environ.get(env_var)
    if val:
        return val.strip()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            v = data.get(name)
            if isinstance(v, str) and v.strip():
                return v.strip()
    except (ValueError, OSError):
        log.warning("could not read secrets file %s", path)
    return ""
