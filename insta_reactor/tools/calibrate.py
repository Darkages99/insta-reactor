"""Dump the current device screen so you can calibrate selectors.

Usage:
    pip install uiautomator2
    python -m uiautomator2 init          # one-time: install the on-device agent
    adb devices                          # confirm your phone/emulator is listed
    # open Instagram to the screen you want to inspect, then:
    python -m insta_reactor.tools.calibrate --out data/calib

Produces:
    data/calib/screen.png        screenshot
    data/calib/hierarchy.xml     full UI Automator dump
    data/calib/elements.txt      flattened text / desc / resource-id per node

Read elements.txt, then paste the durable labels into
insta_reactor/automation/selectors.py.
"""

from __future__ import annotations

import argparse
import os
import re
import sys


def _flatten(xml: str) -> str:
    lines = []
    for m in re.finditer(r"<node\b([^>]*)>", xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        text = attrs.get("text", "")
        desc = attrs.get("content-desc", "")
        rid = attrs.get("resource-id", "")
        clz = attrs.get("class", "")
        bounds = attrs.get("bounds", "")
        if text or desc or rid:
            lines.append(f"text={text!r:30} desc={desc!r:30} "
                         f"id={rid:55} class={clz:35} {bounds}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Dump device screen for calibration")
    ap.add_argument("--out", default=os.path.join("data", "calib"))
    ap.add_argument("--serial", default="")
    args = ap.parse_args(argv)

    from ..device.u2_device import U2Device
    dev = U2Device(args.serial)

    os.makedirs(args.out, exist_ok=True)
    dev.screenshot(os.path.join(args.out, "screen.png"))
    xml = dev.dump_hierarchy()
    with open(os.path.join(args.out, "hierarchy.xml"), "w", encoding="utf-8") as f:
        f.write(xml)
    with open(os.path.join(args.out, "elements.txt"), "w", encoding="utf-8") as f:
        f.write(_flatten(xml))

    print(f"current package: {dev.current_package()}")
    print(f"wrote screenshot + hierarchy + elements to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
