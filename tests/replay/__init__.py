"""Device-free replay harness for P2 parity bring-up.

Lets the *real* V3 device path (`AccessibilityDevice`) run against captured
Instagram node trees with no phone attached: a `ReplayBridge` serves a screen's
node list exactly as the live Kotlin `ReactorAccessibilityService` would, so the
engine (navigator / backend / collector) can be exercised end-to-end and its
selectors re-verified deterministically as `pytest`.

Fixtures are generated from real uiautomator2 `dump_hierarchy()` captures under
`data/calib*/` (see `ig_xml.py`), converted into the a11y JSON node contract the
bridge speaks. This is the off-device half of P2: it proves selector/geometry
parity on real trees. Feeding a *fresh* on-device DM-thread capture through the
same harness is the final on-device green-light (needs the P3 app shell).
"""
