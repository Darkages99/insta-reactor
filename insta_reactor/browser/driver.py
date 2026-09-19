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

from ..paths import data_path

log = logging.getLogger("insta_reactor.browser.driver")

DEFAULT_PROFILE_DIR = data_path("browser_profile")
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

    def _clear_stale_lock(self) -> None:
        """Remove Chrome's SingletonLock/-Cookie/-Socket from the profile dir.

        Chrome writes these while it owns the profile and removes them on a
        clean exit. If a prior run was killed (crash, force-close, task
        manager) they survive, and the next launch sees them and silently
        forwards to a "session" that isn't actually running instead of
        starting fresh — which Playwright then reports as the confusing
        "Opening in existing browser session" failure. They're always safe to
        delete when nothing is actually running against this profile (a
        second *real* launch against the same profile only happens if the
        caller violates the single-run-at-a-time invariant, which is a bug in
        the caller, not something this lock is meant to catch)."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = os.path.join(self.profile_dir, name)
            try:
                if os.path.lexists(path):
                    os.remove(path)
            except OSError:
                pass

    def start(self):
        """Launch the persistent Chromium context and open a page.

        Idempotent: if we already own an open context (e.g. a prior run in
        this same process left the browser open waiting for a manual login),
        reuse it instead of launching a second Chromium against the same
        profile dir — a second launch against a dir another instance already
        owns is exactly what trips Chrome's "Opening in existing browser
        session" failure."""
        if self.context is not None:
            return self.page
        from playwright.sync_api import sync_playwright
        os.makedirs(self.profile_dir, exist_ok=True)
        self._clear_stale_lock()
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
        # IG serves a shimmer/skeleton shell immediately, then hydrates the real
        # UI (search box, thread rows) a variable amount later — a fixed short
        # sleep flakes under load (multiple chromium instances, slow network).
        # Wait for a real post-hydration signal instead: either the search box
        # (logged in) or the login form (logged out) — whichever shows up first.
        try:
            self.page.wait_for_selector(
                'input[placeholder="Search"], input[name="password"]',
                timeout=15000)
        except Exception:
            pass   # fall through; is_logged_in() will just report False

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
        attempt to log in — that's the human's job (we never touch a password).

        Retries a couple of times: right after a fresh persistent-context launch
        the very first navigation can land mid-redirect (a real logged-in profile
        briefly showing a loading/landing URL before settling on the inbox), which
        would otherwise be misread as logged-out."""
        for attempt in range(3):
            self.goto_inbox()
            if self.is_logged_in():
                return True
            self.page.wait_for_timeout(1500 + attempt * 1000)
        return False

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
