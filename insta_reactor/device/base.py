"""Abstract device: the low-level 'hands and eyes' of the automation.

Everything above this layer thinks in terms of `find_text`, `tap`, `swipe`,
`current_package` — never raw coordinates or a specific automation library. That
keeps the navigator portable across uiautomator2, raw adb, or a future backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class UiNode:
    """A resolved on-screen element, in device pixels."""
    text: str
    desc: str            # content-description (accessibility label)
    resource_id: str
    clazz: str           # android class, e.g. android.widget.Button
    bounds: tuple[int, int, int, int]   # (left, top, right, bottom)
    clickable: bool = False

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2


class Device(ABC):
    # ---- observation ----------------------------------------------------
    @abstractmethod
    def current_package(self) -> str: ...

    @abstractmethod
    def screenshot(self, path: str | None = None):
        """Return a screenshot (PIL image or ndarray) and optionally save it."""

    @abstractmethod
    def dump_hierarchy(self) -> str:
        """Return the current UI Automator XML hierarchy."""

    @abstractmethod
    def find(self, *, text: str | None = None, desc: str | None = None,
             resource_id: str | None = None, textContains: str | None = None,
             descContains: str | None = None,
             className: str | None = None) -> UiNode | None:
        """Return the first matching node, or None."""

    @abstractmethod
    def find_all(self, **kwargs) -> list[UiNode]:
        ...

    def exists(self, **kwargs) -> bool:
        return self.find(**kwargs) is not None

    # ---- actions --------------------------------------------------------
    @abstractmethod
    def tap(self, x: int, y: int) -> None: ...

    @abstractmethod
    def long_press(self, x: int, y: int, duration: float = 0.6) -> None: ...

    @abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration: float = 0.2) -> None: ...

    @abstractmethod
    def input_text(self, text: str) -> None: ...

    @abstractmethod
    def press_back(self) -> None: ...

    @abstractmethod
    def press_home(self) -> None: ...

    @abstractmethod
    def window_size(self) -> tuple[int, int]:
        """(width, height) in pixels."""

    # ---- convenience ----------------------------------------------------
    def tap_node(self, node: UiNode) -> None:
        x, y = node.center
        self.tap(x, y)

    def start_app(self, package: str) -> None:
        raise NotImplementedError
