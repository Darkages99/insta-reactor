"""Convert a uiautomator2 `dump_hierarchy()` XML into the a11y node contract.

The V3 device (`AccessibilityDevice`) reads a flat JSON array of nodes in DFS
pre-order (see `AccessibilityBridge` docstring). V2 captured screens as
UIAutomator XML. The field mapping is 1:1 (docs/V3_PLAN.md §3), so we can replay
a real V2 capture through the real V3 device path — that's what makes the P2
parity suite possible with no phone.

Fidelity note: `AccessibilityService.rootInActiveWindow` returns only the
foreground *app* window, whereas u2's `dump_hierarchy()` also captures the system
UI (status/nav bars) and any other windows. We therefore keep only nodes whose
`package` is Instagram, in pre-order — which is exactly the flat, pre-ordered
list `AccessibilityDevice._nodes()` iterates, so `find()`'s "first match in tree
order" semantics are preserved.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

IG_PACKAGE = "com.instagram.android"

_BOUNDS_RE = re.compile(r"-?\d+")


def _bounds(s: str) -> list[int]:
    nums = [int(x) for x in _BOUNDS_RE.findall(s or "")]
    return nums[:4] if len(nums) == 4 else [0, 0, 0, 0]


def _node_obj(el: ET.Element) -> dict:
    """One XML <node> element → the a11y contract's flat node object."""
    a = el.attrib
    return {
        "cls": a.get("class", ""),
        "text": a.get("text", "") or "",
        "desc": a.get("content-desc", "") or "",
        "id": a.get("resource-id", "") or "",
        "bounds": _bounds(a.get("bounds", "")),
        "clickable": a.get("clickable") == "true",
        "scrollable": a.get("scrollable") == "true",
        "longClickable": a.get("long-clickable") == "true",
        "enabled": a.get("enabled") == "true",
    }


def xml_to_nodes(xml_text: str, package: str = IG_PACKAGE) -> list[dict]:
    """Return the app-window nodes as the a11y JSON contract, DFS pre-order.

    Only nodes belonging to `package` are kept (dropping system-UI chrome), which
    mirrors what `rootInActiveWindow` exposes for the foreground app.
    """
    root = ET.fromstring(xml_text)
    out: list[dict] = []

    def walk(el: ET.Element) -> None:
        # <hierarchy> wrapper has no package; only real <node>s carry attribs.
        if el.tag == "node" and el.attrib.get("package") == package:
            out.append(_node_obj(el))
        for child in el:
            walk(child)

    walk(root)
    return out


def convert_file(xml_path: Path) -> list[dict]:
    return xml_to_nodes(xml_path.read_text(encoding="utf-8"))


# --- fixture generation -------------------------------------------------------
# Maps each captured screen dir under data/ to a fixture name the parity suite
# loads. These are real IG trees grabbed during V2 calibration.
_CAPTURES = {
    "clips_viewer": "calib",         # full-screen reel viewer (REEL_VIEWER)
    "chat": "calib_chat",            # a DM thread containing reel bubbles (CHAT)
    "comments": "calib_comments",    # an open comments sheet (COMMENTS)
    "inbox": "calib_inbox",          # the DM inbox thread list (INBOX)
    "dms": "calib_dms",              # the DM/notes tray screen
}


def regenerate(repo_root: Path) -> None:
    """(Re)build tests/fixtures/screens/*.json from data/calib*/hierarchy.xml."""
    out_dir = repo_root / "tests" / "fixtures" / "screens"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, cap in _CAPTURES.items():
        xml = repo_root / "data" / cap / "hierarchy.xml"
        if not xml.exists():
            print(f"skip {name}: {xml} missing")
            continue
        nodes = convert_file(xml)
        (out_dir / f"{name}.json").write_text(
            json.dumps(nodes, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{name}: {len(nodes)} IG nodes -> {name}.json")


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    regenerate(root)
