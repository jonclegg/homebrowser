"""Humanized mouse and keyboard input, delivered as real X11 events.

Every action goes through cdp-server's native input endpoint (`POST /input`),
which drives the browser's X display with xdotool. Pages receive the events the
way a physical mouse and keyboard deliver them, not through CDP's Input domain.
Coordinates are viewport CSS pixels of the page passed in.
"""

import json
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
_cursor = {"position": None}

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
    steps = random.randint(18, 36)
    cx1 = x0 + (x1 - x0) * random.uniform(0.2, 0.45) + random.uniform(-50, 50)
    cy1 = y0 + (y1 - y0) * random.uniform(0.05, 0.4) + random.uniform(-40, 40)
    cx2 = x0 + (x1 - x0) * random.uniform(0.55, 0.85) + random.uniform(-40, 40)
    cy2 = y0 + (y1 - y0) * random.uniform(0.55, 0.95) + random.uniform(-30, 30)
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = (u ** 3) * x0 + 3 * (u ** 2) * t * cx1 + 3 * u * (t ** 2) * cx2 + (t ** 3) * x1
        y = (u ** 3) * y0 + 3 * (u ** 2) * t * cy1 + 3 * u * (t ** 2) * cy2 + (t ** 3) * y1
        if 0 < i < steps:
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)
        points.append((x, y))
    return points

###############################################################################

def _move_actions(x, y):
    """A curved path from the cursor's last spot (or a nearby one) to (x, y)."""
    start = _cursor["position"] or (
        min(1400, max(8, x + random.uniform(-200, 200))),
        min(800, max(8, y + random.uniform(-140, 140))),
    )
    _cursor["position"] = (x, y)
    points = [[px, py, random.randint(4, 14)] for px, py in _curve(start[0], start[1], x, y)]
    return [{"type": "path", "points": points}]

###############################################################################

def human_move(page, x, y):
    send_input(page, _move_actions(x, y))

###############################################################################

def click_at(page, x, y):
    actions = _move_actions(x, y)
    actions.append({"type": "sleep", "ms": random.randint(80, 220)})
    actions.append({"type": "click"})
    send_input(page, actions)

###############################################################################

def human_click(page, locator):
    box = locator.bounding_box()
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

def hold(page, x, y, ms):
    actions = _move_actions(x, y)
    actions += [
        {"type": "sleep", "ms": random.randint(150, 300)},
        {"type": "down"},
        {"type": "sleep", "ms": ms},
        {"type": "up"},
    ]
    send_input(page, actions)

###############################################################################

def drag(page, x, y, x2, y2):
    """Press at (x, y), drag to (x2, y2) with ease-in-out, overshoot, settle, release.

    Slider challenges score the path, not just the endpoints.
    """
    actions = _move_actions(x, y)
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
    _cursor["position"] = (x2, y2)
    send_input(page, actions)
