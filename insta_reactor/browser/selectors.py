"""★ THE ONE FILE YOU CALIBRATE for Instagram *web* (the DOM analogue of
automation/selectors.py for the phone).

Instagram's web CSS class names are obfuscated and rotate constantly, so NOTHING
here keys off a class. Every selector uses a durable signal instead:
  * ARIA roles / aria-labels  (stable, accessibility-backed)
  * visible text / emoji       (what a human reads)
  * structure                  (the largest <ul> is the comment list, etc.)

All strings were verified live against instagram.com/direct on 2026-09-13
(see the browser-pivot note). If IG changes its DM markup, this is the file to
re-verify — usually a one-line change, exactly like the phone selectors.
"""

from __future__ import annotations

# ---- inbox / conversation list -------------------------------------------
SEARCH_BOX = 'input[placeholder="Search"]'
# A conversation row is a role=button that carries an avatar <img>; the visible
# name is a descendant <span>. We match the row by (exact) name text, never class.
CONVERSATION_ROW = 'div[role="button"]:has(img)'

# ---- thread (open conversation) ------------------------------------------
MAIN = 'div[role="main"]'
# A shared reel renders a 24×24 play-icon svg labelled "Clip". Its enclosing
# message container holds the hover React/Reply controls.
REEL_MARKER = '[aria-label="Clip"]'
# Hover controls (the "from <sender>" suffix names who sent the message, which is
# how we tell incoming reels from our own outgoing ones).
REACT_BUTTON_PREFIX = "React to message from "
REPLY_BUTTON_PREFIX = "Reply to message from "
# The DM text composer.
COMPOSER = 'div[role="textbox"][contenteditable="true"]'

# ---- reel viewer overlay (after clicking a reel) --------------------------
DIALOG = 'div[role="dialog"]'
# Opening a reel navigates the tab to /p/<shortcode>/ or /reel/<shortcode>/.
REEL_URL_RE = r"/(?:p|reel)/([A-Za-z0-9_-]+)/"

# ---- native quick-reaction popup -----------------------------------------
# The six emojis IG offers as one-tap reactions. Anything outside this set needs
# the composer typed-reply path (the "+" full picker is deliberately not used).
QUICK_REACTION_EMOJIS = ("❤️", "😂", "😮", "😢", "😡", "👍")
