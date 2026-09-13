"""Browser backend — drive Instagram *web* on a PC via Playwright.

This is the V3-browser pivot: instead of the slow on-phone AccessibilityService,
we automate instagram.com in a real Chromium (its own profile, so the user's
main browser and phone stay free). The engine/runner/flags/LLM/RAG/learning
layers above the `Backend` seam are reused unchanged — only the "hands and eyes"
change from a device node-tree to the DOM.

Key advantage over the phone path: reels shared in DMs open at a stable URL
(`/p/<shortcode>/` or `/reel/<shortcode>/`), giving a real per-reel id, so
cross-run dedup is reliable rather than best-effort.
"""
