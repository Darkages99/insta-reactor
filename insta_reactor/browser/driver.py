"""Playwright lifecycle for the browser backend.

Owns the Chromium instance and the single page we drive. Uses a *persistent*
context with its own user-data dir (`data/browser_profile/`) so the Instagram
login survives across runs and — crucially — is completely separate from the
user's everyday Chrome, leaving their browser and phone free while a run happens.

Nothing here decides anything; it's pure "hands and eyes". Login is never
automated (we must never type the user's password): if the session isn't logged
in, `ensure_logged_in` reports that so the UI can ask the human to log in once
in the opened window.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("insta_reactor.browser.driver")

DEFAULT_PROFILE_DIR = os.path.join("data", "browser_profile")
INBOX_URL = "https://www.instagram.com/direct/inbox/"


class BrowserDriver:
    def __init__(self, profile_dir: str = DEFAULT_PROFILE_DIR,
                 headless: bool = False, slow_mo_ms: int = 0):
        self.profile_dir = profile_dir
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self._pw = None
        self.context = None
        self.page = None

    def start(self):
        """Launch the persistent Chromium context and open a page."""
        from playwright.sync_api import sync_playwright
        os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            viewport={"width": 1400, "height": 900},
            # 2x so thumbnail screenshots are crisp on the control panel's
            # gallery (a 1x clip of a small DM bubble reads as a blurry smudge).
            device_scale_factor=2,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = (self.context.pages[0] if self.context.pages
                     else self.context.new_page())
        return self.page

    def goto_inbox(self) -> None:
        self.page.goto(INBOX_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)

    def is_logged_in(self) -> bool:
        """True if we're on the DM inbox (not bounced to a login/landing page)."""
        try:
            url = self.page.url
            if "accounts/login" in url or url.rstrip("/").endswith("instagram.com"):
                return False
            # The composer search box / thread list only exist when logged in.
            return self.page.locator('input[placeholder="Search"]').count() > 0
        except Exception:
            return False

    def ensure_logged_in(self) -> bool:
        """Navigate to the inbox and report whether we're authenticated. Does NOT
        attempt to log in — that's the human's job (we never touch a password)."""
        self.goto_inbox()
        return self.is_logged_in()

    def self_username(self) -> str:
        """Best-effort read of the logged-in account's handle (used to tell our
        own outgoing messages from incoming ones). Empty string if unknown."""
        try:
            # The account switcher button in the DM header shows the username.
            btn = self.page.locator('div[role="button"]', has_text="").first
            txt = self.page.evaluate(
                """() => {
                    const el = document.querySelector('nav a[href="/"] img[alt]');
                    if (el) { const m = el.getAttribute('alt').match(/([\\w.]+)'s profile/); if (m) return m[1]; }
                    return "";
                }""")
            return (txt or "").strip()
        except Exception:
            return ""

    def screenshot_element(self, locator, path: str) -> bool:
        """Save a PNG screenshot of one element (a reel thumbnail). Returns
        whether it succeeded; never raises."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            locator.screenshot(path=path)
            return True
        except Exception:
            log.exception("thumbnail screenshot failed for %s", path)
            return False

    def close(self) -> None:
        for fn in (lambda: self.context and self.context.close(),
                   lambda: self._pw and self._pw.stop()):
            try:
                fn()
            except Exception:
                pass
        self.context = self.page = self._pw = None
