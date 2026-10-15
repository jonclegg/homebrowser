"""Stealthbox's embedded DeepSeek driver.

A caller chats with Stealthbox; each message becomes one turn. DeepSeek (via
OpenRouter) looks at the screen, picks the next browser action, and Stealthbox
executes it humanized, until DeepSeek replies to the caller.

Design notes
------------
* The screenshot is the page's viewport as the X display shows it, scaled so one
  screenshot pixel is one CSS pixel: browser UI over the page (prompts, open
  dropdown lists) is in it, because real clicks land on it. The model is told
  the exact image size, so its coordinates are usable directly.
* Visible interactive elements are also read from the DOM and handed to the
  model as coordinate hints. That is what makes targeting reliable: on a
  press-and-hold check the model picks the button centre to the pixel.
* Bot checks are detected from the DOM as well as by the model, and the model
  works them: press-and-hold and the like.
* What only the user can do (a sign-in, a payment, a check the model cannot
  clear) ends the turn with status `needs_user`; the server then shares the
  viewer so the user can do it from anywhere. The model never types into a
  password field: a `type` aimed at one becomes that handoff.
* The model keeps notes for itself. Within a turn, `remember` adds to a working
  memory that is always in the prompt (the step history is only a window). Across
  runs, `note` writes to a per-site notebook that is loaded whenever the browser
  is on that site again; see notebook.py.
* `deepseek/deepseek-v4.1-flash` is the default: of the DeepSeek models on
  OpenRouter only the Flash line accepts images, and on a grounding probe it was
  within 5 px of a button centre where `deepseek-v4-flash-vision-exp` was 25-65 px
  off, at a fifth of the price. Low reasoning effort keeps that accuracy; turning
  reasoning off loses it.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote_plus, urlparse

import mss
import mss.exception
from PIL import Image, ImageChops
from websockets.sync.client import connect as websocket_connect

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("STEALTHBOX_EXAMPLES", os.path.join(os.path.dirname(HERE), "examples")))

import bing_first  # noqa: E402
import humanize  # noqa: E402
import page_images  # noqa: E402

DEFAULT_MODEL = os.environ.get("STEALTHBOX_MODEL") or "deepseek/deepseek-v4.1-flash"
# Takes a step when the default model is going round in circles: date pickers and
# custom dropdowns are where Flash gets stuck. About 1.5 cents a step.
ESCALATION_MODEL = os.environ.get("STEALTHBOX_ESCALATION_MODEL") or "anthropic/claude-sonnet-5.5"
# Waits on an unchanged screen before the model is told (still_waits), and twice
# that before the step goes to the escalation model.
STILL_WAITS = 3
# A run ends when this many steps in a row change nothing (steps_without_progress),
# or, as a guard against a page whose animation never stops, after RUN_STEP_CAP steps.
RUN_STEP_CAP = 400
API = "https://openrouter.ai/api/v1/chat/completions"
DATA_DIR = os.environ.get("STEALTHBOX_DATA", "/data")
KEY_PATH = os.path.join(DATA_DIR, "openrouter.key")

BLOCK_PATTERNS = {
    "press_and_hold": r"press\s*(&|and)\s*hold|activate and hold",
    "image_captcha": r"select all images|select each image|click all (images|squares)",
    "slider": r"slide to verify|drag the slider",
    "robot_or_human": r"robot or human",
    "access_denied": r"access denied|reference\s*#\s*\d",
    "human_verification": r"confirm you are human|human verification",
    "access_restricted": r"access (is )?temporarily restricted|unusual activity from your (device|network)",
    "blocked_url": r"/blocked|/sorry/|/challenge|/cdn-cgi/challenge",
}

# Challenge widgets by name: {name: CSS selector}. Only real *widget elements*
# count: a normal page routinely names a captcha vendor inside its JS bundles, so
# matching raw page HTML produces false positives. The vendors' own markers are
# not part of Stealthbox; <data>/guidance/bot-checks.json adds them (a
# {"widgets": {name: selector}} object). Without it, the wording, the URL and the
# model's own view of the screen find a check.
CHALLENGE_WIDGETS = {
    "cloudflare_challenge": "iframe[src*='challenges.cloudflare.com'], #cf-chl-widget",
}
BOT_CHECKS_PATH = os.path.join(DATA_DIR, "guidance", "bot-checks.json")


def challenge_widgets():
    """CHALLENGE_WIDGETS plus the installed bot-checks.json's, read on each scan."""
    if not os.path.exists(BOT_CHECKS_PATH):
        return CHALLENGE_WIDGETS
    with open(BOT_CHECKS_PATH) as fh:
        return {**CHALLENGE_WIDGETS, **json.load(fh)["widgets"]}

INTERSTITIAL_TEXT = (
    r"just a moment|checking your browser|enable javascript and cookies|"
    r"verifying you are human|verify you are human|attention required|"
    r"performing security verification|one more step|ddos protection by"
)

HINT_SELECTOR = (
    "button, a[href], input:not([type=hidden]), select, textarea, "
    "[role=button], [role=link], [id*=px], [id*=captcha]"
)

ACTION_SCHEMA = """Reply with ONLY a JSON object:
{"observation": "<one sentence: what is on screen>",
 "blocked": true|false,
 "block_type": "none"|"press_and_hold"|"image_captcha"|"slider"|"checkbox"|"login"|"other",
 "action": "click"|"type"|"select"|"press"|"hold"|"drag"|"scroll"|"wait"|"back"|"open"|"goto"|"read"|"save_images"|"reply",
 "x": <int px or null>, "y": <int px or null>,
 "x2": <int px or null>, "y2": <int px or null>,
 "text": "<text to type, key name, domain, url, folder name, or your reply; else null>",
 "submit": true|false,  (only for type: press Enter after typing, e.g. to run a search)
 "amount": <int, only for scroll: pixels, positive is down>,
 "hold_ms": <int, only for hold>,
 "status": "done"|"needs_input"|"needs_user"|"failed",  (only for reply)
 "remember": "<optional: a fact to add to your working memory for this message>",
 "note": {"site": "<domain or general>", "text": "<optional: a lesson for future runs>"},
 "reason": "<why>"}"""

FOCUSED_TEXT_FIELD_SCRIPT = """() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el) return false;
  // A field inside a frame is focused through the frame element; trust it.
  if (el.tagName === 'IFRAME') return true;
  return el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
}"""

# The native dropdown list (<select>) at a point, for the select action.
SELECT_AT_SCRIPT = """([x, y]) => {
  const el = document.elementFromPoint(x, y);
  const select = el && el.closest('select');
  if (!select) return null;
  return {options: [...select.options].map(option => (option.label || option.text).trim()), selected: select.selectedIndex};
}"""

# Selects what the focused field already holds, so typing replaces it: typed
# after it, a second search for "jordan" became "jordanjordan" on a shoe store
# and an airport code doubled on an airline's booking form.
SELECT_FIELD_TEXT_SCRIPT = """() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (el && ['INPUT', 'TEXTAREA'].includes(el.tagName) && el.value) el.select();
}"""

# What is in view: the window's scroll position and where the element at the
# centre sits, which also moves when a page scrolls an inner container.
VIEW_SIGNATURE_SCRIPT = """() => {
  const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  return [scrollY, el ? el.tagName : '', el ? Math.round(el.getBoundingClientRect().top) : 0];
}"""

# A browser call that takes longer than this has hit a hung renderer: Playwright's
# sync calls into it never return. Model calls are not counted. Entering a site
# through Bing retries for minutes, so `open` gets longer.
WATCHDOG_SECONDS = 120
WATCHDOG_OPEN_SECONDS = 300
# A tab that does not answer a CDP command in this long has a hung renderer.
RESPONSIVE_SECONDS = 5

READ_TEXT_LIMIT = 6000
READ_LINK_LIMIT = 60


# ---------------------------------------------------------------- OpenRouter

def api_key() -> str:
    """The key from <data>/openrouter.key, which `stealthbox key` writes."""
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as fh:
            key = fh.read().strip()
        if key:
            return key
    raise RuntimeError("No OpenRouter key yet. Run `stealthbox key` and paste it into the page it opens.")


def chat(messages, model, max_tokens=8000, temperature=0.1, json_mode=False):
    """One completion. Returns (content, cost in USD, the model's reasoning text).

    DeepSeek's Flash models reason before answering; the reasoning counts against
    max_tokens, so a small budget returns an empty answer.
    """
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning": {"effort": "low"},
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/jonclegg/stealthbox",
            "X-Title": "Stealthbox",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenRouter {exc.code}: {exc.read().decode()[:500]}") from exc
    except (OSError, http.client.HTTPException) as exc:
        # URLError and timeouts are OSErrors; a connection dropped mid-answer raises
        # IncompleteRead or RemoteDisconnected.
        raise RuntimeError(f"OpenRouter did not answer: {exc}") from exc
    content = payload["choices"][0]["message"].get("content")
    finish_reason = payload["choices"][0].get("finish_reason")
    if not content and finish_reason == "length" and max_tokens < 16000:
        # The reasoning used the whole budget before the answer (three steps
        # running on one site); the same request with room to finish answers.
        # Once: a 32,000-token answer takes minutes, and a dense date picker can
        # spend them all (a travel site sat eight minutes on one step).
        content, cost, reasoning = chat(messages, model, max_tokens * 2, temperature, json_mode)
        return content, cost + float(payload.get("usage", {}).get("cost") or 0.0), reasoning
    if not content:
        raise RuntimeError(f"OpenRouter returned no content (finish_reason {finish_reason})")
    reasoning = payload["choices"][0]["message"].get("reasoning") or ""
    return content, float(payload.get("usage", {}).get("cost") or 0.0), reasoning


def _data_url(path):
    with open(path, "rb") as fh:
        blob = base64.b64encode(fh.read()).decode()
    return f"data:image/png;base64,{blob}"


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def site_of(url):
    """The domain a URL belongs to, for the notebook: host without www."""
    host = (urlparse(url or "").hostname or "").lower()
    return host.removeprefix("www.")


# ------------------------------------------------------------------ the browser

def _browser_socket():
    """A browser-level CDP connection of our own, beside Playwright's."""
    with urllib.request.urlopen(f"{humanize.CDP_URL}/json/version", timeout=10) as response:
        url = json.load(response)["webSocketDebuggerUrl"]
    return websocket_connect(url, max_size=None, open_timeout=10)


def _command(socket, message_id, method, params=None, session=None, timeout=10):
    """Send one CDP command and return its result. Raises TimeoutError if no answer comes."""
    message = {"id": message_id, "method": method, "params": params or {}}
    if session:
        message["sessionId"] = session
    socket.send(json.dumps(message))
    deadline = time.time() + timeout
    while True:
        reply = json.loads(socket.recv(timeout=max(0.1, deadline - time.time())))
        if reply.get("id") == message_id:
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message')}")
            return reply.get("result", {})


def page_url(page):
    """The URL of the page's own tab, as the browser reports it.

    Patchright's page.url can be an iframe's: a listings site's tab reported the
    URL of an ad frame inside it, so the agent refused to go anywhere on the site
    and the console named the session after the ad.
    """
    session = page.context.new_cdp_session(page)
    try:
        return session.send("Target.getTargetInfo")["targetInfo"]["url"]
    finally:
        session.detach()


def page_targets():
    """The open tabs, as Chrome lists them (most recently active first within a window)."""
    with urllib.request.urlopen(f"{humanize.CDP_URL}/json/list", timeout=5) as response:
        return [target for target in json.load(response) if target["type"] == "page"]


def front_tab():
    """The tab in front, as {"id", "url", "title"}, or None with no tab open.

    Chrome lists page targets most recently active first, which is right within one
    window but not across windows: LRN's course player, a popup window in front of
    its opener's, was listed after the opener's tab. So the window manager's active
    window, which Chrome names after its tab, picks the tab, and the order only
    breaks ties. A page with no title (Chrome names the window after its address)
    matches no title, and then the order alone decides.
    """
    tabs = page_targets()
    if not tabs:
        return None
    window = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
    title = window.rsplit(" - ", 1)[0]
    tab = next((tab for tab in tabs if tab["title"] == title), tabs[0])
    return {"id": tab["id"], "url": tab["url"], "title": tab["title"]}


def history_suggestions(text):
    """Addresses from the browser's own history matching `text`, best first.

    cdp-server reads them: Chrome's profile is root's alone, and on a Linux host
    this process runs as the data folder's owner.
    """
    with urllib.request.urlopen(f"{humanize.CDP_URL}/history?q={quote_plus(text)}", timeout=10) as response:
        return json.load(response)["suggestions"]


def display_thumbnail(width):
    """A JPEG of the whole X display, what the live view shows, `width` pixels wide."""
    with mss.mss() as screen:
        shot = screen.grab(screen.monitors[1])
    image = Image.frombytes("RGB", shot.size, shot.rgb)
    image.thumbnail((width, width))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=70)
    return out.getvalue()


def typed_url(text):
    """What the address bar would open for `text`: a URL as typed, a bare host on
    https, anything else as a Bing search."""
    text = text.strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE) or text.startswith(("about:", "chrome:", "data:")):
        return text
    if " " not in text and ("." in text or text.split(":")[0] == "localhost"):
        return "https://" + text
    return "https://www.bing.com/search?q=" + quote_plus(text)


def navigate_tab(target_id, url):
    """Load `url` in a tab, as typed in its address bar, and bring the tab to the front."""
    with _browser_socket() as socket:
        session = _command(socket, 1, "Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        _command(socket, 2, "Page.navigate", {"url": url, "transitionType": "typed"}, session, 30)
        _command(socket, 3, "Target.activateTarget", {"targetId": target_id})
        _command(socket, 4, "Target.detachFromTarget", {"sessionId": session})


def activate_tab(target_id):
    """Bring a tab to the front from the browser side, without Playwright: the
    server's browser thread is busy while a run holds for the user."""
    with _browser_socket() as socket:
        _command(socket, 1, "Target.activateTarget", {"targetId": target_id})


def open_tab(background):
    """Open a blank tab from the browser side, without Playwright: the server's browser
    thread may be busy with a run. In the background it leaves the run's tab in front."""
    with _browser_socket() as socket:
        return _command(socket, 1, "Target.createTarget", {"url": "about:blank", "background": background})["targetId"]


def close_target(target_id):
    """Close a tab from the browser side, which works even when its renderer is hung."""
    with _browser_socket() as socket:
        _command(socket, 1, "Target.closeTarget", {"targetId": target_id})


def close_unresponsive_tabs():
    """Close every tab that would stall a connect, and return their URLs.

    Playwright's connect sets up every page as it attaches, and waits on each.
    A tab whose renderer does not answer stalls it (after one site's renderer
    hung, each new connection timed out for the rest of a site-test shard), and so
    does a tab that never got a URL, not even about:blank (a tab left half-created
    after another site did the same to another shard).
    """
    closed = []
    with _browser_socket() as socket:
        targets = _command(socket, 1, "Target.getTargets")["targetInfos"]
        unborn = {target["targetId"] for target in targets if target["type"] == "page" and not target["url"]}
        if unborn:
            # A tab being created right now has no URL for a moment.
            time.sleep(2)
            targets = _command(socket, 2, "Target.getTargets")["targetInfos"]
            unborn &= {target["targetId"] for target in targets if target["type"] == "page" and not target["url"]}
        for index, target in enumerate(target for target in targets if target["targetId"] in unborn):
            _command(socket, 3 + index, "Target.closeTarget", {"targetId": target["targetId"]})
            closed.append(target)
        pages = [target for target in targets if target["type"] == "page" and target["targetId"] not in unborn]
        for index, target in enumerate(pages):
            base = 100 + index * 3
            try:
                session = _command(socket, base, "Target.attachToTarget",
                                   {"targetId": target["targetId"], "flatten": True})["sessionId"]
            except RuntimeError:
                # Closed between listing and attaching: nothing left to close.
                continue
            try:
                _command(socket, base + 1, "Network.setCacheDisabled", {"cacheDisabled": False},
                         session, RESPONSIVE_SECONDS)
            except TimeoutError:
                _command(socket, base + 2, "Target.closeTarget", {"targetId": target["targetId"]})
                closed.append(target)
                continue
            _command(socket, base + 2, "Target.detachFromTarget", {"sessionId": session})
        # A connect that starts while a closed tab is still going away crashes
        # Patchright's driver ("Network.setCacheDisabled: session closed").
        going = {target["targetId"] for target in closed}
        message_id = 100 + len(pages) * 3
        deadline = time.time() + 10
        while going and time.time() < deadline:
            time.sleep(0.25)
            message_id += 1
            going &= {target["targetId"] for target in _command(socket, message_id, "Target.getTargets")["targetInfos"]}
    return [target["url"] for target in closed]


class Watchdog:
    """Closes the tab when a browser call has hung, so the turn can carry on.

    A hung renderer blocks Playwright's sync calls with no timeout (run 8 lost
    three sites this way until the site test killed them after ten minutes). Armed while the turn works the browser, paused during model calls.
    Closing the tab makes the blocked call fail; the next step replaces the tab.
    """

    def __init__(self, turn):
        self.turn = turn
        self.seconds = WATCHDOG_SECONDS
        self.lock = threading.Lock()
        self.target = None
        self.since = 0.0
        self.stopped = False
        threading.Thread(target=self._watch, daemon=True).start()

    def arm(self, page, seconds=WATCHDOG_SECONDS):
        target = humanize.target_id(page)
        with self.lock:
            self.target, self.since, self.seconds = target, time.time(), seconds

    def disarm(self):
        with self.lock:
            self.target = None

    @contextlib.contextmanager
    def paused(self):
        with self.lock:
            saved, self.target = self.target, None
        try:
            yield
        finally:
            with self.lock:
                self.target, self.since = saved, time.time()

    def stop(self):
        self.stopped = True

    def _watch(self):
        while not self.stopped:
            time.sleep(5)
            with self.lock:
                hung = self.target is not None and time.time() - self.since > self.seconds
                target = self.target if hung else None
                if hung:
                    self.target = None
            if target:
                try:
                    close_target(target)
                except Exception as exc:  # noqa: BLE001 - the watchdog thread must outlive one failure
                    self.turn.repairs.append(f"step {len(self.turn.steps)}: the tab stopped responding "
                                             f"for {self.seconds}s and could not be closed: {exc}")
                    continue
                self.turn.repairs.append(f"step {len(self.turn.steps)}: the tab stopped responding "
                                         f"for {self.seconds}s; closed it")


# ------------------------------------------------------------------ the page

HINTS_SCRIPT = """([selector, limit]) => {
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (out.length >= limit) break;
    const box = el.getBoundingClientRect();
    if (box.width < 4 || box.height < 4) continue;
    if (box.bottom < 0 || box.right < 0 || box.top > innerHeight || box.left > innerWidth) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    out.push({
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60),
      label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.id || '').slice(0, 40),
      x: box.x + box.width / 2,
      y: box.y + box.height / 2,
    });
  }
  return out;
}"""


def frame_offset(frame):
    """Where a frame's viewport sits in the page, or None if it is not on screen."""
    if frame.parent_frame is None:
        return 0.0, 0.0
    box = frame.frame_element().bounding_box()
    if not box or box["width"] < 4 or box["height"] < 4:
        return None
    return box["x"], box["y"]


def is_challenge_frame(frame):
    """Cloudflare's challenge frame, which no script of ours may run in.

    The challenge notices code evaluated inside its frame: with the agent's DOM
    scans running there every step, a click on the checkbox kept cycling back to
    the checkbox; the same click with nothing evaluated in the frame cleared it.
    """
    return "challenges.cloudflare.com" in frame.url


def challenge_checkbox_hints(page):
    """Cloudflare's challenge checkbox, which no DOM scan can see.

    The checkbox sits in a closed shadow root inside Cloudflare's cross-origin
    frame, so querySelectorAll finds nothing there. The frame itself is visible
    from the page, and the checkbox is always at its left edge, mid-height
    (measured: a real click there clears its "Verify you are human").
    """
    hints = []
    for frame in page.frames:
        if not is_challenge_frame(frame):
            continue
        try:
            box = frame.frame_element().bounding_box()
        except Exception:  # noqa: BLE001 - frames detach mid-scan
            continue
        if box and box["width"] >= 100 and box["height"] >= 40:
            hints.append({"tag": "checkbox", "text": "Cloudflare challenge: Verify you are human", "label": "challenge checkbox",
                          "x": round(box["x"] + 28), "y": round(box["y"] + box["height"] / 2)})
    return hints


def dom_hints(page, limit=40):
    """Visible interactive elements in the viewport, with page CSS-pixel centres.

    One script per frame: asking Playwright element by element costs hundreds of
    CDP round trips a step, which was most of a step's time.
    """
    hints = []
    for frame in page.frames:
        if is_challenge_frame(frame):
            continue
        try:
            offset = frame_offset(frame)
            if offset is None:
                continue
            found = frame.evaluate(HINTS_SCRIPT, [HINT_SELECTOR, limit])
        except Exception:  # noqa: BLE001 - frames detach mid-scan
            continue
        for hint in found:
            hint["x"] = round(hint["x"] + offset[0])
            hint["y"] = round(hint["y"] + offset[1])
            hints.append(hint)
    hints = challenge_checkbox_hints(page) + hints
    seen, out = set(), []
    for h in hints:
        key = (h["x"], h["y"], h["text"])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out[:limit]


# Chrome's frame border around a window that is not maximized (cdp-server's value).
WINDOW_FRAME_BORDER = 4


def safe_screenshot(page, path):
    """The page's viewport as the screen shows it, at CSS scale. False if it failed.

    Taken from the X display, not with CDP: input is real X11 events, so the model
    has to see what they land on. A CDP screenshot leaves out browser UI drawn over
    the page (a permission prompt, the crash-restore bubble, an open dropdown or
    autofill list), and each of those took clicks the model could not explain.
    The viewport sits inside the window's frame, flush with its bottom, as
    cdp-server places native input.
    """
    try:
        if page.evaluate("document.visibilityState") != "visible":
            page.bring_to_front()
        humanize.restore_window(page)
        session = page.context.new_cdp_session(page)
        bounds = session.send("Browser.getWindowForTarget")["bounds"]
        metrics = session.send("Page.getLayoutMetrics")
        session.detach()
    except Exception:  # noqa: BLE001 - page navigated or closed mid-call
        return False
    device, css = metrics["visualViewport"], metrics["cssVisualViewport"]
    zoom = device["clientWidth"] / css["clientWidth"]
    scale = zoom / device.get("zoom", 1)
    border = 0 if bounds["windowState"] in ("maximized", "fullscreen") else WINDOW_FRAME_BORDER
    left = round((bounds["left"] + border) * scale)
    top = round((bounds["top"] + bounds["height"] - border) * scale - device["clientHeight"])
    try:
        with mss.MSS() as screen:
            raw = screen.grab({"left": left, "top": top,
                               "width": round(device["clientWidth"]), "height": round(device["clientHeight"])})
    except mss.exception.ScreenShotError:  # the region is off the X screen; the caller retries
        return False
    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    image.resize((round(css["clientWidth"]), round(css["clientHeight"])), Image.LANCZOS).save(path)
    return True


DETECT_SCRIPT = """(widgets) => {
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    if (box.width < 60 || box.height < 30) return false;
    // A challenge is put where the visitor sees it; one far down the page is not.
    if (box.bottom <= 0 || box.top >= innerHeight || box.right <= 0 || box.left >= innerWidth) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };
  const found = [];
  for (const [name, selector] of Object.entries(widgets)) {
    if ([...document.querySelectorAll(selector)].slice(0, 4).some(visible)) found.push(name);
  }
  return {text: (document.title || '') + '\\n' + (document.body ? document.body.innerText : ''), widgets: found};
}"""


def frame_on_screen(frame, width, height):
    """True for the main frame, and for a frame whose box is in view at a real size.

    A challenge frame can be loaded hidden, tiny or off-screen while the real page
    shows: press-and-hold wording on one site and human-verification wording on
    another sat in such frames, and a run counted both homepages as blocked.
    """
    if frame.parent_frame is None:
        return True
    try:
        box = frame.frame_element().bounding_box()
    except Exception:  # noqa: BLE001 - frame detached mid-scan
        return False
    return bool(box and box["width"] >= 4 and box["height"] >= 4
                and box["x"] < width and box["y"] < height
                and box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0)


def detect_block(page):
    """DOM-side block check, independent of the model.

    Challenge text and widgets usually live in a cross-origin iframe, so
    scanning `body` alone misses them: Cloudflare's "Just a moment..." puts the
    wording in the <title>, not the body. This checks the title and body text of
    every frame, the URL, and the real challenge widget elements. Only a *visible*
    widget in view counts: a listings site ships a hidden captcha frame on its
    normal homepage, and an invisible frame, a passive badge, or a widget far
    below the fold is not something to solve.
    """
    url = page_url(page).lower()
    texts = []
    found = []
    try:
        width, height = page.evaluate("[innerWidth, innerHeight]")
    except Exception:  # noqa: BLE001 - page navigated mid-evaluate
        return []
    widgets = challenge_widgets()
    for frame in page.frames:
        if is_challenge_frame(frame) or not frame_on_screen(frame, width, height):
            continue
        try:
            result = frame.evaluate(DETECT_SCRIPT, widgets)
        except Exception:  # noqa: BLE001 - frames detach mid-scan
            continue
        texts.append(result["text"])
        found.extend(result["widgets"])
    all_text = "\n".join(texts).lower()

    for name, pat in BLOCK_PATTERNS.items():
        if name == "blocked_url":
            if re.search(pat, url):
                found.append(name)
        elif re.search(pat, all_text):
            found.append(name)

    if re.search(INTERSTITIAL_TEXT, all_text):
        found.append("interstitial")
        found.append("blocked")

    return sorted(set(found))


PASSWORD_TARGET_SCRIPT = """([x, y]) => {
  const isPassword = (el) => el && el.tagName === 'INPUT' && el.type === 'password';
  return isPassword(x === null || y === null ? null : document.elementFromPoint(x, y)) || isPassword(document.activeElement);
}"""


def types_into_password(page, decision):
    """True when a `type` would land in a password field: the one at x,y, or the focused one.

    Only the main frame is checked; a sign-in inside an iframe is the model's call.
    """
    if (decision.get("action") or "").lower() != "type":
        return False
    try:
        return bool(page.evaluate(PASSWORD_TARGET_SCRIPT, [decision.get("x"), decision.get("y")]))
    except Exception:  # noqa: BLE001 - page navigated mid-evaluate
        return False


def read_page(page):
    """The page's visible text and links, for the model to read on its next step."""
    content = page.evaluate(
        """([textLimit, linkLimit]) => {
          const main = document.querySelector('main');
          const scope = main && (main.innerText || '').trim().length > 200 ? main : document.body;
          const text = (scope.innerText || '').replace(/\\n{3,}/g, '\\n\\n').slice(0, textLimit);
          const seen = new Set();
          const links = [];
          for (const a of document.querySelectorAll('a[href]')) {
            const label = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
            if (!label || seen.has(a.href) || !a.href.startsWith('http')) continue;
            seen.add(a.href);
            // Tracking parameters make some hrefs kilobytes long; the path is what matters.
            links.push(label.slice(0, 80) + ' -> ' + a.href.slice(0, 200));
            if (links.length >= linkLimit) break;
          }
          return {text, links};
        }""",
        [READ_TEXT_LIMIT, READ_LINK_LIMIT],
    )
    return content["text"] + "\n\nLinks:\n" + "\n".join(content["links"])


# ------------------------------------------------------------------ deciding

def _conversation_text(conversation):
    if not conversation:
        return "(this is the first message)"
    lines = []
    for entry in conversation[-10:]:
        speaker = "caller" if entry["role"] == "caller" else "you"
        lines.append(f"{speaker}: {entry['text']}")
    return "\n".join(lines)


def _signature(step, tolerance=20):
    x, y = step.get("x"), step.get("y")
    spot = (round(x / tolerance), round(y / tolerance)) if x is not None and y is not None else None
    return step.get("action"), spot, step.get("text")


def stuck(steps, window=6):
    """True when the page has not moved on and the model is going round in circles.

    Either the last three steps were the identical action, or the last `window`
    steps on one URL used at most two different actions (Escape, click, Escape,
    click; or read, scroll, read, scroll). Waiting out a check is not stuck.
    """
    if len(steps) >= 3:
        last = steps[-3:]
        if (len({_signature(step) for step in last}) == 1
                and len({step.get("url") for step in last}) == 1
                and last[0].get("action") not in ("wait", "hold")):
            return True
    if len(steps) < window:
        return False
    last = steps[-window:]
    if len({step.get("url") for step in last}) != 1:
        return False
    if any(step.get("action") in ("wait", "hold", "save_images") for step in last):
        return False
    return len({_signature(step) for step in last}) <= 2


def record_screen(turn, shot):
    """Note whether this step's screenshot matches the last one, or the one before it.

    Full size, so a video's clock moving counts as a change. Only the last two
    screenshots are kept; for each step, just whether it changed.
    """
    index = len(turn.steps)
    with Image.open(shot) as image:
        now = image.convert("L")
    previous, before = turn.prints.get(index - 1), turn.prints.get(index - 2)
    same_previous = previous is not None and previous.size == now.size and not _changed(now, previous)
    same_before = before is not None and before.size == now.size and not _changed(now, before)
    turn.unchanged[index] = (same_previous, same_previous or same_before)
    turn.prints = {index - 1: previous, index: now} if previous is not None else {index: now}


def _changed(now, earlier):
    """Any pixel clearly different: a video's clock moving is a small change, and counts."""
    return ImageChops.difference(now, earlier).point(lambda value: 255 if value > 40 else 0).getbbox() is not None


def still_waits(turn):
    """How many of the last steps were waits that left the screen as it was.

    Waiting out a video or a loading page is right while the screen moves on. An
    LRN course video stopped itself at 0:59 on a "check yourself" card, and the
    model waited 40 steps, each time reporting it "playing at 0:59".
    """
    index, count = len(turn.steps), 0
    while index >= 1 and turn.steps[index - 1].get("action") == "wait" and turn.unchanged.get(index, (False,))[0]:
        count += 1
        index -= 1
    return count


def steps_without_progress(turn):
    """How many steps in a row left the screen as it was one or two steps before.

    A run stops on this, not on how many steps it took: one LRN lesson took more
    than 40 steps of real progress (videos, Next buttons) and was cut off. Going
    back and forth between two screens counts as no progress too.
    """
    index, count = len(turn.steps), 0
    while index >= 1 and turn.unchanged.get(index, (False, False))[1]:
        count += 1
        index -= 1
    return count


def decide(page, shot, turn, model):
    size = png_size(shot)
    dims = f"{size[0]}x{size[1]} pixels" if size else "unknown"
    try:
        vp = page.evaluate("({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})")
    except Exception:  # noqa: BLE001 - page navigated mid-evaluate (challenges do)
        vp = {"w": 1280, "h": 720, "dpr": 1}
    hints = dom_hints(page)
    site = site_of(page_url(page))
    recent = [
        {key: step.get(key) for key in ("step", "url", "action", "text", "did")}
        for step in turn.steps[-8:]
    ]
    extra = ""
    if turn.steps and turn.steps[-1].get("action") == "read" and turn.last_read:
        extra = f"\nText of the page from your last `read`:\n{turn.last_read}\n"
    memory = "\n".join(f"- {item}" for item in turn.memory) or "(empty)"
    record_screen(turn, shot)
    still = still_waits(turn)
    if still >= STILL_WAITS:
        extra += (
            f"\nNOTE: nothing on the screen changed during your last {still} waits, so waiting "
            "is not moving this on. A video whose time has not moved is paused: press its play "
            "button. A page that stays the same is waiting for you to do something.\n"
        )
    if stuck(turn.steps):
        extra += (
            "\nWARNING: you are going round in circles: the same few actions on the same "
            "page, and it is not moving on. Do something different: `type` with x,y, "
            "press Enter, `read` to find the link you need and `goto` it, or reply.\n"
        )
    prompt = (
        f"You are Stealthbox. You operate a real Chrome browser for a caller who "
        f"chats with you. Work through the caller's latest message one browser "
        f"action per step, then reply to them.\n\n"
        f"Conversation so far:\n{_conversation_text(turn.conversation)}\n\n"
        f"Caller's latest message: {turn.message}\n\n"
        f"Your notebook (guidance that ships with Stealthbox, lessons you wrote on "
        f"earlier runs, and the run log):\n"
        f"{turn.notebook.context_for(site)}\n\n"
        f"Your working memory for this message:\n{memory}\n\n"
        f"Step {len(turn.steps) + 1}. The run stops when {turn.max_stuck_steps} steps in a row change nothing on the page. "
        f"Your recent steps: {json.dumps(recent) if recent else 'none'}\n"
        f"{extra}\n"
        f"Current URL: {page_url(page)}\n"
        f"The screenshot is exactly {dims}. The CSS viewport is "
        f"{vp['w']}x{vp['h']} at devicePixelRatio {vp['dpr']}. Screenshot pixels map "
        f"1:1 to CSS pixels, so give x,y in screenshot pixels.\n"
        f"Interactive elements found in the DOM, with centre coordinates already in "
        f"screenshot pixels (use these when they match what you see):\n"
        f"{json.dumps(hints, separators=(',', ':'))}\n\n"
        f"Actions:\n"
        f"- click x,y | type text (give x,y to click the field first) | press key | "
        f"scroll amount | hold x,y hold_ms | drag x,y to x2,y2 | wait | back\n"
        f"- select: choose `text` from the dropdown list (<select>) at x,y with the "
        f"keyboard, which works even when clicking an option does not.\n"
        f"- open: enter a site you are not already on, by bare domain "
        f"(text \"example.com\"). It runs the vetted Bing-first path; do not drive Bing "
        f"by hand.\n"
        f"- goto: a URL on the site you are already on. Refused for other sites.\n"
        f"- read: get the page's text and links on your next step. Use it to read "
        f"details, prices, or listing URLs instead of guessing from the screenshot.\n"
        f"- save_images: download the photos of the item on this page into folder "
        f"`text` (for example \"car-1\"). Give x,y on the main photo when there is "
        f"one. Use it on a product or listing page.\n"
        f"- reply: send `text` to the caller and end this message. status \"done\" "
        f"when finished (include what they asked for), \"needs_input\" to ask them "
        f"something they can answer in chat (a texted code, a choice), \"needs_user\" "
        f"when the user must act in the page themselves, \"failed\" otherwise.\n"
        f"Never invent a username, password, payment detail or personal detail the "
        f"conversation does not give you. A sign-in, a payment, or a bot check you "
        f"cannot clear is \"needs_user\": reply with `text` saying exactly what they "
        f"need to do (\"Sign in to example.com\"). They do it in the viewer, then the "
        f"caller tells you to carry on.\n"
        f"For several items from a list (the first three results, every order), "
        f"`read` the list once, `remember` each item's URL, then `goto` them one by one; "
        f"going back to a results page is slow and loses your place.\n"
        f"Keep notes for yourself. `remember` facts you will need later in this "
        f"message (listing URLs, prices, what you saved), because old steps scroll "
        f"out of view. Write a `note` when you learn something about a site that "
        f"would save time next run: an obstacle, what got past it, where things are. "
        f"A note is about the site, not this message's answer (no prices or results).\n"
        f"If a bot check is on screen set blocked=true and name its block_type, then "
        f"work it with the right action:\n"
        f"- press_and_hold: action \"hold\" at the button centre, hold_ms at least 8000.\n"
        f"- slider: action \"drag\" from the handle to where the gap ends (x,y -> x2,y2).\n"
        f"- checkbox (\"I am not a robot\", \"Verify you are human\"): action \"click\" on the checkbox.\n"
        f"- image_captcha: read the prompt, then \"click\" each matching tile centre in "
        f"turn, one action per step. If tiles are ambiguous, say so in `reason`.\n"
        f"When the check is gone, keep going. If it will not clear, reply needs_user.\n"
        f"Detected from the DOM right now: {turn.dom_blocks or 'no bot check'}\n"
        f"{ACTION_SCHEMA}"
    )
    if stuck(turn.steps) or still >= 2 * STILL_WAITS:
        model = ESCALATION_MODEL
    turn.last_model = model
    model_started = time.time()
    with turn.watchdog.paused():
        raw, cost, reasoning = chat(
            [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_url(shot)}},
            ]}],
            model=model,
            json_mode=True,
        )
    turn.last_model_seconds = time.time() - model_started
    turn.last_reasoning = reasoning
    turn.cost_usd += cost
    try:
        # Models other than DeepSeek may wrap the object in a code fence.
        return json.loads(raw[raw.find("{"):raw.rfind("}") + 1] if "{" in raw else raw)
    except json.JSONDecodeError:
        return {"observation": raw[:300], "blocked": False, "action": "wait",
                "reason": "unparseable model output"}


# ------------------------------------------------------------------ actions

def do_hold(page, x, y, ms):
    """Press and hold. Returns the real elapsed milliseconds."""
    start = time.time()
    humanize.hold(page, x, y, ms)
    elapsed = int((time.time() - start) * 1000)
    humanize.pause(1.0, 1.8)
    return elapsed


def do_drag(page, x, y, x2, y2):
    """Press at (x,y), drag to (x2,y2) with human-like easing, release.

    Slider challenges score the path, not just the endpoints: a straight
    teleport is rejected.
    """
    humanize.drag(page, x, y, x2, y2)
    humanize.pause(1.0, 1.8)
    return f"drag ({x},{y})->({x2},{y2})"


def execute(page, d, turn):
    """Run one action. Returns (log line, the page to drive next)."""
    action = (d.get("action") or "wait").lower()
    x, y, text = d.get("x"), d.get("y"), d.get("text")
    x2, y2 = d.get("x2"), d.get("y2")

    if action in ("click", "type", "select", "hold", "drag"):
        # Real input outside the page lands on the browser's toolbar or the bare
        # desktop under the window (an airline site: a click at y=701 on a page
        # 688 tall, then a scroll there hid the whole window).
        width, height = page.evaluate("[innerWidth, innerHeight]")
        points = [(x, y)] + ([(x2, y2)] if action == "drag" else [])
        outside = [(px, py) for px, py in points
                   if px is not None and py is not None and not (0 <= px < width and 0 <= py < height)]
        if outside:
            return (f"{action} refused: {outside[0]} is outside the page, which is {width}x{height}; "
                    f"scroll to bring the target into view"), page

    if action == "drag":
        if None in (x, y, x2, y2):
            return "drag without both start and end coordinates", page
        return do_drag(page, x, y, x2, y2), page

    if action == "hold":
        if x is None or y is None:
            return "hold without coordinates", page
        ms = int(d.get("hold_ms") or 8000)
        held = do_hold(page, x, y, ms)
        return f"hold ({x},{y}) {held}ms", page

    if action == "click" and x is not None and y is not None:
        before = set(page.context.pages)
        humanize.click_at(page, x, y)
        # Wait through Playwright: a new tab or popup reaches it as an event, which
        # only its own calls let in. After a plain sleep none ever showed: the agent
        # clicked a sign-in portal tile five times, opening five tabs it never saw.
        opened = []
        for _ in range(4):
            page.wait_for_timeout(random.randint(200, 350))
            opened = [item for item in page.context.pages if item not in before]
            if opened:
                break
        if opened:
            opened[-1].wait_for_load_state("domcontentloaded")
            return f"click ({x},{y}) opened a new tab", opened[-1]
        return f"click ({x},{y})", page

    if action == "type" and text:
        if x is not None and y is not None:
            humanize.click_at(page, x, y)
            humanize.pause(0.2, 0.4)
        # A header search button often opens an overlay whose input takes focus
        # a moment later (seen on four sites, and on an airline's airport field).
        deadline = time.time() + 2.0
        while not page.evaluate(FOCUSED_TEXT_FIELD_SCRIPT) and time.time() < deadline:
            time.sleep(0.25)
        if not page.evaluate(FOCUSED_TEXT_FIELD_SCRIPT):
            # Typed into the page instead, the keys act on it: a space scrolls a
            # screen and Enter does nothing (measured on a pharmacy site, where the
            # agent then spent 38 steps looking for the header it had scrolled off).
            return (f"type not done: the click at ({x},{y}) did not focus a text field; "
                    f"find the field itself, or click the search icon that opens it"), page
        page.evaluate(SELECT_FIELD_TEXT_SCRIPT)
        humanize.human_type(page, text)
        humanize.pause(0.3, 0.7)
        if d.get("submit"):
            humanize.press(page, "Enter")
            humanize.pause(0.8, 1.6)
            return f"type {text!r} and Enter", page
        return f"type {text!r}", page

    if action == "select" and text:
        if x is None or y is None:
            return "select without coordinates", page
        dropdown = page.evaluate(SELECT_AT_SCRIPT, [x, y])
        if not dropdown:
            return f"select: there is no dropdown list at ({x},{y})", page
        labels = [label.lower() for label in dropdown["options"]]
        wanted = text.strip().lower()
        matches = [i for i, label in enumerate(labels) if label == wanted] or \
                  [i for i, label in enumerate(labels) if wanted in label]
        if not matches:
            return f"select: no option like {text!r}; options: {dropdown['options'][:30]}", page
        target = matches[0]
        # Focus it the way a person does, close the list, then step with the arrow
        # keys: real key presses that fire the page's change events.
        humanize.click_at(page, x, y)
        humanize.pause(0.3, 0.6)
        humanize.press(page, "Escape")
        steps = target - dropdown["selected"]
        for _ in range(abs(steps)):
            humanize.press(page, "ArrowDown" if steps > 0 else "ArrowUp")
            humanize.pause(0.08, 0.2)
        humanize.pause(0.4, 0.8)
        now = (page.evaluate(SELECT_AT_SCRIPT, [x, y]) or {}).get("selected")
        if now != target:
            return (f"select {dropdown['options'][target]!r} did not take: the list shows "
                    f"option {now}; try clicking the option's label on the page"), page
        return f"select {dropdown['options'][target]!r}", page

    if action == "press" and text:
        humanize.press(page, text)
        humanize.pause(0.6, 1.2)
        return f"press {text}", page

    if action == "scroll":
        amount = int(d.get("amount") or 600)
        before = page.evaluate(VIEW_SIGNATURE_SCRIPT)
        humanize.scroll(page, amount)
        humanize.pause(0.6, 1.2)
        if page.evaluate(VIEW_SIGNATURE_SCRIPT) == before:
            # The wheel goes to whatever is under the pointer, and a carousel there
            # can swallow it.
            return (f"scroll {amount}: the page did not move; `press` Home, End, "
                    f"PageUp or PageDown instead"), page
        return f"scroll {amount}", page

    if action == "back":
        # Single-page sites go back without a load event, so wait only for commit.
        page.go_back(wait_until="commit", timeout=10000)
        humanize.pause(0.8, 1.6)
        return "back", page

    if action == "open" and text:
        landed = bing_first.open_via_bing(page, text.strip())
        return f"open {text} -> {page_url(landed)[:80]}", landed

    if action == "goto" and text:
        if site_of(text) != site_of(page_url(page)):
            return (f"goto refused: {site_of(text)} is not the site you are on; "
                    f"enter it with open first"), page
        page.goto(text, wait_until="domcontentloaded")
        humanize.pause(0.8, 1.6)
        return f"goto {text}", page

    if action == "read":
        turn.last_read = read_page(page)
        return f"read {len(turn.last_read)} characters", page

    if action == "save_images":
        folder = page_images.safe_folder(text or f"page-{len(turn.steps)}")
        saved = page_images.save_images(page, os.path.join(turn.files_dir, folder), x, y)
        turn.files.extend(os.path.join(folder, name) for name in saved)
        return f"saved {len(saved)} images to {folder}/", page

    humanize.pause(1.0, 1.8)
    return "wait", page


# -------------------------------------------------------------------- loop

def usable_page(page, turn):
    """The page to drive this step, repairing what other CDP clients can break.

    The browser is shared: on the Dell another agent drove its own tab during a
    run, our tab's viewport shrank to 300x250 for seven minutes, and later our
    tab was closed. A closed tab is replaced by the newest tab on the same site
    (or a new one); a shrunken window is re-sized by cdp-server to a size whose
    reported geometry holds up (maximized does not, on this binary). A zoomed page
    goes back to 100%. All of it is logged.
    """
    if page.is_closed() and turn.openers.get(page) is not None and not turn.openers[page].is_closed():
        # A window it followed (a course player, a sign-in) closed: back to the page that opened it.
        page = turn.openers[page]
        turn.repairs.append(f"step {len(turn.steps)}: the window closed; back on {page_url(page)[:80]}")
    if page.is_closed():
        site = site_of(turn.steps[-1]["url"]) if turn.steps else ""
        same_site = [item for item in turn.context.pages if site and site_of(item.url) == site]
        page = same_site[-1] if same_site else turn.context.new_page()
        turn.repairs.append(f"step {len(turn.steps)}: tab was closed; now on {page_url(page)[:80]}")
    # A window the page opened since the last step (a course player, a sign-in
    # popup), maybe a moment after the click: work there. Bringing the opener back
    # to the front instead hid the course player behind it.
    opened = [item for item in turn.context.pages
              if item not in turn.seen_pages and not item.is_closed() and item.opener() == page]
    turn.seen_pages.update(turn.context.pages)
    if opened:
        turn.openers[opened[-1]] = page
        page = opened[-1]
        page.wait_for_load_state("domcontentloaded")
        turn.repairs.append(f"step {len(turn.steps)}: followed the window the page opened: {page_url(page)[:80]}")
    # Screenshots and real input go to whatever tab is in front, and another
    # program sharing the browser (the FSD finder) brings its own tabs there.
    page.bring_to_front()
    try:
        size = page.evaluate("[innerWidth, innerHeight]")
    except Exception:  # noqa: BLE001 - mid-navigation; check again next step
        return page
    if size[0] < 800 or size[1] < 500:
        humanize.settle_window(page)
        humanize.pause(0.5, 1.0)
        turn.repairs.append(f"step {len(turn.steps)}: viewport was {size[0]}x{size[1]}; re-sized the window")
    zoom = humanize.reset_zoom(page)
    if zoom != 1:
        turn.repairs.append(f"step {len(turn.steps)}: page was zoomed to {zoom:.0%}; reset to 100%")
    return page


class Turn:
    """One caller message being worked: its steps, notes, files and cost."""

    def __init__(self, run_id, context, message, conversation, notebook, run_dir, max_stuck_steps, model):
        self.run_id = run_id
        self.context = context
        self.message = message
        self.conversation = conversation
        self.notebook = notebook
        self.run_dir = run_dir
        self.shots_dir = os.path.join(run_dir, "shots")
        self.files_dir = os.path.join(run_dir, "files")
        self.max_stuck_steps = max_stuck_steps
        self.model = model
        # Pages open when the turn began, and windows it followed with their openers
        # (usable_page): only a window the page opens during the turn is followed.
        self.seen_pages = set(context.pages)
        # The last two steps' screenshots, and for each step whether its screen
        # matched the one before (and the one before that): record_screen.
        self.prints = {}
        self.unchanged = {}
        self.openers = {}
        self.steps = []
        self.memory = []
        self.files = []
        self.notes_written = []
        self.repairs = []
        self.sites = []
        self.blocks_seen = []
        self.dom_blocks = []
        self.last_read = ""
        self.last_model_seconds = 0.0
        self.last_model = model
        self.last_reasoning = ""
        self.cost_usd = 0.0
        self.cancelled = False
        self.paused = False
        self.watchdog = Watchdog(self)
        os.makedirs(self.shots_dir, exist_ok=True)
        os.makedirs(self.files_dir, exist_ok=True)

    def write_note(self, note, page_url):
        if not isinstance(note, dict) or not (note.get("text") or "").strip():
            return
        site = (note.get("site") or site_of(page_url) or "general").strip().lower()
        if site != "general":
            site = site_of(site if "://" in site else "https://" + site)
        self.notebook.add_note(site, note["text"].strip(), self.run_id)
        self.notes_written.append({"site": site, "text": note["text"].strip()})


def describe(decision):
    """What an action is about to do, in words a person watching would use."""
    action = (decision.get("action") or "wait").lower()
    text = decision.get("text") or ""
    x, y = decision.get("x"), decision.get("y")
    return {
        "open": f"Opening {text} through Bing",
        "goto": f"Going to {text}",
        "click": f"Clicking at ({x}, {y})",
        "type": f"Typing “{text}”",
        "select": f"Choosing “{text}”",
        "press": f"Pressing {text}",
        "scroll": "Scrolling",
        "hold": f"Pressing and holding at ({x}, {y})",
        "drag": "Dragging the slider",
        "back": "Going back",
        "read": "Reading the page",
        "save_images": "Saving the photos",
        "reply": "Writing the reply",
    }.get(action, "Waiting for the page")


def run_turn(page, turn, on_step, on_phase):
    """Work one caller message until the model replies, the step limit, or cancel.

    Returns (status, reply, page). `on_step` is called after every step and
    `on_phase(phase, text, reason)` as each step moves through looking, thinking
    and acting, so the caller can show progress inside a step that takes a
    minute. While `turn.paused` is set (the user has taken over), the loop holds
    before its next step.
    """
    try:
        return _run_steps(page, turn, on_step, on_phase)
    finally:
        turn.watchdog.stop()


def _run_steps(page, turn, on_step, on_phase):
    failures = 0
    for step in range(RUN_STEP_CAP):
        if turn.paused:
            # The user took over the browser. Hold here, then look again.
            while turn.paused and not turn.cancelled:
                time.sleep(0.5)
            turn.memory.append("The user took over the browser for a while: the page may have "
                               "changed, so look at it again before acting.")
        if turn.cancelled:
            return "cancelled", "Cancelled by the caller.", page
        if not page.is_closed():
            turn.watchdog.arm(page)
        page = usable_page(page, turn)
        turn.watchdog.arm(page)
        on_phase("looking", "Looking at the page", None)
        shot = os.path.join(turn.shots_dir, f"step-{step:02d}.png")
        started = time.time()
        if not safe_screenshot(page, shot):
            # A challenge that is mid-navigation destroys the page context. A
            # press-and-hold overlay in particular can navigate as it arms, and a
            # single retry is not enough: aborting here meant the hold never ran
            # and a beatable challenge was recorded as a failure. Keep trying.
            for wait in (2.5, 4.0, 6.0, 8.0):
                if page.is_closed():
                    break
                humanize.pause(wait, wait + 1.5)
                if safe_screenshot(page, shot):
                    break
            else:
                return "failed", "I could not take a screenshot of the page.", page
            if page.is_closed():
                # The watchdog closed a hung tab; the next step replaces it.
                continue

        shot_done = time.time()
        try:
            turn.dom_blocks = detect_block(page)
        except Exception:  # noqa: BLE001
            turn.dom_blocks = []
        detect_done = time.time()

        on_phase("thinking", "Deciding what to do next", None)
        try:
            decision = decide(page, shot, turn, turn.model)
        except RuntimeError as exc:
            failures += 1
            turn.steps.append({"step": step, "url": page_url(page), "did": f"decide failed: {str(exc)[:200]}"})
            on_step(turn)
            if failures >= 3:
                return "failed", f"The model call kept failing: {str(exc)[:200]}", page
            humanize.pause(1.5, 2.5)
            continue
        failures = 0
        decide_done = time.time()
        if steps_without_progress(turn) >= turn.max_stuck_steps:
            return "step_limit", (f"I stopped: {turn.max_stuck_steps} steps in a row changed nothing "
                                  "on the page."), page

        url = page_url(page)
        site = site_of(url)
        if site and site not in turn.sites:
            turn.sites.append(site)
        blocked = bool(decision.get("blocked")) or bool(turn.dom_blocks)
        block_type = decision.get("block_type") or "none"
        if block_type == "none" and turn.dom_blocks:
            block_type = turn.dom_blocks[0]
        if blocked and block_type != "none":
            turn.blocks_seen.append({"site": site, "type": block_type, "step": step})

        if decision.get("remember"):
            turn.memory.append(str(decision["remember"]).strip())
        turn.write_note(decision.get("note"), url)

        record = {
            "step": step,
            "url": url,
            # The tab the step leaves the run in, set again after the action: `open`
            # can land in a new one, and the console follows the run there.
            "tab": humanize.target_id(page),
            "observation": decision.get("observation"),
            "action": decision.get("action"),
            "x": decision.get("x"),
            "y": decision.get("y"),
            "text": decision.get("text"),
            "blocked": blocked,
            "block_type": block_type,
            "dom_blocks": turn.dom_blocks,
            "reason": decision.get("reason"),
            "thinking": turn.last_reasoning[:4000],
            "model": turn.last_model,
            "screenshot": os.path.relpath(shot, turn.run_dir),
            "seconds": {
                "screenshot": round(shot_done - started, 1),
                "detect": round(detect_done - shot_done, 1),
                "hints": round(decide_done - detect_done - turn.last_model_seconds, 1),
                "model": round(turn.last_model_seconds, 1),
            },
        }

        if types_into_password(page, decision):
            decision = {**decision, "action": "reply", "status": "needs_user",
                        "text": f"Sign in to {site or url} in the viewer, then tell me to carry on."}
            record.update(action="reply", text=decision["text"],
                          reason="a password field: only the user signs in")

        on_phase("acting", describe(decision), decision.get("reason"))
        if (decision.get("action") or "").lower() == "reply":
            status = decision.get("status") or "done"
            if status not in ("done", "needs_input", "needs_user", "failed"):
                status = "done"
            record["did"] = f"reply ({status})"
            turn.steps.append(record)
            on_step(turn)
            return status, decision.get("text") or "", page

        if (decision.get("action") or "").lower() == "open":
            turn.watchdog.arm(page, WATCHDOG_OPEN_SECONDS)
        try:
            record["did"], page = execute(page, decision, turn)
        except Exception as exc:  # noqa: BLE001 - one bad action should not end the turn
            record["did"] = f"action failed: {str(exc)[:200]}"
        record["tab"] = humanize.target_id(page)
        if repeats_without_progress(turn.steps, record):
            # Run 7 lost two sites this way: the same click 20 times over.
            record["did"] += (" (the same action at the same place as your last two steps, on the "
                              "same page; it is not working, so do something different)")
        record["seconds"]["action"] = round(time.time() - decide_done, 1)
        turn.watchdog.disarm()
        turn.steps.append(record)
        on_step(turn)
    return "step_limit", f"I stopped after {RUN_STEP_CAP} steps, the most one message gets.", page


def repeats_without_progress(steps, record):
    """True when this step and the two before it are one action at one spot on one URL."""
    if len(steps) < 2 or record["action"] in ("wait", "read", "scroll", None):
        return False

    def same(step):
        # A step whose model call failed has no action; it ended one site's run with KeyError.
        return (step["url"] == record["url"] and step.get("action") == record["action"]
                and step.get("text") == record.get("text")
                and all(a is None and b is None or a is not None and b is not None and abs(a - b) <= 12
                        for a, b in ((step.get("x"), record.get("x")), (step.get("y"), record.get("y")))))
    return same(steps[-1]) and same(steps[-2])


def reflect(turn, status, reply):
    """After a turn that hit trouble, ask the model what to remember next time.

    The model writes notes as it goes, but under pressure it often does not. One
    text-only call at the end turns the step log into a lesson per site. Runs that
    went smoothly are skipped: there is nothing to learn and it saves a call.
    """
    trouble = status != "done" or turn.blocks_seen or any(
        "failed" in (step.get("did") or "") or "refused" in (step.get("did") or "")
        for step in turn.steps
    )
    if not trouble or not turn.sites:
        return
    log = [
        {key: step.get(key) for key in ("step", "url", "observation", "did", "block_type")}
        for step in turn.steps
    ]
    existing = "\n\n".join(f"{site}:\n{turn.notebook.notes_for(site)}" for site in turn.sites)
    prompt = (
        f"You are Stealthbox reviewing your own browser run, to keep notes for next "
        f"time.\nCaller's message: {turn.message}\nOutcome: {status}: {reply}\n"
        f"Steps:\n{json.dumps(log)}\n\nNotes you already have:\n{existing or '(none)'}\n\n"
        f"Write only lessons that are new and would save time on a future run: a bot "
        f"check and what cleared it (or that nothing did), a page that misbehaved, "
        f"where a feature lives on the site. One short sentence each. Return "
        f'{{"notes": [{{"site": "<domain>", "text": "..."}}]}}, or {{"notes": []}} '
        f"if there is nothing new."
    )
    raw, cost, _reasoning = chat([{"role": "user", "content": prompt}], model=turn.model, json_mode=True)
    turn.cost_usd += cost
    try:
        notes = json.loads(raw).get("notes") or []
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"reflect got unparseable model output: {raw[:200]!r}") from exc
    for note in notes:
        turn.write_note(note, "")
