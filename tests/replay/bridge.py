"""A `ReplayBridge` that serves a captured screen to the real device path.

It implements the exact `AccessibilityBridge` surface the live Kotlin service
exposes, but instead of reading `rootInActiveWindow` it returns a fixed, captured
node list. Wrapping it in the *real* `AccessibilityDevice` means the parity suite
tests production code — `_nodes()` JSON parsing, `find/find_all` selector
semantics, gesture unit conversion — not a stand-in.

Gestures are recorded (so tests can assert coordinates/units) and, by default,
no-op on the tree. For multi-screen flows a test can hand several screens and
drive transitions explicitly via `set_screen()`.
"""

from __future__ import annotations

import json
from pathlib import Path

_FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "screens"

IG_PACKAGE = "com.instagram.android"


def load_screen(name: str) -> list[dict]:
    return json.loads((_FIX_DIR / f"{name}.json").read_text(encoding="utf-8"))


class ReplayBridge:
    """Duck-typed stand-in for `ReactorAccessibilityService` (the P1 bridge).

    Serves `nodes` as the active window. `package`/`size` mimic
    `currentPackage()`/`windowSize()`. Every gesture is appended to `.calls` for
    assertions; none mutate the tree unless the test swaps screens itself.
    """

    def __init__(self, nodes: list[dict], *, package: str = IG_PACKAGE,
                 size: tuple[int, int] = (1272, 2772)):
        self._nodes = nodes
        self._package = package
        self._size = size
        self.calls: list[tuple] = []

    # -- test controls (not part of the bridge surface) -------------------
    def set_screen(self, nodes: list[dict], package: str = IG_PACKAGE) -> None:
        self._nodes = nodes
        self._package = package

    # -- AccessibilityBridge surface --------------------------------------
    def currentPackage(self) -> str:
        return self._package

    def windowSize(self):
        return [self._size[0], self._size[1]]

    def nodesJson(self) -> str:
        return json.dumps(self._nodes, ensure_ascii=False)

    def tap(self, x: int, y: int) -> bool:
        self.calls.append(("tap", x, y))
        return True

    def longPress(self, x: int, y: int, durationMs: int) -> bool:
        self.calls.append(("longPress", x, y, durationMs))
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, durationMs: int) -> bool:
        self.calls.append(("swipe", x1, y1, x2, y2, durationMs))
        return True

    def inputText(self, text: str) -> bool:
        self.calls.append(("inputText", text))
        return True

    def pressBack(self) -> bool:
        self.calls.append(("pressBack",))
        return True

    def pressHome(self) -> bool:
        self.calls.append(("pressHome",))
        return True

    def startApp(self, package: str) -> bool:
        self.calls.append(("startApp", package))
        return True
