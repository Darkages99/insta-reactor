"""Repo-root-anchored paths.

All persistent state defaults to ``<repo_root>/data/...``. These used to be
plain relative paths (``os.path.join("data", "config.json")``), which only
resolve correctly when the process's current working directory happens to be
the repo root — e.g. running the web server from a different cwd raised
``OSError: [Errno 22] Invalid argument`` trying to open ``data\\config.json``.
Anchoring to this file's location makes the defaults independent of cwd.
"""

from __future__ import annotations

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_path(*parts: str) -> str:
    return os.path.join(REPO_ROOT, "data", *parts)
