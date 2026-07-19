"""Chaquopy entry shim for the V3 app.

Kotlin (`EngineBridge.kt`) binds to this ONE module and calls its functions, so
there is a single stable Chaquopy entry point. It just re-exports
`insta_reactor.app_entry` (the real seam) and adds on-device logging: there is no
console the user can read, so engine logs go to a file in the app's data dir.
"""

from __future__ import annotations

import logging
import os

# Re-export the real boundary so Kotlin can call v3_entry.<fn> directly.
from insta_reactor.app_entry import (  # noqa: F401
    load_config,
    save_config,
    review_queue,
    run,
)

_configured = False


def init_logging(data_dir: str) -> str:
    """Route engine logging to <data_dir>/engine.log. Idempotent."""
    global _configured
    if not _configured:
        logging.basicConfig(
            filename=os.path.join(data_dir, "engine.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        _configured = True
    return data_dir
