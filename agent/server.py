"""Stealthbox's chat API. Runs inside the container, beside the browser.

    POST /v1/chat                    {"message": "...", "session": null, "max_stuck_steps": 12, "wait": true}
                                     a run stops when max_stuck_steps steps in a row change nothing
                                     "tab": a page target id instead of "session" starts a session
                                     in that open tab (left open when the session ends)
    GET  /v1/runs/{run}              status, reply, steps, files, cost
    POST /v1/runs/{run}/cancel
    GET  /v1/runs/{run}/files/{path} a file the run saved (photos)
    GET  /v1/runs/{run}/shots/{path} a step screenshot
    GET  /v1/notes                   sites with notes
    GET  /v1/notes/{site}            the notes and run log for one site
    POST /v1/smoke                   {"sites": [...], "wait": true}: reach a few sites
                                     and screenshot them (report.html in <data>/smoke/)
    GET  /v1/smoke/{id}
    POST /v1/runs/{run}/pause        hold the run after its current step and bring its tab to the front, so the user can drive
    POST /v1/runs/{run}/resume
    GET  /v1/sessions                every session: status, latest screenshot, the conversation
    POST /v1/sessions                a blank session: a browser of its own, no task; message it later
    GET  /v1/sessions/{session}
    POST /v1/sessions/{session}/focus  bring its tab to the front, so the live view shows it
    GET  /v1/browser/screenshot      a JPEG of the main browser's display, for the console's preview;
                                     ?width= up to 1920 (default 320), ?session= that session's browser
    GET  /v1/browser/history?q=      addresses from the browser's history, for the address bar
    POST /v1/browser/close           {"tab": id}: close a tab (the console's Browser entry); a
                                     session's tab closes its session
    POST /v1/browser/navigate        {"url": "...", "session": null}: load what was typed in the
                                     console's address bar in the session's tab, or the tab in front
    POST /v1/sessions/{session}/close  close its tab and forget it
    POST /v1/sessions/{session}/archive    hide it from the console's list (tab and conversation stay);
    POST /v1/sessions/{session}/unarchive  a message to it, or a run that needs the user, brings it back
    POST /v1/share                   an HTTPS link to the console (a Cloudflare quick tunnel)
    POST /v1/unshare                 stop it; the devices signed in through it are signed out
    POST /v1/password-link           {"share": false}: a one-time link to set the console password
    GET  /v1/devices                 browsers signed in to the console
    POST /v1/devices/{device}/sign-out
    POST /v1/devices/sign-out-everywhere
    GET  /console                    the web console, or its sign-in page
    POST /console/sign-in            the console password -> a device cookie for 30 days
    POST /v1/password                {"current", "password", "again"}: change it from a signed-in console
    GET  /password?code=             set the console password (one-time link; works through the share)
    GET  /viewer/...                 the live browser (noVNC), for the console
    GET  /v1/status
    GET  /v1/events                  Server-Sent Events: every state change, in order.
                                     ?run= or ?session= filters; resume with Last-Event-ID
                                     (or ?since=<id>; ?since=0 replays what is still held)
    GET  /v1/events/next?since=<id>  the same events as a long poll (waits up to 25 s);
                                     without since, returns the current last_id at once
    GET  /v1/health                  no token; for the container health check

Every route but /v1/health needs `Authorization: Bearer <token>`; the token is
<data>/api-token, created on first start. The console instead carries a device
cookie, which signing in with the console password gives out (console_auth). A
session is a browser tab and the conversation in it. A browser has one mouse, so
each session works in a browser of its own (cdp-server starts one per session, on
its own X display), and sessions run at the same time. A tab a caller lends is in
the main browser, the one other CDP clients share. Each browser's runs go through
its own worker thread, the only thread that touches that browser's Playwright.

Every state change (a run queued, started, stepped or finished; a session created
or expired; the browser connecting; a share; a smoke test; the key setup) is
published once to the event stream, so a caller or a dashboard subscribes once
instead of polling.

A run that ends `needs_user` (a sign-in, a payment, a check only the user can
clear) starts the share itself, and carries a console link to that session under
`needs_user`, so the user can finish the step from any machine and carry on. With
no console password yet, that link sets one first.
"""

from __future__ import annotations

import collections
import hmac
import itertools
import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import sync_playwright
from websockets.exceptions import WebSocketException

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import steps as agent  # noqa: E402
import console_auth  # noqa: E402
import key_setup  # noqa: E402
import smoke  # noqa: E402
from notebook import Notebook  # noqa: E402

DATA_DIR = agent.DATA_DIR
HOST_DATA_DIR = os.environ.get("STEALTHBOX_HOST_DATA_DIR", "")
PORT = int(os.environ.get("STEALTHBOX_API_LISTEN_PORT", "8423"))
HOST_PORT = os.environ.get("STEALTHBOX_API_HOST_PORT") or str(PORT)
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
VIEWER_PORT = 6080
# websockify for the session browsers' displays, each under its browser's key (cdp-server
# writes the tokens); internal to the container, reached through /viewer/sessions/<key>/.
SESSION_VIEWER_PORT = 6081
MAIN = "main"
MAIN_DISPLAY = os.environ.get("DISPLAY", ":99")
# Session browsers open at once; each session works in one of its own. At the limit,
# the least recently used finished session gives its browser to a new one.
SESSION_BROWSERS = int(os.environ.get("STEALTHBOX_SESSION_BROWSERS", "3"))
# Copying the profile and starting Chrome on a new display: seconds on a fast machine.
SESSION_BROWSER_START_SECONDS = 180
EXPIRY_CHECK_SECONDS = 60
# How often an idle session browser's worker checks that its Chrome is still running.
QUIT_CHECK_SECONDS = 3
# Put in a browser's queue to close it after the work ahead of it.
CLOSE = object()
CONSOLE_PAGE = os.path.join(HERE, "console.html")
CONSOLE_COOKIE = "stealthbox_console"
PASSWORD_FILE = os.path.join(DATA_DIR, "console-password")
DEVICES_FILE = os.path.join(DATA_DIR, "console-devices.json")
SESSION_ID = re.compile(r"^s_[A-Za-z0-9_-]+$")
# cloudflared's metrics in the `share` service: its address, and whether it is routable.
SHARE_METRICS = os.environ.get("STEALTHBOX_SHARE_METRICS", "http://share:20241")
SHARE_URL_SECONDS = 60
# The console's live preview of the browser, in the sidebar.
PREVIEW_WIDTH = 320
MAX_PREVIEW_WIDTH = 1920
BROWSER_START_SECONDS = 300
SESSION_IDLE_SECONDS = int(os.environ.get("STEALTHBOX_SESSION_IDLE_SECONDS", "1800"))
# How long a run waits for a tab handed to it ("tab") to reach Playwright.
BORROW_SECONDS = 10
# Steps in a row that change nothing on the page before a run gives up.
DEFAULT_MAX_STUCK_STEPS = 12
MAX_STUCK_STEPS_LIMIT = 100
EVENTS_HELD = 5000
EVENTS_KEEPALIVE_SECONDS = 15
# Under Cloudflare's 100-second limit for a response to start.
EVENTS_POLL_SECONDS = 25
# A reader that stops reading blocks its own thread on write; this frees it.
EVENTS_WRITE_TIMEOUT_SECONDS = 30


def new_id(prefix):
    return f"{prefix}_{time.strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(3)}"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Events:
    """The global event stream: every state change, in order.

    Publishing never waits on a reader. Events go into a bounded buffer, and each
    reader walks it from the id it last saw, at its own pace. A reader that falls
    further behind than the buffer holds is told so by a `stream.gap` event, then
    carries on from the oldest event still held.
    """

    def __init__(self, held=EVENTS_HELD):
        self.held = collections.deque(maxlen=held)
        # Ids start at the start time in milliseconds, so they keep rising across
        # restarts: a reader resuming with an id from before a restart gets a gap,
        # not a resume into unrelated events that reuse its number.
        self.next_id = int(time.time() * 1000)
        self.readers = 0
        self.changed = threading.Condition()

    def publish(self, kind, **data):
        with self.changed:
            event = {"id": self.next_id, "type": kind, "time": now(), **data}
            self.next_id += 1
            self.held.append(event)
            self.changed.notify_all()
        return event

    def last_id(self):
        with self.changed:
            return self.next_id - 1

    def after(self, last_id, timeout):
        """Events newer than `last_id`, waiting up to `timeout` for the first one.

        Returns (events, gap): `gap` is True when events after `last_id` have
        already left the buffer.
        """
        with self.changed:
            self.changed.wait_for(lambda: self.next_id - 1 > last_id, timeout)
            if not self.held:
                return [], False
            first = self.held[0]["id"]
            start = max(last_id + 1 - first, 0)
            return list(itertools.islice(self.held, start, None)), last_id + 1 < first

    def reader(self, delta):
        with self.changed:
            self.readers += delta


def step_event(run, step):
    """A step as published: the step record, its screenshot URL, and any files it saved."""
    published = {key: value for key, value in step.items() if key != "screenshot"}
    if step.get("screenshot"):
        published["shot_url"] = f"/v1/runs/{run.id}/{step['screenshot']}"
    return published


class Share:
    """The console's share link: a Cloudflare quick tunnel run by the `share` service
    (docker-compose.yml) in a container of its own, so restarting or updating
    Stealthbox keeps its address, and with it every device signed in through it
    (a device cookie belongs to one address). `stealthbox share` starts it and
    `stealthbox unshare` stops it, on the host; this reads its address from
    cloudflared's metrics. It reaches this API (console, live viewer, routes), never
    CDP, and the console asks for the password.
    """

    def __init__(self, events):
        self.events = events

    def url(self):
        """The share's base URL, or None when it is not running or not yet routable."""
        try:
            # /ready answers 503 until the edge can route the address.
            with urllib.request.urlopen(f"{SHARE_METRICS}/ready", timeout=2):
                pass
            with urllib.request.urlopen(f"{SHARE_METRICS}/quicktunnel", timeout=2) as response:
                host = json.load(response)["hostname"]
        except OSError:
            # Not started (its name does not resolve), starting, or stopped.
            return None
        return f"https://{host}" if host else None

    def wait(self):
        """The share's base URL once it is up; `stealthbox share` has just started it."""
        deadline = time.time() + SHARE_URL_SECONDS
        while (url := self.url()) is None:
            if time.time() > deadline:
                raise RuntimeError("Nothing is being shared. Start it with `stealthbox share` "
                                   "on the computer running Stealthbox.")
            time.sleep(0.5)
        return url

    def active_url(self):
        url = self.url()
        return f"{url}/console" if url else None


class Session:
    def __init__(self, tab=None, browser=None):
        self.id = new_id("s")
        self.page = None
        self.target_id = tab
        # A tab the caller already had open in the main browser (its own scraper tab,
        # say): the run works in it, and closing or expiring the session leaves it open.
        self.borrowed = tab is not None
        # The browser it works in: the main one for a borrowed tab, otherwise one of its
        # own, which an idle session gives up to a newer one (Stealthbox._give_browser)
        # and gets back, new, with its next message.
        self.browser = browser
        # Work for a browser it does not have yet (runs, a blank session's first tab), in order.
        self.pending = []
        self.url = None
        self.conversation = []
        self.runs = []
        self.last_used = time.time()
        self.archived = False

    @property
    def busy(self):
        return any(run.status in ("queued", "running") for run in self.runs)

    def state(self):
        """One word for the console: new, needs, working, paused, waiting, done or stopped."""
        running = next((run for run in self.runs if run.status == "running"), None)
        if running:
            return "paused" if running.paused else "working"
        if any(run.status == "queued" for run in self.runs):
            return "waiting"
        if not self.runs:
            return "new"
        last = self.runs[-1]
        if last.status in ("needs_user", "needs_input"):
            return "needs"
        if last.status == "cancelled":
            return "stopped"
        return "done"

    def view(self, share, front_tab, waiting_for_browser):
        """`front_tab` is the tab in front in the session's browser; `waiting_for_browser`
        says it waits for a browser to free up."""
        last = self.runs[-1] if self.runs else None
        shot = None
        for run in reversed(self.runs):
            steps = [step for step in (run.turn.steps if run.turn else []) if step.get("screenshot")]
            if steps:
                shot = f"/v1/runs/{run.id}/{steps[-1]['screenshot']}"
                break
        messages = []
        for run in self.runs:
            view = run.view(full=True)
            messages.append({"from": "you", "text": run.message, "time": run.started})
            messages.append({
                "from": "stealthbox", "run": run.id, "status": run.status, "paused": run.paused,
                "phase": view["phase"],
                "text": run.reply, "time": run.finished, "cost_usd": view["cost_usd"],
                "files": view["files"],
                "steps": [{"observation": step.get("observation"), "did": step.get("did"),
                           "reason": step.get("reason"), "thinking": step.get("thinking"),
                           "seconds": round(sum((step.get("seconds") or {}).values()), 1)}
                          for step in view.get("trace", [])],
            })
        state = self.state()
        browser = self.browser
        in_front = bool(front_tab and self.target_id == front_tab["id"])
        return {
            "session": self.id,
            "state": state,
            "site": agent.site_of(self.url) or None,
            "url": self.url,
            "tab": self.target_id,
            "shot_url": shot,
            "in_front": in_front,
            # The address its tab is on now (the user may have browsed since the run).
            "live_url": front_tab["url"] if in_front else None,
            # Where its live view and preview come from: the main browser (a borrowed
            # tab), its own browser, or none right now (it gets one with its next message).
            "browser": None if browser is None else {
                "kind": "main" if browser.main else "own",
                "open": browser.ready,
                # Not open and not opening: its Chrome quit (its last tab closed); the
                # session's next message opens it again.
                "opening": browser.opening,
                "viewer": browser.viewer,
                "preview": "/v1/browser/screenshot" + ("" if browser.main else f"?session={self.id}"),
            },
            "waiting_for_browser": waiting_for_browser,
            "archived": self.archived,
            "task": last.reply if state == "needs" else None,
            "needs_user": last.needs_user if state == "needs" else None,
            "running_run": next((run.id for run in self.runs if run.status in ("running", "queued")), None),
            "stopping": any(run.cancel_requested for run in self.runs if run.status in ("running", "queued")),
            "last_used": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.last_used)),
            "messages": messages,
        }


class Run:
    def __init__(self, session, message, max_stuck_steps):
        self.id = new_id("r")
        self.session = session
        self.message = message
        self.max_stuck_steps = max_stuck_steps
        self.dir = os.path.join(DATA_DIR, "runs", self.id)
        self.status = "queued"
        self.reply = None
        self.turn = None
        self.final_url = None
        self.needs_user = None
        self.paused = False
        self.phase = None
        self.error = None
        self.started = now()
        self.finished = None
        self.cancel_requested = False
        self.files_published = 0
        self.done = threading.Event()

    def view(self, full=False):
        turn = self.turn
        files = []
        if turn:
            for path in turn.files:
                entry = {"path": path, "url": f"/v1/runs/{self.id}/files/{path}"}
                if HOST_DATA_DIR:
                    entry["host_path"] = os.path.join(HOST_DATA_DIR, "runs", self.id, "files", path)
                files.append(entry)
        out = {
            "run": self.id,
            "session": self.session.id,
            "status": self.status,
            "message": self.message,
            "reply": self.reply,
            "files": files,
            "host_dir": os.path.join(HOST_DATA_DIR, "runs", self.id) if HOST_DATA_DIR else None,
            "final_url": self.final_url,
            "needs_user": self.needs_user,
            "paused": self.paused,
            "phase": self.phase if self.status == "running" else None,
            "steps": len(turn.steps) if turn else 0,
            "sites": turn.sites if turn else [],
            "blocks": turn.blocks_seen if turn else [],
            "memory": turn.memory if turn else [],
            "notes_written": turn.notes_written if turn else [],
            "repairs": turn.repairs if turn else [],
            "cost_usd": round(turn.cost_usd, 5) if turn else 0.0,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
        }
        if full and turn:
            out["trace"] = turn.steps
        elif turn:
            out["trace"] = [
                f"[{step['step']}] {step.get('did', '')}"
                + (f" [bot check: {step['block_type']}]" if step.get("blocked") else "")
                for step in turn.steps
            ]
        return out

    def persist(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, "run.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(self.view(full=True), fh, indent=2)
        os.replace(tmp, os.path.join(self.dir, "run.json"))


def cdp_server(path):
    """POST to cdp-server, which starts and stops the browsers, beside CDP on the same port."""
    request = urllib.request.Request(f"{CDP_URL}{path}", data=b"{}", headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=SESSION_BROWSER_START_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"cdp-server {path} failed ({error.code}): {error.read().decode()[:300]}") from None


def cookie_key(cookie):
    return cookie["name"], cookie["domain"], cookie["path"]


class Browser:
    """One Chrome, the X display it draws on, and the worker thread that drives it.

    The main browser is the saved profile on the main display, shared with other CDP
    clients: a run in a tab a caller lends, smoke tests and setup pages work there.
    Every other session gets a browser of its own from cdp-server, so sessions work
    at the same time, each with its own mouse, keyboard and live view. A session's
    browser starts from a copy of the main profile; the cookies it sets go back to
    the main browser after each run and when it closes.
    """

    def __init__(self, stealthbox, key):
        self.stealthbox = stealthbox
        self.key = key
        self.main = key == MAIN
        self.cdp_url = CDP_URL if self.main else f"{CDP_URL}/sessions/{key}"
        # A session browser's display is known once cdp-server has opened it.
        self.display = MAIN_DISPLAY if self.main else None
        self.viewer = "/viewer/websockify" if self.main else f"/viewer/sessions/{key}/websockify"
        self.queue = queue.Queue()
        self.connection = None
        self.current = None
        # Live views connected to it (the console's): a browser someone watches is not closed.
        self.viewers = 0
        self.opening = not self.main
        self.closing = False
        # A session browser's cookies as last copied to the main browser, by cookie_key.
        self.cookies = {}
        self.thread = threading.Thread(target=self.work, name=f"browser-{key}", daemon=True)

    @property
    def ready(self):
        return self.connection is not None and self.connection.is_connected()

    def close(self):
        """Close a session's browser once its worker is free: copy its cookies, then stop it."""
        self.closing = True
        self.queue.put(CLOSE)

    def front_tab(self):
        """The tab its live view shows, or None while the browser is not up."""
        if not self.ready or self.display is None:
            return None
        try:
            return agent.front_tab(self.cdp_url, self.display)
        except (OSError, subprocess.SubprocessError):
            # The browser or the display went away since the check above.
            return None

    def work(self):
        playwright = sync_playwright().start()
        if self.main:
            # Connect now, not on the first message: cdp-server launches Chrome on its
            # first connection, which can take minutes on a cold start. It also starts
            # after this server, so keep trying for a while; after that a run retries.
            deadline = time.time() + BROWSER_START_SECONDS
            while True:
                try:
                    self.connect(playwright)
                    break
                except (PlaywrightError, OSError, WebSocketException) as exc:
                    if time.time() > deadline:
                        print(f"browser not reachable, the first run will retry: {exc}", file=sys.stderr, flush=True)
                        break
                    time.sleep(2)
        else:
            try:
                self.connect(playwright)
            except Exception:  # noqa: BLE001 - its first run tries again, and says why if it fails
                traceback.print_exc()
        try:
            while True:
                try:
                    item = self.queue.get(timeout=QUIT_CHECK_SECONDS)
                except queue.Empty:
                    self._notice_quit()
                    continue
                if item is CLOSE:
                    return
                self.stealthbox.work_on(self, playwright, item)
        finally:
            self._shut(playwright)

    def _notice_quit(self):
        """A session's Chrome quits with its last tab. Playwright on this thread hears of it
        only on its next call, so ask cdp-server, and let the console say it closed."""
        if self.main or self.connection is None:
            return
        try:
            urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=5).close()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                self.connection = None
                self.stealthbox.events.publish("browser.closed", browser=self.key)
        except OSError:
            # cdp-server busy or restarting: ask again next time.
            pass

    def connect(self, playwright):
        """The browser's context: a session's browser is opened first, and either is
        connected again if the connection dropped (Chrome restarted, say)."""
        if not self.ready:
            self.opening = True
            try:
                return self._connect(playwright)
            finally:
                self.opening = False
        return self.connection.contexts[0]

    def _connect(self, playwright):
        """Open (a session's) and connect; under connect(), which marks it opening."""
        reconnect = self.connection is not None
        if not self.main:
            # Opens it, or finds it running; one whose Chrome quit (its last tab
            # closed) starts again from a fresh copy of the main profile.
            self.display = cdp_server(f"/sessions/{self.key}/open")["display"]
        closed = agent.close_unresponsive_tabs(self.cdp_url)
        if closed:
            self.stealthbox.events.publish("browser.hung_tabs_closed", browser=self.key, urls=closed)
        self.connection = playwright.chromium.connect_over_cdp(self.cdp_url)
        if not self.main:
            self.cookies = {cookie_key(cookie): cookie for cookie in agent.browser_cookies(self.cdp_url)}
        self.stealthbox.events.publish("browser.connected", browser=self.key, reconnect=reconnect)
        # Pages from an old connection are dead.
        for session in self.stealthbox.sessions_in(self):
            if session.page is not None:
                self.stealthbox.events.publish("session.tab_lost", session=session.id)
            session.page = None
        return self.connection.contexts[0]

    def save_cookies(self):
        """Copy the cookies this session's browser set or changed since the last copy to
        the main browser, so a sign-in made in a session lasts. Ones it deleted stay there."""
        cookies = agent.browser_cookies(self.cdp_url)
        changed = [cookie for cookie in cookies if self.cookies.get(cookie_key(cookie)) != cookie]
        if changed:
            agent.set_cookies(CDP_URL, changed)
            self.stealthbox.events.publish("browser.cookies_saved", browser=self.key, cookies=len(changed))
        self.cookies = {cookie_key(cookie): cookie for cookie in cookies}

    def _shut(self, playwright):
        try:
            if not self.main and self.ready:
                self.save_cookies()
        except Exception:  # noqa: BLE001 - a browser that is closing still has to close
            traceback.print_exc()
        finally:
            if not self.main:
                try:
                    cdp_server(f"/sessions/{self.key}/close")
                except (OSError, RuntimeError):
                    # cdp-server closes a browser nobody is connected to after its idle timeout.
                    traceback.print_exc()
            playwright.stop()
            self.stealthbox.browser_closed(self)


class Stealthbox:
    """Sessions, the browsers they work in, and the runs waiting for a browser."""

    def __init__(self):
        self.events = Events()
        self.share = Share(self.events)
        self.notebook = Notebook(DATA_DIR)
        self.sessions = {}
        self.runs = {}
        self.lock = threading.Lock()
        self.main = Browser(self, MAIN)
        # Session browsers open, opening or closing, by key: each counts against
        # SESSION_BROWSERS until it has closed.
        self.browsers = {}
        # Sessions with work and no browser, waiting for one, oldest first.
        self.waiting = []
        self.smokes = {}
        self.setup_codes = key_setup.SetupCodes()
        self.password_codes = key_setup.SetupCodes()
        self.password = console_auth.Password(PASSWORD_FILE)
        self.devices = console_auth.Devices(DEVICES_FILE)

    def start(self):
        self.main.thread.start()
        threading.Thread(target=self._expire_sessions, name="session-expiry", daemon=True).start()

    # ---- called from HTTP threads

    def submit(self, message, session_id, max_stuck_steps, tab=None):
        if tab and session_id:
            raise ValueError("a tab starts a new session; leave out session")
        if not self._has_key():
            raise PermissionError("No OpenRouter key yet. Run `stealthbox key` and paste it "
                                  "into the page it opens.")
        with self.lock:
            if session_id:
                session = self.sessions.get(session_id)
                if session is None:
                    raise LookupError(f"no session {session_id} (it may have expired)")
                if session.archived:
                    self._archive(session, False)
            else:
                # A borrowed tab is the main browser's; any other session gets its own.
                session = Session(tab, self.main if tab else None)
                self.sessions[session.id] = session
                self.events.publish("session.created", session=session.id)
            session.last_used = time.time()
            # A message to a busy session queues behind its current run.
            run = Run(session, message, max_stuck_steps)
            session.runs.append(run)
            self.runs[run.id] = run
            # Published before a worker can see the run, so `run.queued` always
            # comes before that run's `run.started`.
            self.events.publish("run.queued", run=run.id, session=session.id, message=message,
                                max_stuck_steps=max_stuck_steps)
            if session.browser is None:
                self._wait_for_browser(session, run)
            else:
                session.browser.queue.put(run)
        return run

    def cancel(self, run):
        run.cancel_requested = True
        if run.turn is not None:
            run.turn.cancelled = True
        self.events.publish("run.cancel_requested", run=run.id, session=run.session.id)
        session = run.session
        with self.lock:
            if run not in session.pending:
                return
            # Waiting for a browser: it ends here, with no browser opened for it.
            session.pending.remove(run)
            if not session.pending and session in self.waiting:
                self.waiting.remove(session)
        run.status = "cancelled"
        run.reply = "Cancelled before it started."
        self._finished(run)

    def pause(self, run, paused):
        """Hold a running run after its current step (the user takes over), or let it go on."""
        if run.status != "running" or run.turn is None:
            raise RuntimeError(f"run {run.id} is {run.status}; only a running run can pause")
        if paused:
            # The user takes over in the live view, which shows the tab in front.
            agent.activate_tab(run.session.browser.cdp_url, run.session.target_id)
        run.paused = paused
        run.turn.paused = paused
        self.events.publish("run.paused" if paused else "run.resumed", run=run.id, session=run.session.id)

    def _sessions(self):
        """A copy of the sessions: HTTP threads add and remove them while others read."""
        with self.lock:
            return list(self.sessions.values())

    def sessions_in(self, browser):
        return [session for session in self._sessions() if session.browser is browser]

    def session(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise LookupError(f"no session {session_id} (it may have expired or been closed)")
        return session

    def browser(self, key):
        """A session browser by its key (for its live view)."""
        with self.lock:
            browser = self.browsers.get(key)
        if browser is None:
            raise LookupError(f"no browser {key} (its session may have closed)")
        return browser

    def sessions_view(self):
        with self.lock:
            sessions = sorted(self.sessions.values(), key=lambda s: s.last_used, reverse=True)
            waiting = set(self.waiting)
        fronts = {}
        for session in sessions:
            if session.browser is not None and session.browser.key not in fronts:
                fronts[session.browser.key] = session.browser.front_tab()
        return [session.view(self.share, fronts.get(session.browser.key) if session.browser else None,
                             session in waiting)
                for session in sessions]

    def session_view(self, session):
        with self.lock:
            waiting = session in self.waiting
        return session.view(self.share, session.browser.front_tab() if session.browser else None, waiting)

    def navigate(self, text, session):
        """Load what the user typed in the console's address bar: in the session's tab, or
        with no session in the main browser's tab in front. Not in a tab a run is driving:
        the run would work on a page it did not choose."""
        if session is None:
            browser = self.main
            tab = browser.front_tab()
            if tab is None:
                raise LookupError("no tab is open")
            target = tab["id"]
            with self.lock:
                session = next((s for s in self.sessions.values()
                                if s.browser is browser and s.target_id == target), None)
        else:
            browser, target = session.browser, session.target_id
            if browser is None or target is None:
                raise LookupError(f"session {session.id} has no tab yet")
        if session is not None and session.state() in ("working", "waiting"):
            raise RuntimeError("Stealthbox is working in this tab; stop or pause the run first")
        url = agent.typed_url(text)
        agent.navigate_tab(browser.cdp_url, target, url)
        if session is not None:
            session.url = url
            session.last_used = time.time()
        self.events.publish("browser.navigated", browser=browser.key, tab=target, url=url)
        return url

    def focus(self, session):
        """Bring the session's tab to the front of its browser, after the run in progress if
        there is one. A paused run holds the browser's thread until the user hands back, so
        its tab comes to the front at once."""
        session.last_used = time.time()
        browser = session.browser
        if browser is None or not browser.ready:
            raise LookupError(f"session {session.id} has no browser open; message it to get one")
        if session.target_id and session.target_id not in {tab["id"] for tab in agent.page_targets(browser.cdp_url)}:
            raise LookupError(f"session {session.id}'s tab was closed")
        if session.state() == "paused":
            agent.activate_tab(browser.cdp_url, session.target_id)
            self.events.publish("session.focused", session=session.id)
            return
        def chore(context):
            page = self._session_page(context, session)
            if page is None:
                self.events.publish("session.tab_lost", session=session.id)
                return
            page.bring_to_front()
            self.events.publish("session.focused", session=session.id)
        browser.queue.put(chore)

    @staticmethod
    def _session_page(context, session):
        """On the browser's thread: the session's open page. After the browser reconnects
        the old page object is dead, so find its tab again by target id."""
        if session.page is None or session.page.is_closed():
            session.page = next((page for page in context.pages
                                 if session.target_id and agent.humanize.target_id(page) == session.target_id), None)
        return session.page

    def open_blank(self):
        """A session with no task yet, for the user to browse or message later, in a browser
        of its own (once one is free): the tab it opens with is the session's."""
        session = Session()

        def chore(context):
            if session.id not in self.sessions:
                # Closed before its browser opened.
                return
            session.page = context.pages[0] if context.pages else context.new_page()
            session.target_id = agent.humanize.target_id(session.page)
            session.page.bring_to_front()
            self.events.publish("session.tab_opened", session=session.id, tab=session.target_id)
            self.events.publish("session.focused", session=session.id)
        with self.lock:
            self.sessions[session.id] = session
            self.events.publish("session.created", session=session.id)
            self._wait_for_browser(session, chore)
        return session

    def archive(self, session, archived):
        with self.lock:
            session.last_used = time.time()
            self._archive(session, archived)

    def _archive(self, session, archived):
        """Under self.lock. Hidden from the console's list; the tab and conversation stay."""
        if session.archived == archived:
            return
        session.archived = archived
        self.events.publish("session.archived" if archived else "session.unarchived", session=session.id)

    def close(self, session):
        """Forget the session. Its own browser closes (its cookies kept); a borrowed tab stays open."""
        with self.lock:
            if session.busy:
                raise RuntimeError(f"session {session.id} is still working; stop its run first")
            # Two consoles can close it at once; the second finds it gone.
            if self.sessions.pop(session.id, None) is None:
                raise LookupError(f"no session {session.id} (it was closed)")
            self._forget(session)
        self.events.publish("session.closed", session=session.id)

    def close_tab(self, target):
        """Close a main-browser tab from the console's Browser entry, which stands for a tab
        no listed session shows: another program's, one opened by hand, or an archived
        session's (closed with it). Chrome quits with its last tab, so a blank one takes its
        place first. Under the lock, so two consoles cannot both close the last two tabs."""
        browser = self.main
        with self.lock:
            owner = next((s for s in self.sessions.values() if s.browser is browser and s.target_id == target), None)
            if owner is not None and not owner.archived:
                # The console's tab id is a refresh old; the user may have switched tabs since.
                raise RuntimeError(f"that tab is session {owner.id}'s now; close it from its row")
            if owner is not None and owner.busy:
                raise RuntimeError(f"session {owner.id} is still working in that tab; stop its run first")
            tabs = agent.page_targets(browser.cdp_url)
            if target not in {tab["id"] for tab in tabs}:
                raise LookupError(f"no tab {target} (it may have closed already)")
            if len(tabs) == 1:
                agent.open_tab(browser.cdp_url, background=False)
            agent.close_target(browser.cdp_url, target)
            if owner is not None:
                del self.sessions[owner.id]
        if owner is not None:
            self.events.publish("session.closed", session=owner.id)
        self.events.publish("browser.tab_closed", tab=target)

    def submit_smoke(self, sites):
        job = smoke.Smoke(sites, HOST_DATA_DIR)
        with self.lock:
            self.smokes[job.id] = job
        self.events.publish("smoke.queued", smoke=job.id, sites=job.sites)
        self.main.queue.put(job)
        return job

    def status(self):
        with self.lock:
            browsers = [self.main, *self.browsers.values()]
            waiting = sum(len(session.pending) for session in self.waiting)
        return {
            "browser_connected": self.main.ready,
            "model": agent.DEFAULT_MODEL,
            "openrouter_key": self._has_key(),
            "key_setup": self.setup_codes.status(),
            "queue_depth": waiting + sum(browser.queue.qsize() for browser in browsers),
            # Read once: a worker clears it when its run ends.
            "running": [run.id for browser in browsers if (run := browser.current)],
            "session_browsers": {"open": len(browsers) - 1, "limit": SESSION_BROWSERS},
            "front_tab": self.main.front_tab(),
            "share_url": self.share.active_url(),
            "console_password": self.password.is_set(),
            "password_setup": self.password_codes.status(),
            "last_event_id": self.events.last_id(),
            "event_readers": self.events.readers,
            "sessions": [
                {"session": session.id, "turns": len(session.conversation) // 2,
                 "idle_seconds": int(time.time() - session.last_used), "busy": session.busy,
                 "browser": session.browser.key if session.browser else None}
                for session in self._sessions()
            ],
        }

    def setup_link(self, in_browser):
        """A one-time key setup URL. In-browser, it opens in a new tab of Stealthbox's
        own browser, for a user who is reaching the machine through the viewer."""
        code = self.setup_codes.issue()
        self.events.publish("key.issued", in_browser=in_browser)
        if in_browser:
            url = f"http://127.0.0.1:{PORT}/setup?code={code}&in_browser=1"
            self.main.queue.put(lambda context: context.new_page().goto(url))
        else:
            url = f"http://127.0.0.1:{HOST_PORT}/setup?code={code}"
        return {"setup_url": url, "in_browser": in_browser, "openrouter_key": self._has_key(),
                "expires_in_seconds": key_setup.CODE_SECONDS}

    def share_link(self, session_id=None):
        """The link to send the user, once the share is up: the console, on `session_id` if
        given. With no console password yet, a one-time link that sets one first."""
        base = self.share.wait()
        if self.password.is_set():
            url = f"{base}/console" + (f"#{session_id}" if session_id else "")
        else:
            url = self.password_link(base, session_id)
        return {"url": url, "local_url": f"http://127.0.0.1:{HOST_PORT}/console",
                "console_password": self.password.is_set()}

    def password_link(self, base, session_id=None):
        """A one-time link to set the console password, on `base` (the share, or localhost)."""
        code = self.password_codes.issue()
        self.events.publish("password.issued")
        return f"{base}/password?code={code}" + (f"&session={session_id}" if session_id else "")

    def unshare(self):
        """Sign out the devices that used the share; the host then stops its container."""
        url = self.share.url()
        if url is None:
            return False
        self.devices.sign_out(host=urlparse(url).netloc)
        self.events.publish("share.stopped")
        return True

    def set_password(self, password):
        self.password.set(password)
        self.devices.sign_out()
        self.events.publish("password.set")

    def password_progress(self, code, state, error=None):
        self.password_codes.note(code, state, error)
        self.events.publish(f"password.{state}", **({"error": error} if error else {}))

    def key_progress(self, code, state, error=None):
        """Record what happened to a setup link, and publish it. Never the key itself."""
        self.setup_codes.note(code, state, error)
        self.events.publish(f"key.{state}", **({"error": error} if error else {}))

    @staticmethod
    def _has_key():
        try:
            agent.api_key()
            return True
        except RuntimeError:
            return False

    # ---- the browsers' worker threads

    def work_on(self, browser, playwright, item):
        """On `browser`'s worker thread: one run, smoke test or chore."""
        if isinstance(item, smoke.Smoke):
            self.events.publish("smoke.started", smoke=item.id)
            try:
                smoke.run(browser.connect(playwright), browser.cdp_url, browser.display, item, self.notebook,
                          self._has_key())
            except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad job
                traceback.print_exc()
                item.status = f"failed: {type(exc).__name__}: {exc}"
            finally:
                view = item.view()
                self.events.publish("smoke.finished", smoke=item.id, status=view["status"],
                                    passed=view["passed"], total=view["total"])
                item.done.set()
            return
        if not isinstance(item, Run):
            # A small browser chore (opening the setup page, a blank session's tab), not a chat run.
            try:
                item(browser.connect(playwright))
            except Exception:  # noqa: BLE001 - a chore must not stop the worker
                traceback.print_exc()
            return
        run = item
        browser.current = run
        try:
            self._run(browser, playwright, run)
        except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad run
            traceback.print_exc()
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.reply = run.reply or f"Stealthbox hit an internal error: {exc}"
        finally:
            browser.current = None
            self._finished(run)
        if not browser.main and browser.ready:
            try:
                browser.save_cookies()
            except Exception as exc:  # noqa: BLE001 - the run is over; the next save carries these too
                traceback.print_exc()
                self.events.publish("browser.cookies_not_saved", browser=browser.key, error=f"{type(exc).__name__}: {exc}")
        with self.lock:
            # The session may be idle now, and its browser free for a waiting run.
            self.dispatch()

    def _finished(self, run):
        run.finished = now()
        run.session.last_used = time.time()
        if run.status in ("needs_user", "needs_input"):
            # Never hide a question for the user in the archive.
            self.archive(run.session, False)
        run.persist()
        view = run.view()
        self.events.publish("run.finished", run=run.id, session=run.session.id,
                            status=run.status, reply=run.reply, error=run.error,
                            final_url=run.final_url, needs_user=run.needs_user,
                            steps=view["steps"],
                            files=view["files"], cost_usd=view["cost_usd"])
        run.done.set()

    def browser_closed(self, browser):
        """From a session browser's worker as it exits: its place is free."""
        with self.lock:
            self.browsers.pop(browser.key, None)
            self.dispatch()

    def _wait_for_browser(self, session, item):
        """Under self.lock: `item` (a run or chore) goes to the session's browser once it has one."""
        session.pending.append(item)
        if session not in self.waiting:
            self.waiting.append(session)
        self.dispatch()

    def dispatch(self):
        """Under self.lock: give waiting sessions a browser, oldest first, and it their work."""
        while self.waiting:
            session = self.waiting[0]
            if not self._give_browser(session):
                return
            self.waiting.pop(0)
            for item in session.pending:
                session.browser.queue.put(item)
            session.pending.clear()

    def _forget(self, session):
        """Under self.lock, for a session gone from self.sessions: it waits for nothing, and
        its own browser closes (its cookies kept). A borrowed tab stays open."""
        if session in self.waiting:
            self.waiting.remove(session)
        session.pending.clear()
        if session.browser is not None and not session.borrowed:
            self._take_browser(session)

    def _give_browser(self, session):
        """Under self.lock: open a browser of its own for the session, if fewer than
        SESSION_BROWSERS are open. At the limit, the least recently used session that is
        done (not working, waiting, or waiting on the user, and not being watched) gives
        its browser up. Returns whether the session got one."""
        if len(self.browsers) >= SESSION_BROWSERS:
            # One closing already frees a place; closing another would leave one spare.
            if any(browser.closing for browser in self.browsers.values()):
                return False
            idle = [s for s in self.sessions.values()
                    if s.browser is not None and not s.borrowed and s.state() in ("done", "stopped")
                    and not s.browser.viewers]
            if not idle:
                return False
            self._take_browser(min(idle, key=lambda s: s.last_used))
            return False
        browser = Browser(self, f"{session.id}-{secrets.token_hex(2)}")
        self.browsers[browser.key] = browser
        session.browser, session.page, session.target_id = browser, None, None
        browser.thread.start()
        self.events.publish("session.browser_opened", session=session.id, browser=browser.key)
        return True

    def _take_browser(self, session):
        """Under self.lock: close the session's own browser. Its tab and live view go; its
        conversation stays, and its next message gets it a new browser."""
        browser, session.browser, session.page = session.browser, None, None
        browser.close()
        self.events.publish("session.browser_closed", session=session.id, browser=browser.key)

    def _expire_sessions(self):
        """Forget sessions idle for SESSION_IDLE_SECONDS, closing their own browsers."""
        while True:
            time.sleep(EXPIRY_CHECK_SECONDS)
            cutoff = time.time() - SESSION_IDLE_SECONDS
            # The main browser's tab in front is the one the user is looking at, perhaps
            # browsing in it by hand, which no request to the API shows; so is a session
            # browser with a live view connected.
            front = (self.main.front_tab() or {}).get("id")
            with self.lock:
                stale = [s for s in self.sessions.values()
                         if not s.busy and s.last_used < cutoff
                         and not (front and s.browser is self.main and s.target_id == front)
                         and not (s.browser and s.browser.viewers)]
                for session in stale:
                    del self.sessions[session.id]
                    self._forget(session)
            for session in stale:
                self.events.publish("session.expired", session=session.id)

    def _stepped(self, run):
        run.persist()
        run.session.url = run.turn.steps[-1].get("url") or run.session.url
        run.session.target_id = run.turn.steps[-1].get("tab") or run.session.target_id
        turn = run.turn
        files = run.view()["files"][run.files_published:]
        run.files_published += len(files)
        self.events.publish("run.step", run=run.id, session=run.session.id,
                            step=step_event(run, turn.steps[-1]), files=files,
                            cost_usd=round(turn.cost_usd, 5))

    def _phase(self, run, phase, text, reason):
        """Where a step is (looking, thinking, acting), so a slow step still shows progress."""
        run.phase = {"phase": phase, "text": text, "reason": reason, "since_ms": int(time.time() * 1000)}
        self.events.publish("run.phase", run=run.id, session=run.session.id, **run.phase)

    def _borrowed_page(self, context, target_id):
        """The open tab with this target id. A tab the caller opened a moment ago
        reaches Playwright as an event, which only a Playwright call lets in, so
        wait on the next new page for a while before calling it gone."""
        deadline = time.time() + BORROW_SECONDS
        while True:
            page = next((item for item in context.pages if self._target_of(item) == target_id), None)
            if page is not None:
                return page
            if time.time() > deadline:
                raise RuntimeError(f"tab {target_id} is not open")
            try:
                context.wait_for_event("page", timeout=500)
            except PlaywrightError:
                pass

    @staticmethod
    def _target_of(page):
        """The page's target id, or None for a tab that closed while the list was read
        (the browser is shared: a run or the user closes tabs at any moment)."""
        if page.is_closed():
            return None
        try:
            return agent.humanize.target_id(page)
        except PlaywrightError:
            return None

    def _run(self, browser, playwright, run):
        if run.cancel_requested:
            run.status = "cancelled"
            run.reply = "Cancelled before it started."
            return
        run.status = "running"
        run.phase = {"phase": "starting", "text": "Getting the tab ready" if browser.ready else "Opening its browser",
                     "reason": None, "since_ms": int(time.time() * 1000)}
        self.events.publish("run.started", run=run.id, session=run.session.id)
        context = browser.connect(playwright)
        session = run.session
        if session.page is None and session.borrowed:
            session.page = self._borrowed_page(context, session.target_id)
        if session.page is None or session.page.is_closed():
            # Its own tab if it is still open (the browser reconnected since); in a new
            # browser of its own, the tab the browser opened with; else a new one.
            session.page = (self._session_page(context, session)
                            or (context.pages[0] if session.target_id is None and context.pages else None)
                            or context.new_page())
        page = session.page
        try:
            page.bring_to_front()
        except PlaywrightError:
            # Closed since the last run, by the user in the viewer or another CDP
            # client; Playwright can still report it open until the next call.
            self.events.publish("session.tab_lost", session=session.id)
            page = session.page = context.new_page()
            page.bring_to_front()
        page.set_default_timeout(30000)
        session.target_id = agent.humanize.target_id(page)

        turn = agent.Turn(run.id, context, browser.cdp_url, browser.display, run.message, list(session.conversation),
                          self.notebook, run.dir, run.max_stuck_steps, agent.DEFAULT_MODEL)
        # Set run.turn before reading the flag: cancel() sets the flag, then the
        # turn's, so a cancel that lands between the two lines is not lost.
        run.turn = turn
        turn.cancelled = run.cancel_requested
        turn.paused = run.paused
        run.persist()

        status, reply, page = agent.run_turn(page, turn, lambda _turn: self._stepped(run),
                                             lambda phase, text, reason: self._phase(run, phase, text, reason))
        session.page = page
        if not page.is_closed():
            session.target_id = agent.humanize.target_id(page)
            run.final_url = session.url = agent.page_url(page)
        try:
            agent.reflect(turn, status, reply)
        except RuntimeError as exc:
            print(f"reflect failed for {run.id}: {exc}", file=sys.stderr)

        run.status = status
        run.reply = reply
        if status == "needs_user":
            # The link when the share is on; otherwise the caller starts it (`stealthbox share`).
            shared = self.share.url() is not None
            run.needs_user = {"task": reply, "share_url": self.share_link(session.id)["url"] if shared else None}
        session.conversation.append({"role": "caller", "text": run.message})
        session.conversation.append({"role": "stealthbox", "text": reply})

        entry = {
            "time": run.started, "run": run.id, "session": session.id,
            "message": run.message, "status": status, "reply": reply[:300],
            "steps": len(turn.steps), "cost_usd": round(turn.cost_usd, 5),
        }
        for site in turn.sites:
            blocks = sorted({b["type"] for b in turn.blocks_seen if b["site"] == site})
            self.notebook.log_run(site, {**entry, "blocks": blocks})
        with open(os.path.join(DATA_DIR, "runs.jsonl"), "a") as fh:
            fh.write(json.dumps({**entry, "sites": turn.sites}) + "\n")


def load_token():
    path = os.path.join(DATA_DIR, "api-token")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32) + "\n")
    with open(path) as fh:
        return fh.read().strip()


class BadRequest(Exception):
    """A request the API cannot take; answered 400 with this message."""


def text_field(body, name):
    """A JSON body's text field, or None when it is left out."""
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise BadRequest(f"{name} must be text")
    return value


def make_handler(stealthbox, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stealthbox"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"api {self.address_string()} {fmt % args}\n")

        def _send(self, code, payload, cookie=None):
            body = json.dumps(payload, indent=2).encode()
            self.send_response(code)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # The share tunnel goes through Cloudflare's CDN: nothing private may be cached there.
            self.send_header("Cache-Control", "no-store, private")
            self.end_headers()
            self.wfile.write(body)

        def _console_cookie(self):
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            return cookie[CONSOLE_COOKIE].value if CONSOLE_COOKIE in cookie else ""

        def _device(self):
            return stealthbox.devices.find(self._console_cookie(), self.headers.get("Host"))

        def _authorized(self):
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if hmac.compare_digest(given, token):
                return True
            # Browsers send Origin on every POST and websocket: a signed-in device's
            # cookie does nothing for a page on another site.
            origin = self.headers.get("Origin")
            if self._device() is not None and (origin is None or urlparse(origin).netloc == self.headers.get("Host")):
                return True
            self._send(401, {"error": "missing or wrong bearer token (see ~/.stealthbox/data/api-token), "
                                      "or not signed in to the console"})
            return False

        def _form(self):
            length = int(self.headers.get("Content-Length") or 0)
            return {name: values[0] for name, values in parse_qs(self.rfile.read(length).decode()).items()}

        def _device_cookie(self):
            """Sign this browser in as a new device; returns its Set-Cookie value."""
            device = stealthbox.devices.sign_in(self.headers.get("Host"), self.headers.get("User-Agent"))
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            # Lax, not Strict: a link opened from Messages is a cross-site
            # navigation, and Strict would leave the cookie off it.
            return (f"{CONSOLE_COOKIE}={device}; Path=/; HttpOnly; SameSite=Lax; "
                    f"Max-Age={console_auth.DEVICE_SECONDS}{secure}")

        def _console(self):
            """The console for a signed-in device; otherwise its sign-in page. On this computer
            with no password yet, the page to choose one: it carries a fresh one-time code, which
            another website's form could not read, so it cannot set the password either."""
            if self._device() is None:
                password_set = stealthbox.password.is_set()
                if not password_set and key_setup.local_host(self.headers.get("Host")):
                    return self._send_html(200, key_setup.password_page(stealthbox.password_codes.issue(), "", False))
                return self._send_html(401, console_auth.sign_in_page(
                    password_set, local_console=f"http://127.0.0.1:{HOST_PORT}/console"))
            with open(CONSOLE_PAGE) as fh:
                page = fh.read()
            return self._send_html(200, page.replace("<!-- password fields -->", console_auth.password_fields(
                "New password", f"At least {console_auth.MIN_LENGTH} characters. Your password manager can save it.",
                required=True)))

        def _sign_in(self):
            form = self._form()
            session = form.get("session", "")
            if not stealthbox.password.is_set():
                return self._send_html(401, console_auth.sign_in_page(
                    False, local_console=f"http://127.0.0.1:{HOST_PORT}/console"))
            try:
                stealthbox.password.check(form.get("password", ""))
            except console_auth.SignInRefused as exc:
                stealthbox.events.publish("console.sign_in_refused")
                return self._send_html(401, console_auth.sign_in_page(True, str(exc), session))
            self._to_console(session)
            stealthbox.events.publish("console.signed_in")
            return None

        def _to_console(self, session="", saved=None):
            """Sign this browser in and send it to the console: on `session` if given, with
            `saved` telling the console what was just saved."""
            self.send_response(303)
            self.send_header("Set-Cookie", self._device_cookie())
            self.send_header("Location", "/console" + (f"?saved={saved}" if saved else "")
                             + (f"#{session}" if SESSION_ID.match(session) else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _password(self, method):
            """Set the console password from a one-time link. Not localhost-only, unlike /setup:
            the user is often away, so this link goes through the share; its code guards it."""
            form = parse_qs(urlparse(self.path).query) if method == "GET" else None
            form = {name: values[0] for name, values in form.items()} if form is not None else self._form()
            code, session = form.get("code", ""), form.get("session", "")
            if not stealthbox.password_codes.valid(code):
                return self._send_html(410, key_setup.expired_page("stealthbox password"))
            if method == "GET":
                stealthbox.password_progress(code, "opened")
                return self._send_html(200, key_setup.password_page(code, session, stealthbox.password.is_set()))
            password = form.get("password", "")
            try:
                console_auth.check_new(password, form.get("again", ""))
            except ValueError as exc:
                stealthbox.password_progress(code, "rejected", str(exc))
                return self._send_html(400, key_setup.password_page(code, session, stealthbox.password.is_set(),
                                                                    str(exc)))
            stealthbox.set_password(password)
            stealthbox.password_progress(code, "saved")
            stealthbox.password_codes.consume(code)
            return self._to_console(session, saved="password")

        def _change_password(self):
            """A new console password from a signed-in console, which gives the current one first.
            Not for the bearer token: an agent never handles the password."""
            if self._device() is None:
                return self._send(403, {"error": "change the console password from a signed-in console"})
            body = self._json_body()
            try:
                stealthbox.password.check(body.get("current", ""))
            except console_auth.SignInRefused as exc:
                stealthbox.events.publish("console.sign_in_refused")
                return self._send(403, {"error": str(exc).replace("Wrong password", "Wrong current password")})
            try:
                console_auth.check_new(body.get("password", ""), body.get("again", ""))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            stealthbox.set_password(body["password"])
            # Every device is signed out with the old password; this one carries on.
            return self._send(200, {"saved": True}, self._device_cookie())

        def _proxy_viewer(self):
            """Pass /viewer/... through to noVNC: its files, and its websocket byte for byte.

            cloudflared keeps connections to this server alive and sends the next
            request down the same one, so a file request is one request and one
            response, never a pipe: a pipe would hand that next request to noVNC.

            /viewer/sessions/<key>/websockify is a session browser's display, through
            the websockify that routes by the browser's key; the rest is the main one's.
            """
            upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
            parts = self._parts()
            if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "websockify":
                try:
                    browser = stealthbox.browser(parts[2])
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                port, path = SESSION_VIEWER_PORT, f"/websockify?token={browser.key}"
            else:
                browser, port, path = stealthbox.main, VIEWER_PORT, self.path[len("/viewer"):] or "/"
            upstream = socket.create_connection(("127.0.0.1", port))
            head = [f"{self.command} {path} {self.request_version}"]
            for name, value in self.headers.items():
                if name.lower() in ("cookie", "host") or (name.lower() == "connection" and not upgrade):
                    continue
                head.append(f"{name}: {value}")
            head.append(f"Host: 127.0.0.1:{port}")
            if not upgrade:
                head.append("Connection: close")
            upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
            self.close_connection = True
            if not upgrade:
                with upstream:
                    # noVNC answers HTTP/1.1 with a Content-Length and no Connection
                    # header, so a browser takes the connection as reusable; this one
                    # closes after the response. Safari sent its next file down it and
                    # failed the load, which stopped the console's script loading.
                    response = b""
                    while b"\r\n\r\n" not in response and (chunk := upstream.recv(65536)):
                        response += chunk
                    head, _, rest = response.partition(b"\r\n\r\n")
                    lines = [line for line in head.split(b"\r\n") if not line.lower().startswith(b"connection:")]
                    self._responded = True
                    self.connection.sendall(b"\r\n".join([*lines, b"Connection: close"]) + b"\r\n\r\n" + rest)
                    while chunk := upstream.recv(65536):
                        self.connection.sendall(chunk)
                return None

            def to_client():
                try:
                    while chunk := upstream.recv(65536):
                        self.connection.sendall(chunk)
                except OSError:
                    pass
                finally:
                    # Wakes the read below when noVNC's side ends first.
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            pump = threading.Thread(target=to_client, daemon=True)
            self._responded = True
            pump.start()
            # Someone is watching: the browser is not closed for being idle.
            with stealthbox.lock:
                browser.viewers += 1
            try:
                while chunk := self.rfile.read1(65536):
                    upstream.sendall(chunk)
            except OSError:
                pass
            finally:
                with stealthbox.lock:
                    browser.viewers -= 1
                    # Unwatched, a finished session's browser can go to one that waits.
                    if not browser.viewers:
                        stealthbox.dispatch()
                upstream.close()
                pump.join(5)
            return None

        def _send_html(self, code, page, cookie=None):
            body = page.encode()
            self.send_response(code)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _setup(self, method):
            if not key_setup.local_host(self.headers.get("Host")):
                return self._send(403, {"error": "the setup page only answers on localhost"})
            password_set = stealthbox.password.is_set()
            if method == "GET":
                query = parse_qs(urlparse(self.path).query)
                code, in_browser = (query.get("code") or [""])[0], bool(query.get("in_browser"))
                if not stealthbox.setup_codes.valid(code):
                    return self._send_html(410, key_setup.expired_page("stealthbox key"))
                stealthbox.key_progress(code, "opened")
                return self._send_html(200, key_setup.form_page(code, password_set, in_browser))
            form = self._form()
            code, in_browser = form.get("code", ""), bool(form.get("in_browser"))
            key = form.get("key", "").strip()
            password, again = form.get("password", ""), form.get("again", "")
            if not stealthbox.setup_codes.valid(code):
                return self._send_html(410, key_setup.expired_page("stealthbox key"))
            # Both fields empty keeps the password there is; with none yet, it is required.
            new_password = bool(password or again) or not password_set
            try:
                if new_password:
                    console_auth.check_new(password, again)
                info = key_setup.check_key(key)
            except ValueError as exc:
                stealthbox.key_progress(code, "rejected", str(exc))
                return self._send_html(400, key_setup.form_page(code, password_set, in_browser, str(exc)))
            key_setup.save_key(key)
            if new_password:
                stealthbox.set_password(password)
            stealthbox.key_progress(code, "saved")
            stealthbox.setup_codes.consume(code)
            print("openrouter key saved", flush=True)
            if in_browser:
                # Stealthbox's own tab, seen through the console: the console stays where it is.
                return self._send_html(200, key_setup.saved_page(info, new_password))
            return self._to_console(saved="setup")

        def _parts(self):
            return [unquote(part) for part in self.path.split("?")[0].strip("/").split("/")]

        def send_response(self, code, message=None):
            self._responded = True
            super().send_response(code, message)

        def _answer(self, route):
            """Every request gets an answer. A handler that raised used to drop the
            connection, which reaches the console through the share as Cloudflare's
            bare 502, saying nothing about what went wrong."""
            self._responded = False
            try:
                route()
            except BadRequest as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - the HTTP boundary
                if self._responded:
                    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                        # The caller hung up partway through the answer.
                        return
                    raise
                # A reset here can be one of ours (cdp-server restarting), not the caller's.
                traceback.print_exc()
                try:
                    self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
                except OSError:
                    # The caller is gone too.
                    return

        def do_GET(self):  # noqa: N802
            self._answer(self._get)

        def do_POST(self):  # noqa: N802
            self._answer(self._post)

        def _get(self):
            parts = self._parts()
            if parts == ["v1", "health"]:
                return self._send(200, {"ok": True})
            if parts == ["favicon.ico"]:
                self.send_response(204)
                self.end_headers()
                return None
            if parts == ["setup"]:
                return self._setup("GET")
            if parts == [""]:
                self.send_response(303)
                self.send_header("Location", "/console")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if parts == ["console"]:
                return self._console()
            if parts == ["console", "sign-in"]:
                self.send_response(303)
                self.send_header("Location", "/console")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if parts == ["password"]:
                return self._password("GET")
            if not self._authorized():
                return None
            if parts[0] == "viewer":
                return self._proxy_viewer()
            if parts == ["v1", "browser", "history"]:
                text = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                return self._send(200, {"suggestions": agent.history_suggestions(text) if text.strip() else []})
            if parts == ["v1", "browser", "screenshot"]:
                query = parse_qs(urlparse(self.path).query)
                width = query.get("width", [str(PREVIEW_WIDTH)])[0]
                if not width.isdigit():
                    raise BadRequest("width must be a whole number of pixels")
                width = int(width)
                # A session's own browser, or the main browser.
                try:
                    browser = stealthbox.session(query["session"][0]).browser if "session" in query else stealthbox.main
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                if browser is None or browser.display is None:
                    return self._send(404, {"error": "that session has no browser open"})
                return self._send_image(agent.display_thumbnail(browser.display, min(width, MAX_PREVIEW_WIDTH)))
            if parts == ["v1", "status"]:
                return self._send(200, stealthbox.status())
            if parts == ["v1", "devices"]:
                return self._send(200, {"devices": stealthbox.devices.view(self._device())})
            if parts == ["v1", "sessions"]:
                return self._send(200, {"sessions": stealthbox.sessions_view()})
            if len(parts) == 3 and parts[:2] == ["v1", "sessions"]:
                try:
                    session = stealthbox.session(parts[2])
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                return self._send(200, stealthbox.session_view(session))
            if parts == ["v1", "events"]:
                return self._stream_events()
            if parts == ["v1", "events", "next"]:
                return self._next_events()
            if parts == ["v1", "notes"]:
                return self._send(200, {"sites": stealthbox.notebook.sites(),
                                        "general": stealthbox.notebook.notes_for("general", 1000),
                                        "guided_sites": stealthbox.notebook.guided_sites(),
                                        "guidance": stealthbox.notebook.guidance_for("general")})
            if len(parts) == 3 and parts[:2] == ["v1", "notes"]:
                site = parts[2]
                try:
                    return self._send(200, {"site": site,
                                            "guidance": stealthbox.notebook.guidance_for(site),
                                            "notes": stealthbox.notebook.notes_for(site, 1000),
                                            "runs": stealthbox.notebook.runs_for(site, 1000)})
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
            if len(parts) == 3 and parts[:2] == ["v1", "smoke"]:
                job = stealthbox.smokes.get(parts[2])
                if job is None:
                    return self._send(404, {"error": f"no smoke test {parts[2]}"})
                return self._send(200, job.view())
            if len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
                run = stealthbox.runs.get(parts[2])
                if len(parts) == 3:
                    if run is not None:
                        return self._send(200, run.view(full=True))
                    return self._send_saved_run(parts[2])
                if len(parts) >= 5 and parts[3] in ("files", "shots"):
                    return self._send_file(parts[2], parts[3], parts[4:])
            return self._send(404, {"error": "not found"})

        def _stream_events(self):
            query = parse_qs(urlparse(self.path).query)
            run_filter = (query.get("run") or [None])[0]
            session_filter = (query.get("session") or [None])[0]
            since = self.headers.get("Last-Event-ID") or (query.get("since") or [None])[0]
            if since is not None and not since.isdigit():
                return self._send(400, {"error": "Last-Event-ID / since must be a non-negative event id"})
            last = stealthbox.events.last_id() if since is None else int(since)
            if last > stealthbox.events.last_id():
                # An id this stream never issued (a clock that went back): replay what
                # is held, which starts with a gap.
                last = 0

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store, private")
            self.end_headers()
            self.connection.settimeout(EVENTS_WRITE_TIMEOUT_SECONDS)
            stealthbox.events.reader(+1)
            try:
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                while True:
                    events, gap = stealthbox.events.after(last, EVENTS_KEEPALIVE_SECONDS)
                    chunks = []
                    if gap:
                        chunks.append(self._event_chunk({"id": events[0]["id"] - 1, "type": "stream.gap",
                                                         "missed_after": last}))
                    for event in events:
                        last = event["id"]
                        if run_filter and event.get("run") != run_filter:
                            continue
                        if session_filter and event.get("session") != session_filter:
                            continue
                        chunks.append(self._event_chunk(event))
                    self.wfile.write(b"".join(chunks) if chunks else b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                # The reader went away, or stopped reading for the write timeout.
                return None
            finally:
                stealthbox.events.reader(-1)

        def _next_events(self):
            """The event stream as a long poll, for the console through the share tunnel:
            Cloudflare quick tunnels hold a Server-Sent Events response until it ends."""
            since = (parse_qs(urlparse(self.path).query).get("since") or [None])[0]
            if since is None:
                return self._send(200, {"events": [], "gap": False, "last_id": stealthbox.events.last_id()})
            if not since.isdigit():
                return self._send(400, {"error": "since must be a non-negative event id"})
            since = int(since)
            if since > stealthbox.events.last_id():
                # An id this stream never issued (a clock that went back): replay what is held.
                since = 0
            events, gap = stealthbox.events.after(since, EVENTS_POLL_SECONDS)
            # With nothing new, the stream's own last id, so a caller never asks again
            # for an id below it and gets an instant empty answer, over and over.
            return self._send(200, {"events": events, "gap": gap,
                                    "last_id": events[-1]["id"] if events else max(since, stealthbox.events.last_id())})

        @staticmethod
        def _event_chunk(event):
            return (f"id: {event['id']}\nevent: {event['type']}\n"
                    f"data: {json.dumps(event, separators=(',', ':'))}\n\n").encode()

        def _send_saved_run(self, run_id):
            path = os.path.join(DATA_DIR, "runs", os.path.basename(run_id), "run.json")
            if not os.path.exists(path):
                return self._send(404, {"error": f"no run {run_id}"})
            with open(path) as fh:
                return self._send(200, json.load(fh))

        def _send_image(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, private")
            self.end_headers()
            self.wfile.write(body)
            return None

        def _send_file(self, run_id, kind, parts):
            base = os.path.realpath(os.path.join(DATA_DIR, "runs", os.path.basename(run_id), kind))
            path = os.path.realpath(os.path.join(base, *parts))
            if not path.startswith(base + os.sep) or not os.path.isfile(path):
                return self._send(404, {"error": "no such file"})
            ext = os.path.splitext(path)[1].lower()
            types = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
            with open(path, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", types.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, private")
            self.end_headers()
            self.wfile.write(body)
            return None

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise BadRequest(f"body is not JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise BadRequest("body must be a JSON object")
            return body

        def _post(self):
            parts = self._parts()
            if parts == ["setup"]:
                return self._setup("POST")
            if parts == ["console", "sign-in"]:
                return self._sign_in()
            if parts == ["password"]:
                return self._password("POST")
            if not self._authorized():
                return None
            if parts == ["v1", "setup-link"]:
                body = self._json_body()
                return self._send(200, stealthbox.setup_link(bool(body.get("in_browser"))))
            if parts == ["v1", "share"]:
                try:
                    return self._send(200, stealthbox.share_link())
                except RuntimeError as exc:
                    return self._send(502, {"error": str(exc)})
            if parts == ["v1", "unshare"]:
                return self._send(200, {"stopped": stealthbox.unshare()})
            if parts == ["v1", "password-link"]:
                shared = bool(self._json_body().get("share"))
                try:
                    base = stealthbox.share.wait() if shared else f"http://127.0.0.1:{HOST_PORT}"
                except RuntimeError as exc:
                    return self._send(502, {"error": str(exc)})
                return self._send(200, {"password_url": stealthbox.password_link(base), "shared": shared,
                                        "console_password": stealthbox.password.is_set(),
                                        "expires_in_seconds": key_setup.CODE_SECONDS})
            if parts == ["v1", "password"]:
                return self._change_password()
            if parts == ["v1", "devices", "sign-out-everywhere"]:
                return self._send(200, {"signed_out": stealthbox.devices.sign_out()})
            if len(parts) == 4 and parts[:2] == ["v1", "devices"] and parts[3] == "sign-out":
                signed_out = stealthbox.devices.sign_out(device_id=parts[2])
                if not signed_out:
                    return self._send(404, {"error": f"no device {parts[2]}"})
                return self._send(200, {"signed_out": signed_out})
            if parts == ["v1", "smoke"]:
                body = self._json_body()
                sites = body.get("sites") or []
                if not isinstance(sites, list) or not all(isinstance(site, str) and site for site in sites):
                    return self._send(400, {"error": "sites must be a list of domains"})
                sites = sites or smoke.default_sites()
                if not sites:
                    return self._send(400, {"error": "name the sites to visit, e.g. "
                                                     "`stealthbox smoke example.com` (no smoke-sites.txt "
                                                     "in the data folder)"})
                job = stealthbox.submit_smoke(sites)
                if body.get("wait", True) is False:
                    return self._send(202, job.view())
                job.done.wait()
                return self._send(200, job.view())
            if parts == ["v1", "chat"]:
                body = self._json_body()
                message = (text_field(body, "message") or "").strip()
                if not message:
                    return self._send(400, {"error": "message is required"})
                max_stuck_steps = body.get("max_stuck_steps") or DEFAULT_MAX_STUCK_STEPS
                if not isinstance(max_stuck_steps, int) or isinstance(max_stuck_steps, bool) or max_stuck_steps < 1:
                    raise BadRequest("max_stuck_steps must be a whole number above 0")
                max_stuck_steps = min(max_stuck_steps, MAX_STUCK_STEPS_LIMIT)
                try:
                    run = stealthbox.submit(message, text_field(body, "session"), max_stuck_steps,
                                            text_field(body, "tab"))
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                except PermissionError as exc:
                    return self._send(412, {"error": str(exc)})
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._send(409, {"error": str(exc)})
                if body.get("wait", True) is False:
                    return self._send(202, run.view())
                run.done.wait()
                return self._send(200, run.view())
            if len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] in ("cancel", "pause", "resume"):
                run = stealthbox.runs.get(parts[2])
                if run is None:
                    return self._send(404, {"error": f"no run {parts[2]}"})
                if parts[3] == "cancel":
                    stealthbox.cancel(run)
                    return self._send(200, {"run": run.id, "cancelling": run.status in ("queued", "running")})
                try:
                    stealthbox.pause(run, parts[3] == "pause")
                except RuntimeError as exc:
                    return self._send(409, {"error": str(exc)})
                return self._send(200, {"run": run.id, "paused": run.paused})
            if parts == ["v1", "browser", "close"]:
                tab = text_field(self._json_body(), "tab")
                if not tab:
                    return self._send(400, {"error": "tab is required"})
                try:
                    stealthbox.close_tab(tab)
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._send(409, {"error": str(exc)})
                return self._send(200, {"tab": tab, "closed": True})
            if parts == ["v1", "browser", "navigate"]:
                body = self._json_body()
                text = (text_field(body, "url") or "").strip()
                if not text:
                    return self._send(400, {"error": "url is required"})
                try:
                    session = stealthbox.session(text_field(body, "session")) if body.get("session") else None
                    url = stealthbox.navigate(text, session)
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._send(409, {"error": str(exc)})
                return self._send(200, {"url": url})
            if parts == ["v1", "sessions"]:
                return self._send(201, {"session": stealthbox.open_blank().id})
            if len(parts) == 4 and parts[:2] == ["v1", "sessions"] and parts[3] in ("focus", "close", "archive",
                                                                                   "unarchive"):
                try:
                    session = stealthbox.session(parts[2])
                    if parts[3] == "focus":
                        stealthbox.focus(session)
                    elif parts[3] == "close":
                        stealthbox.close(session)
                    else:
                        stealthbox.archive(session, parts[3] == "archive")
                except LookupError as exc:
                    return self._send(404, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._send(409, {"error": str(exc)})
                return self._send(200, {"session": session.id, parts[3]: True})
            return self._send(404, {"error": "not found"})

    return Handler


class Server(ThreadingHTTPServer):
    # The default listen backlog is 5: twenty callers at once, beside a few event
    # streams, overflowed it and got their connections reset.
    request_queue_size = 128


def drop_privileges():
    """Run as the owner of the data directory, so files land owned by the host user.

    On Linux a bind mount keeps host uids: files the root-run container writes
    would be root's, and the host user could not read the token or the photos.
    """
    owner = os.stat(DATA_DIR)
    if os.getuid() != 0 or owner.st_uid == 0:
        return
    home = "/tmp/stealthbox-api"
    os.makedirs(home, exist_ok=True)
    os.chown(home, owner.st_uid, owner.st_gid)
    os.setgroups([])
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)
    os.environ["HOME"] = home


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    drop_privileges()
    os.makedirs(os.path.join(DATA_DIR, "runs"), exist_ok=True)
    token = load_token()
    stealthbox = Stealthbox()
    stealthbox.start()
    server = Server(("0.0.0.0", PORT), make_handler(stealthbox, token))
    print(f"stealthbox api on :{PORT}, model {agent.DEFAULT_MODEL}, data {DATA_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
