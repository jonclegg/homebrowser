#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
"""The screen, and the arithmetic between its four numbers.

`screen.width` alone is one lookup. What makes this a script is that the values
have to AGREE with each other and with the window, and a detector checks the
arithmetic rather than the numbers:

  * `availWidth`/`availHeight` cannot exceed the screen. A taskbar or a menu bar
    takes some, so on a real machine `avail` is smaller — and equal on both axes
    is what a naive spoof produces.
  * `outerWidth - innerWidth` is the browser's own chrome. It is a small,
    stable number on a real browser; if the screen is spoofed and the window is
    not, the two stop being consistent with each other.
  * CSS asks the same questions. `matchMedia('(device-width: 1920px)')`,
    `(resolution: 1dppx)` and `(color: 8)` read ScreenInfo directly rather than
    through Screen, and until 27.09.2026 they answered with the HOST's screen
    beside a persona's screen.width — measured on 0.2.2 as JS 1920x1080 at 1x
    against CSS 1470 at 2dppx. fv.pro called that "screen is not real" and
    iphey flagged it twice; neither did once CSS agreed.
  * a Worker has no `screen` at all, so there is nothing to disagree — which is
    why this one is checked in an IFRAME instead, where there is.

Usage: browser/verify/verify-0020.py <core binary>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Claims, launch  # noqa: E402

CORE = sys.argv[1]

# A 1920x1080 Windows machine: a taskbar 40 px tall, no side bars. Deliberately
# not this Mac, which is neither that size nor that shape.
SCREEN = {
    "width": 1920,
    "height": 1080,
    "availWidth": 1920,
    "availHeight": 1040,
    "availLeft": 0,
    "availTop": 0,
    "colorDepth": 24,
    "devicePixelRatio": 1,
}

READ = """
(() => {
  const s = screen;
  const out = {
    width: s.width, height: s.height,
    availWidth: s.availWidth, availHeight: s.availHeight,
    availLeft: s.availLeft, availTop: s.availTop,
    colorDepth: s.colorDepth, pixelDepth: s.pixelDepth,
    dpr: devicePixelRatio,
    outerWidth, innerWidth, outerHeight, innerHeight,
  };
  const mm = (q) => matchMedia(q).matches;
  out.css = {
    width: mm(`(device-width: ${s.width}px)`),
    height: mm(`(device-height: ${s.height}px)`),
    aspect: mm(`(device-aspect-ratio: ${s.width}/${s.height})`),
    resolution: mm(`(resolution: ${devicePixelRatio}dppx)`),
    webkitRatio: mm(`(-webkit-device-pixel-ratio: ${devicePixelRatio})`),
    color: mm(`(color: ${s.colorDepth / 3})`),
  };
  const f = document.createElement('iframe');
  f.src = 'about:blank';
  document.body.appendChild(f);
  const i = f.contentWindow.screen;
  out.iframe = {width: i.width, height: i.height, availHeight: i.availHeight};
  f.remove();
  return JSON.stringify(out);
})()
"""


def main():
    claims = Claims("0020 — screen and window", CORE)

    with launch(CORE, {"screen": SCREEN}) as s:
        got = json.loads(s.js(READ))
        print(f"  measured: {got}")

        for key in ("width", "height", "availWidth", "availHeight", "colorDepth"):
            claims.check(got[key] == SCREEN[key],
                         f"screen.{key} is {SCREEN[key]} (got {got[key]})")

        claims.check(got["pixelDepth"] == got["colorDepth"],
                     f"pixelDepth matches colorDepth, as it does everywhere real "
                     f"({got['pixelDepth']} vs {got['colorDepth']})")

        # The arithmetic, which is what a detector actually checks.
        claims.check(got["availWidth"] <= got["width"]
                     and got["availHeight"] <= got["height"],
                     "the available area does not exceed the screen")
        claims.check(got["availHeight"] < got["height"],
                     f"and something is taken by a taskbar — equal on both axes is "
                     f"what a naive spoof produces ({got['availHeight']} < {got['height']})")

        # The window has to be consistent with the screen it claims to be on.
        chrome_h = got["outerHeight"] - got["innerHeight"]
        claims.check(0 <= chrome_h <= 200,
                     f"outerHeight - innerHeight is a real browser's chrome, not a "
                     f"leftover from the host ({chrome_h} px)")
        claims.check(got["outerWidth"] <= got["width"],
                     f"the window fits on the screen it claims "
                     f"({got['outerWidth']} <= {got['width']})")

        for feature, ok in got["css"].items():
            claims.check(ok, f"CSS agrees with JS on {feature} — a media query "
                             f"answering with the host's screen contradicts "
                             f"screen.* ({got['css']})")

        claims.check(got["iframe"]["width"] == SCREEN["width"]
                     and got["iframe"]["availHeight"] == SCREEN["availHeight"],
                     f"an iframe measures the same screen: {got['iframe']}")

    with launch(CORE, None) as s:
        bare = json.loads(s.js(
            "JSON.stringify({w: screen.width, h: screen.height, "
            "css1920: matchMedia('(device-width: 1920px)').matches})"))
        print(f"  unconfigured: {bare}")
        claims.control(
            (bare["w"], bare["h"]) != (SCREEN["width"], SCREEN["height"])
            and not bare["css1920"],
            f"an unconfigured build reports the host's own screen (got {bare})",
        )

    return claims.done()


if __name__ == "__main__":
    sys.exit(main())
