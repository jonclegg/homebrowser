"""Humanized mouse and keyboard input, delivered as real X11 events.

Every action goes through cdp-server's native input endpoint (`POST /input`),
which drives the browser's X display with xdotool. Pages receive the events the
way a physical mouse and keyboard deliver them, not through CDP's Input domain.
Coordinates are viewport CSS pixels of the page passed in.
"""

import json
import math
import os
import random
import time
import urllib.error
import urllib.request
import weakref
from urllib.parse import parse_qs, urlparse

CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")

# Playwright key names -> X11 keysyms
KEYSYMS = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "ArrowDown": "Down",
    "ArrowUp": "Up",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "PageDown": "Next",
    "PageUp": "Prior",
}

_target_ids = weakref.WeakKeyDictionary()

###############################################################################

def pause(low=0.35, high=1.1):
    time.sleep(random.uniform(low, high))

###############################################################################

def _cdp_server_url(endpoint):
    parsed = urlparse(CDP_URL)
    seed = parse_qs(parsed.query).get("fingerprint", [None])[0]
    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/fingerprint/{seed}/{endpoint}" if seed else f"{base}/{endpoint}"

###############################################################################

def target_id(page):
    if page not in _target_ids:
        session = page.context.new_cdp_session(page)
        _target_ids[page] = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        session.detach()
    return _target_ids[page]

###############################################################################

def _post(endpoint, body):
    request = urllib.request.Request(
        _cdp_server_url(endpoint), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"cdp-server /{endpoint} failed ({error.code}): {error.read().decode()[:300]}") from None

###############################################################################

def restore_window(page):
    """Un-minimize the page's window. Returns True if it was minimized.

    Real X11 input needs the window on screen. Chrome restores a window's saved
    state after a crash, and a minimized one stays unmapped: every click and key
    then lands nowhere while CDP screenshots still render the page (measured: a
    whole site-test shard typed into an empty Bing box).
    """
    session = page.context.new_cdp_session(page)
    window = session.send("Browser.getWindowForTarget")
    minimized = window["bounds"]["windowState"] == "minimized"
    if minimized:
        session.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "normal"}})
    session.detach()
    if minimized:
        pause(0.4, 0.8)
    return minimized

###############################################################################

def send_input(page, actions):
    """Run native input actions against one page. Raises on any failure."""
    restore_window(page)
    return _post("input", {"target": target_id(page), "actions": actions})

###############################################################################

def settle_window(page):
    """Have cdp-server re-size this page's window to one whose geometry holds up."""
    return _post("window", {"target": target_id(page)})

###############################################################################

def reset_zoom(page):
    """Put a zoomed page back to 100%, the way a person would (Ctrl+0).

    Chrome remembers zoom per site, so one stray Ctrl+wheel keeps a site zoomed
    for good, and CSS-scale screenshots of a zoomed page come out shrunken.
    """
    session = page.context.new_cdp_session(page)
    zoom = session.send("Page.getLayoutMetrics")["cssVisualViewport"].get("zoom", 1)
    session.detach()
    if zoom != 1:
        send_input(page, [{"type": "key", "keys": "ctrl+0"}])
        pause(0.4, 0.8)
    return zoom

###############################################################################

def _curve(x0, y0, x1, y1):
    """Timed path points [x, y, ms] along a curve from (x0, y0) to (x1, y1).

    Sampled every 6-12 ms and placed on a minimum-jerk profile: the hand starts
    slowly, is fastest midway and settles onto the target, taking longer for
    longer moves. Evenly spaced points with per-point noise moved at one speed
    with a zig-zag no hand makes.
    """
    cx1 = x0 + (x1 - x0) * random.uniform(0.2, 0.45) + random.uniform(-50, 50)
    cy1 = y0 + (y1 - y0) * random.uniform(0.05, 0.4) + random.uniform(-40, 40)
    cx2 = x0 + (x1 - x0) * random.uniform(0.55, 0.85) + random.uniform(-40, 40)
    cy2 = y0 + (y1 - y0) * random.uniform(0.55, 0.95) + random.uniform(-30, 30)
    duration = random.uniform(180, 260) + math.hypot(x1 - x0, y1 - y0) * random.uniform(0.35, 0.55)
    points, elapsed = [], 0
    while elapsed < duration:
        step = min(random.randint(6, 12), math.ceil(duration - elapsed))
        elapsed += step
        progress = min(1.0, elapsed / duration)
        t = 10 * progress ** 3 - 15 * progress ** 4 + 6 * progress ** 5
        u = 1 - t
        x = (u ** 3) * x0 + 3 * (u ** 2) * t * cx1 + 3 * u * (t ** 2) * cx2 + (t ** 3) * x1
        y = (u ** 3) * y0 + 3 * (u ** 2) * t * cy1 + 3 * u * (t ** 2) * cy2 + (t ** 3) * y1
        points.append([x, y, step])
    return points

###############################################################################

def cursor(page):
    """Where the pointer is now, in the page's viewport CSS pixels."""
    return send_input(page, [])["cursor"]

###############################################################################

def _move_actions(page, x, y):
    """A curved path from where the pointer really is to (x, y).

    Starting anywhere else makes the pointer jump there first: the page sees one
    pointermove hundreds of pixels long (measured: 560 x 294 px).
    """
    start = cursor(page)
    return [{"type": "path", "points": _curve(start[0], start[1], x, y)}]

###############################################################################

def human_move(page, x, y):
    send_input(page, _move_actions(page, x, y))

###############################################################################

def click_at(page, x, y):
    actions = _move_actions(page, x, y)
    actions.append({"type": "sleep", "ms": random.randint(80, 220)})
    actions.append({"type": "click"})
    send_input(page, actions)

###############################################################################

# The first element matching a CSS selector (and holding the text, when given)
# that is on screen and shown, as the agent reads pages (agent/steps.py HINTS_SCRIPT).
FIND_SCRIPT = """([selector, text]) => {
  for (const el of document.querySelectorAll(selector)) {
    if (text && !(el.innerText || '').includes(text)) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 4 || box.height < 4) continue;
    if (box.bottom < 0 || box.right < 0 || box.top > innerHeight || box.left > innerWidth) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    return { x: box.x, y: box.y, width: box.width, height: box.height };
  }
  return null;
}"""


def find(page, selector, text=None):
    """The viewport box {x, y, width, height} of the first visible match, or None.

    One page.evaluate in Patchright's isolated world. Patchright's locator API
    sends some 25 CDP calls per lookup (DOM.describeNode, DOM.resolveNode,
    DOM.getBoxModel, Runtime.getProperties...), and a bot-check vendor saw it:
    a search box found this way passed 3 of 3, found by locator it was
    challenged at once, with the same input.
    """
    return page.evaluate(FIND_SCRIPT, [selector, text])

###############################################################################

def human_click(page, selector, text=None):
    box = find(page, selector, text)
    if box is None:
        raise RuntimeError(f"nothing visible matches {selector!r}" + (f" with text {text!r}" if text else ""))
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    click_at(page, x, y)

###############################################################################

def human_type(page, text):
    actions = []
    for char in text:
        actions.append({"type": "type", "text": char, "delay_ms": 0})
        actions.append({"type": "sleep", "ms": random.randint(55, 150)})
        if char == " " and random.random() < 0.3:
            actions.append({"type": "sleep", "ms": random.randint(100, 350)})
    send_input(page, actions)

###############################################################################

def press(page, key):
    send_input(page, [{"type": "key", "keys": KEYSYMS.get(key, key)}])

###############################################################################

def scroll(page, amount):
    clicks = max(1, round(abs(amount) / 100))
    send_input(page, [{"type": "wheel", "direction": "down" if amount > 0 else "up", "clicks": clicks}])

###############################################################################

def _drift(x, y, ms):
    """Path points for a hand holding still for ms: a slow wander within 2.5 px of (x, y).

    A held mouse is never perfectly still; a hold with no pointermove at all is
    what a script sends (measured: none in 1.5 s).
    """
    points, held, px, py = [], 0, x, y
    while held < ms:
        step = min(random.randint(40, 160), ms - held)
        px = x + max(-2.5, min(2.5, px - x + random.uniform(-0.8, 0.8)))
        py = y + max(-2.5, min(2.5, py - y + random.uniform(-0.8, 0.8)))
        points.append([px, py, step])
        held += step
    return points

###############################################################################

def hold(page, x, y, ms):
    actions = _move_actions(page, x, y)
    actions += [
        {"type": "sleep", "ms": random.randint(150, 300)},
        {"type": "down"},
        {"type": "path", "points": _drift(x, y, ms)},
        {"type": "up"},
    ]
    send_input(page, actions)

###############################################################################

def drag(page, x, y, x2, y2):
    """Press at (x, y), drag to (x2, y2) with ease-in-out, overshoot, settle, release.

    Slider challenges score the path, not just the endpoints.
    """
    actions = _move_actions(page, x, y)
    actions += [{"type": "sleep", "ms": random.randint(120, 250)}, {"type": "down"},
                {"type": "sleep", "ms": random.randint(50, 150)}]
    steps = random.randint(28, 45)
    overshoot = random.uniform(3, 9)
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = 3 * t * t - 2 * t * t * t
        px = x + (x2 - x) * eased + (overshoot if i == steps else 0) + random.uniform(-1.2, 1.2)
        py = y + (y2 - y) * eased + random.uniform(-1.2, 1.2)
        points.append([px, py, random.randint(8, 22)])
    for settle in (0.6, 0.3, 0.1, 0.0):
        points.append([x2 + overshoot * settle, y2, random.randint(30, 70)])
    actions += [{"type": "path", "points": points},
                {"type": "sleep", "ms": random.randint(100, 200)}, {"type": "up"}]
    send_input(page, actions)
