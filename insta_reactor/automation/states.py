"""Explicit UI states + how to recognize each one.

The whole navigation layer is a state machine, not a linear script. Every
action verifies it reached the expected state before continuing; if not, it
retries and then recovers to a known state. This module owns the "where am I?"
question.
"""

from __future__ import annotations

import enum

INSTAGRAM_PACKAGE = "com.instagram.android"


class State(enum.Enum):
    UNKNOWN = "unknown"          # can't tell / interrupted
    NOT_INSTAGRAM = "not_ig"     # some other app / launcher in foreground
    INBOX = "inbox"              # the DM list (Direct)
    CHAT = "chat"                # inside one conversation (thread)
    REEL_VIEWER = "reel"         # a reel is open fullscreen
    COMMENTS = "comments"        # the comments sheet is open
    INTERRUPTION = "interrupt"   # dialog/popup/update prompt on top


# Recovery: from any state, pressing Back this many times should walk us out
# to the inbox. The navigator uses this as its "panic" path.
MAX_BACK_TO_INBOX = 5
