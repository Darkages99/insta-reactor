"""Per-chat persistence for AndroidBackend.scan_organic_reactions, so re-running
`coldstart --live-chat NAME` on the same chat resumes from where it left off
instead of re-walking (and re-logging) the same reel-reply groups.

Reels have no stable id (see android.py's iter_reels), so across separate
process runs there is no way to say "this is the exact same bubble as last
time." The best available anchor is content + walk order: remember the
(kind, reply_text) of the OLDEST group reached last run as a resume marker,
and next run skip everything from the bottom until that marker is seen again,
then keep collecting only what's older than it. New messages arriving at the
bottom between runs (this is a live chat) just get walked past harmlessly.
"""
from __future__ import annotations

import json
import os
import re

from ..paths import data_path

_STATE_DIR = data_path("coldstart_scans")


def _safe_name(chat: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", chat) or "chat"


def _paths(chat: str, base_dir: str = _STATE_DIR) -> tuple[str, str]:
    safe = _safe_name(chat)
    return (os.path.join(base_dir, f"{safe}.state.json"),
            os.path.join(base_dir, f"{safe}.jsonl"))


def load_resume_marker(chat: str, base_dir: str = _STATE_DIR) -> dict | None:
    state_path, _ = _paths(chat, base_dir)
    if not os.path.exists(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f).get("resume_after")


def save_resume_marker(chat: str, marker: dict | None,
                        base_dir: str = _STATE_DIR) -> None:
    os.makedirs(base_dir, exist_ok=True)
    state_path, _ = _paths(chat, base_dir)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"resume_after": marker}, f)


def append_pairs(chat: str, pairs: list[dict], base_dir: str = _STATE_DIR) -> None:
    if not pairs:
        return
    os.makedirs(base_dir, exist_ok=True)
    _, jsonl_path = _paths(chat, base_dir)
    with open(jsonl_path, "a", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")


def load_all_pairs(chat: str, base_dir: str = _STATE_DIR) -> list[dict]:
    _, jsonl_path = _paths(chat, base_dir)
    if not os.path.exists(jsonl_path):
        return []
    out: list[dict] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
