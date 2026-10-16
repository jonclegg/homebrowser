"""End-to-end tests for the Stealthbox console, through the Cloudflare share link.

They drive a separate headless Chromium (the host's Playwright) against a running
Stealthbox, the way a user on another machine would: every page load goes through
the share tunnel. Runs use the real model, so they cost a few cents and take
several minutes.

    pip install pytest playwright && playwright install chromium
    pytest tests/test_console.py

Stealthbox must be running with an OpenRouter key and a console password, and
STEALTHBOX_CONSOLE_PASSWORD must hold that password. STEALTHBOX_CONSOLE_URL and
STEALTHBOX_CDP_URL point the tests at a console on this computer instead of the
share link (then run only the tests that do not need the share, with -k). The tests sign in with it
(one wrong try too, never enough to lock sign-in) and sign out only the devices
they signed in. They start a share (`stealthbox share --json`), and the last one
stops and restarts it, which signs out the devices that used the old link.

STEALTHBOX_TEST_BROWSER=webkit drives the console with Safari's engine instead of
Chromium (with STEALTHBOX_CONSOLE_URL).

Run them on a Linux host too (with STEALTHBOX_CONSOLE_URL over an SSH tunnel, say).
There the API runs as the data folder's owner while Chrome runs as root, and the
address bar's history once failed only there; on a Mac host both run as root.

Every trycloudflare.com name is served from the same Cloudflare addresses, so the
test browser resolves them through public DNS: a resolver that was asked about a
new name too early (macOS caches the "no such name" for a while) would otherwise
fail tests that have nothing to do with Stealthbox.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SECONDS = 240
RUN_TIMEOUT = RUN_SECONDS * 1000
PASSWORD = os.environ.get("STEALTHBOX_CONSOLE_PASSWORD", "")
# A console on this computer (e.g. http://127.0.0.1:8423/console) instead of a share
# link, with its CDP URL: for the tests that do not need the share itself.
CONSOLE_URL = os.environ.get("STEALTHBOX_CONSOLE_URL", "")
CDP_URL = os.environ.get("STEALTHBOX_CDP_URL", "")
# The browser the tests drive the console with: chromium, or webkit for Safari's engine
# (hover and focus differ between them).
ENGINE = os.environ.get("STEALTHBOX_TEST_BROWSER", "chromium")


def stealthbox(*args):
    result = subprocess.run([os.path.join(ROOT, "stealthbox"), *args], capture_output=True, text=True, check=True)
    return result.stdout


def share_link():
    lines = [line for line in stealthbox("share", "--json").splitlines() if line.startswith("{")]
    return json.loads(lines[-1])["url"]


def sign_in(page, url):
    """Open a console link and sign in; the page ends on the console, live."""
    page.goto(url)
    page.get_by_label("Console password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("events-state")).to_have_text("Live updates on", timeout=30000)


def cloudflare_address():
    answer = subprocess.run(["dig", "+short", "@1.1.1.1", "trycloudflare.com"],
                            capture_output=True, text=True, check=True).stdout.split()
    return next(line for line in answer if re.match(r"^\d+\.\d+\.\d+\.\d+$", line))


def wait_reachable(browser, url):
    """A new tunnel can answer with an error for its first seconds."""
    context = browser.new_context()
    try:
        deadline = time.time() + 120
        while True:
            try:
                response = context.request.get(url.split("/console")[0] + "/v1/health", timeout=10000)
                if response.status == 200:
                    return
            except Exception:
                if time.time() > deadline:
                    raise
            time.sleep(2)
    finally:
        context.close()


@pytest.fixture(scope="module", autouse=True)
def console_password():
    if not PASSWORD:
        pytest.fail("Set STEALTHBOX_CONSOLE_PASSWORD to this install's console password.")


# How each engine names a request the page itself gave up on (a reload ends the long poll).
ABORTED = {"net::ERR_ABORTED", "cancelled"}
# Playwright's Chromium, for connecting to Stealthbox's own browser over CDP whatever
# engine drives the console.
CHROMIUM = []


def stealthbox_browser():
    """The main browser, over CDP."""
    return CHROMIUM[0].connect_over_cdp(main_cdp_url())


def main_cdp_url():
    return CDP_URL or json.loads(stealthbox("status", "--json"))["cdp_url"]


def session_browser(view):
    """A session's own browser, over CDP (cdp-server routes it by the browser's key)."""
    key = view["browser"]["viewer"].split("/")[-2]
    return CHROMIUM[0].connect_over_cdp(f"{main_cdp_url()}/sessions/{key}")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        CHROMIUM[:] = [playwright.chromium]
        if ENGINE == "chromium":
            browser = playwright.chromium.launch(
                args=[f"--host-resolver-rules=MAP *.trycloudflare.com {cloudflare_address()}"])
        else:
            # Safari's engine (webkit) has no resolver rules; use it with a local console URL.
            browser = getattr(playwright, ENGINE).launch()
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def link(browser):
    url = CONSOLE_URL or share_link()
    wait_reachable(browser, url)
    # After a restart the browser can take minutes to come up; the tests time runs, not that.
    context = browser.new_context()
    sign_in(context.new_page(), url)
    deadline = time.time() + 300
    while not context.request.get(url.split("/console")[0] + "/v1/status").json()["browser_connected"]:
        assert time.time() < deadline, "the browser never connected"
        time.sleep(3)
    context.close()
    return url


@pytest.fixture
def page(browser, link):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    sign_in(page, link)
    base = link.split("/console")[0]
    before = {s["session"] for s in context.request.get(base + "/v1/sessions").json()["sessions"]}
    # Whatever a test does, the console's own requests never fail on the server's side:
    # through the share, a handler that broke reached the user as a bare "502".
    failed = []
    page.on("response", lambda response: response.status >= 500 and "x-test-fault" not in response.headers
            and failed.append(f"{response.request.method} {response.url} -> {response.status}"))
    # A connection dropped without an answer (aborted ones are the page's or a test's doing).
    page.on("requestfailed", lambda request: request.failure not in ABORTED and failed.append(
        f"{request.method} {request.url} -> {request.failure}"))
    yield page
    # Stop and close what this test opened, so the next one starts from the same list:
    # a run a failed test left going (or paused) holds a browser the next test may need.
    opened = [s["session"] for s in context.request.get(base + "/v1/sessions").json()["sessions"]
              if s["session"] not in before]
    deadline = time.time() + RUN_SECONDS
    for session in opened:
        while run := context.request.get(f"{base}/v1/sessions/{session}").json()["running_run"]:
            assert time.time() < deadline, f"session {session} never stopped"
            context.request.post(f"{base}/v1/runs/{run}/cancel")
            time.sleep(2)
        context.request.post(f"{base}/v1/sessions/{session}/close")
    context.close()
    # After the cleanup, so a failure here does not leave runs holding the browser for the next test.
    assert not failed, f"requests the server failed: {failed}"


def start_session(page, message):
    """Start a session from the console; returns its id once the page has selected it."""
    before = [s["session"] for s in page.request.get(page.url.split("/console")[0] + "/v1/sessions").json()["sessions"]]
    page.get_by_test_id("new-message").fill(message)
    page.get_by_test_id("new-start").click()
    expect(page.get_by_test_id("new-message")).to_have_value("")
    page.wait_for_function("before => location.hash.startsWith('#s_') && !before.includes(location.hash.slice(1))",
                           arg=before)
    return page.evaluate("location.hash.slice(1)")


def status(page):
    return page.get_by_test_id("session-status")


def stealthbox_messages(page):
    return page.get_by_test_id("message-stealthbox")


def list_item(page, session):
    return page.locator(f'[data-testid="session-item"][data-session="{session}"]')


def step_count(page):
    steps = page.get_by_test_id("steps").last
    if not steps.count():
        return 0
    return int(re.match(r"\d+", steps.locator(":scope > summary").inner_text()).group(0))


def assert_on_screen(page, locator):
    """The bottom of `locator` is inside the window: the newest thing shows without scrolling."""
    box = locator.bounding_box()
    height = page.viewport_size["height"]
    assert box and 0 <= box["y"] + box["height"] <= height + 1, f"off screen: {box} in a window {height}px tall"


def assert_latest_in_view(page):
    """The page itself never scrolls; the live view, the newest message and the reply box are all in sight."""
    assert page.evaluate("document.scrollingElement.scrollHeight <= innerHeight"), "the page scrolls"
    assert_on_screen(page, page.locator("#live:not([hidden]), #still:not([hidden])").first)
    assert_on_screen(page, page.locator("[data-testid=message-you], [data-testid=message-stealthbox]").last)
    assert_on_screen(page, page.get_by_test_id("reply"))


# ---------------------------------------------------------------- the link


def test_the_console_asks_for_the_password_and_keeps_the_device_30_days(browser, link):
    base = link.split("/console")[0]
    context = browser.new_context()
    page = context.new_page()

    page.goto(link)
    expect(page.get_by_role("heading", name="Sign in to the console")).to_be_visible()
    assert page.request.get(base + "/v1/sessions").status == 401
    assert page.request.get(base + "/viewer/core/rfb.js").status == 401

    page.get_by_label("Console password").fill(PASSWORD + "-not")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_contain_text("Wrong password")

    page.get_by_label("Console password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("events-state")).to_have_text("Live updates on", timeout=30000)
    assert page.request.get(base + "/v1/sessions").status == 200
    cookie = next(c for c in context.cookies() if c["name"] == "stealthbox_console")
    # Secure only through the HTTPS share: a browser drops a Secure cookie on plain http.
    assert cookie["httpOnly"] and cookie["secure"] == link.startswith("https:") and cookie["sameSite"] == "Lax"
    assert abs(cookie["expires"] - time.time() - 30 * 24 * 3600) < 600

    # Another site's page cannot use the cookie.
    assert page.request.post(base + "/v1/devices/sign-out-everywhere",
                             headers={"Origin": "https://example.com"}).status == 401
    context.close()


def test_signing_out_another_device_sends_it_back_to_sign_in(browser, link):
    base = link.split("/console")[0]
    mine = browser.new_context()
    other = browser.new_context()
    page = mine.new_page()
    other_page = other.new_page()
    sign_in(page, link)
    sign_in(other_page, link)
    other_id = next(d["device"] for d in other.request.get(base + "/v1/devices").json()["devices"] if d["current"])

    page.get_by_test_id("devices-open").click()
    expect(page.get_by_test_id("devices")).to_be_visible()
    expect(page.get_by_test_id("device").filter(has_text="This device")).to_have_count(1)
    page.locator(f'[data-device="{other_id}"]').click()
    expect(page.locator(f'[data-device="{other_id}"]')).to_have_count(0)

    expect(other_page.get_by_role("heading", name="Sign in to the console")).to_be_visible(timeout=60000)
    assert other.request.get(base + "/v1/sessions").status == 401
    assert page.request.get(base + "/v1/sessions").status == 200
    mine_id = next(d["device"] for d in mine.request.get(base + "/v1/devices").json()["devices"] if d["current"])
    mine.request.post(f"{base}/v1/devices/{mine_id}/sign-out")
    mine.close()
    other.close()


# ---------------------------------------------------------------- sessions


def test_new_session_runs_to_a_reply_with_steps_and_a_live_view(page):
    session = start_session(page, "Open example.com and tell me the page's title.")
    expect(list_item(page, session)).to_be_visible()
    expect(status(page)).to_have_text(re.compile("Waiting|Working"), timeout=30000)

    # Progress inside the first step: what it is doing, for how long, and the live tab.
    now = page.get_by_test_id("now")
    expect(now).to_be_visible(timeout=RUN_TIMEOUT)
    # Queued while the session's own browser opens, then a phase.
    expect(stealthbox_messages(page).last).to_have_attribute(
        "data-phase", re.compile("starting|looking|thinking|acting"), timeout=60000)
    expect(now.locator(".elapsed")).to_have_text(re.compile(r"^\d+s$"), timeout=5000)
    expect(page.get_by_test_id("live-view")).to_be_visible()
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-connected", "true", timeout=30000)
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "true")
    # The address bar is read-only while the run drives the tab.
    expect(page.get_by_test_id("url")).to_have_js_property("readOnly", True)

    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    expect(page.get_by_test_id("url")).to_have_js_property("readOnly", False)
    expect(page.get_by_test_id("session-title")).to_have_text("example.com")
    expect(now).to_contain_text("Finished")
    assert_latest_in_view(page)
    # What the model says varies; the console's job is to show it.
    expect(stealthbox_messages(page).last.locator(".bubble")).to_contain_text(re.compile("example|domain", re.I))
    assert step_count(page) >= 1
    steps = page.get_by_test_id("steps").last
    if not steps.evaluate("details => details.open"):
        steps.locator(":scope > summary").click()
    expect(steps.locator("li").first).to_be_visible()
    expect(steps.locator(".why").first).to_contain_text("Why:")
    steps.locator(":scope > summary").click()
    expect(steps.locator("li").first).to_be_hidden()

    item = list_item(page, session)
    expect(item).to_have_attribute("data-state", "done")
    thumbnail = item.locator("img")
    expect(thumbnail).to_have_count(1)
    page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=thumbnail.element_handle())
    expect(page.get_by_test_id("browser-state")).to_have_text("Browser ready")

    # When idle, the live view hands the user the mouse.
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false")


def test_a_follow_up_queues_behind_the_run_and_stays_in_the_same_tab(page):
    session = start_session(page, "Open example.com and tell me the page's title.")
    expect(status(page)).to_have_text(re.compile("Waiting|Working"), timeout=30000)
    page.get_by_test_id("reply").fill("What does the one link on that page say?")
    page.get_by_test_id("send").click()

    expect(page.get_by_test_id("message-you")).to_have_count(2)
    expect(stealthbox_messages(page)).to_have_count(2)
    expect(stealthbox_messages(page).last).to_have_attribute("data-status", re.compile("queued|running"))

    expect(stealthbox_messages(page).first).to_have_attribute("data-status", "done", timeout=RUN_TIMEOUT)
    expect(stealthbox_messages(page).last).to_have_attribute("data-status", "done", timeout=RUN_TIMEOUT)
    expect(stealthbox_messages(page).last.locator(".bubble")).not_to_have_text("(no reply)")
    assert page.evaluate("location.hash.slice(1)") == session
    expect(list_item(page, session)).to_contain_text("What does the one link")


def test_take_over_holds_the_run_and_hand_back_resumes_it_then_stop(page):
    # Long enough that the run is still going when the test stops it.
    start_session(page, "Go to wikipedia.org, search for Austin, Texas, open the article, then open "
                        "the article of each of the ten largest cities in Texas one by one, and tell "
                        "me the population in each infobox and the name of each mayor.")
    expect(status(page)).to_have_text("Working", timeout=RUN_TIMEOUT)
    page.wait_for_function("() => document.querySelectorAll('[data-testid=steps] li').length >= 1",
                           timeout=RUN_TIMEOUT)

    page.get_by_test_id("pause").click()
    expect(status(page)).to_have_text("Paused · you have the mouse")
    expect(page.get_by_test_id("paused-note")).to_be_visible()
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false")

    # The step in flight may finish; after that nothing moves while the user drives.
    time.sleep(20)
    held = step_count(page)
    time.sleep(15)
    assert step_count(page) == held, "the run took steps while paused"

    page.get_by_test_id("resume").click()
    expect(status(page)).to_have_text("Working")
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "true")
    page.wait_for_function(f"() => {{ const s = document.querySelectorAll('[data-testid=steps]');"
                           f" const summary = s[s.length - 1]?.querySelector('summary')?.textContent || '';"
                           f" return parseInt(summary) > {held}; }}", timeout=RUN_TIMEOUT)

    page.get_by_test_id("cancel").click()
    expect(status(page)).to_have_text("Stopped", timeout=RUN_TIMEOUT)
    expect(stealthbox_messages(page).last).to_have_attribute("data-status", "cancelled")


def test_the_message_box_button_stops_the_run_until_the_user_types(page):
    start_session(page, "Go to wikipedia.org, search for Austin, Texas, open the article, "
                        "and tell me the population in the infobox and the name of the mayor.")
    expect(status(page)).to_have_text("Working", timeout=RUN_TIMEOUT)

    # Typing turns the stop button back into send, which would queue the message.
    page.get_by_test_id("reply").fill("Also the area")
    expect(page.get_by_test_id("send")).to_be_enabled()
    page.get_by_test_id("reply").fill("")

    page.get_by_test_id("stop").click()
    expect(page.get_by_test_id("stop")).to_be_disabled()
    expect(status(page)).to_have_text("Stopped", timeout=RUN_TIMEOUT)
    expect(stealthbox_messages(page).last).to_have_attribute("data-status", "cancelled")
    expect(page.get_by_test_id("send")).to_be_disabled()


# ---------------------------------------------------------------- blank sessions and the archive


def api_get(page, path):
    return page.request.get(page.url.split("/console")[0] + path).json()


def open_blank_session(page):
    """Start a blank session from the console; returns its id once its tab is open and in front."""
    before = [s["session"] for s in api_get(page, "/v1/sessions")["sessions"]]
    page.get_by_test_id("new-blank").click()
    page.wait_for_function("before => location.hash.startsWith('#s_') && !before.includes(location.hash.slice(1))",
                           arg=before)
    session = page.evaluate("location.hash.slice(1)")
    deadline = time.time() + 30
    while not api_get(page, f"/v1/sessions/{session}")["in_front"]:
        assert time.time() < deadline, "the blank session's tab never came to the front"
        time.sleep(0.5)
    return session


def archive_toggle(page):
    return page.get_by_test_id("archive-toggle")


def archive_from_the_list(page, session):
    list_item(page, session).hover()
    page.locator(f'[data-archive="{session}"]').click()


def test_a_blank_session_opens_a_new_tab_to_browse_with_no_task(page):
    session = open_blank_session(page)
    expect(list_item(page, session)).to_have_attribute("data-state", "new")
    expect(list_item(page, session)).to_contain_text("Blank tab · no messages")
    expect(status(page)).to_have_text("New")
    expect(page.get_by_test_id("session-title")).to_have_text("New session")
    expect(page.get_by_test_id("blank-hint")).to_be_visible()
    view = api_get(page, f"/v1/sessions/{session}")
    # A browser of its own, with the session's tab in front.
    assert view["messages"] == [] and view["in_front"] and view["browser"]["kind"] == "own", view

    # The user drives it: the mouse is theirs, and the address bar loads in this session's tab.
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false", timeout=15000)
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    url.type("example.com")
    url.press("Enter")
    expect(page.get_by_test_id("session-title")).to_have_text("example.com", timeout=15000)
    expect(list_item(page, session)).to_contain_text("example.com")
    assert api_get(page, f"/v1/sessions/{session}")["tab"] == view["tab"]

    page.get_by_test_id("close").click()
    expect(list_item(page, session)).to_have_count(0)


def test_typing_in_a_blank_sessions_address_bar_suggests_without_an_error(page):
    """The Dell's 502: on a Linux host the API could not read Chrome's history, and
    every keystroke in the address bar put a bare "502" over the session."""
    open_in_browser_view(page, "example.org")
    expect(page.get_by_test_id("url")).to_have_value(re.compile(r"^https://example\.org/?$"), timeout=15000)
    session = open_blank_session(page)
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false", timeout=15000)
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    with page.expect_request(lambda request: "/v1/browser/history" in request.url) as sent:
        url.type("goo", delay=60)
    answer = sent.value.response()
    assert answer is not None, f"no answer: {sent.value.failure}"
    assert answer.status == 200, answer.text()
    assert isinstance(answer.json()["suggestions"], list)
    page.wait_for_timeout(1000)
    expect(page.get_by_test_id("error")).to_be_hidden()
    expect(list_item(page, session)).to_have_attribute("data-state", "new")


def test_a_request_that_fails_on_the_server_says_what_failed(page):
    """A handler that raises answers 500 with its error, and a bad body 400, instead of
    dropping the connection (which the share turns into Cloudflare's bare 502)."""
    base = page.url.split("/console")[0]
    for path, code in (("/v1/browser/screenshot?width=wide", 400), ("/v1/sessions/not-a-session", 404)):
        response = page.request.get(base + path)
        assert response.status == code and response.json()["error"], f"{path}: {response.status} {response.text()}"
    for path, body, error in (
            ("/v1/browser/navigate", b"{not json", "not JSON"),
            ("/v1/browser/navigate", b'["a list"]', "JSON object"),
            ("/v1/browser/navigate", b'{"url": ["example.com"]}', "url must be text"),
            ("/v1/browser/navigate", b'{"url": "example.com", "session": 7}', "session must be text"),
            ("/v1/chat", b'{"message": 5}', "message must be text"),
            ("/v1/chat", b'{"message": "hi", "max_stuck_steps": "abc", "wait": false}', "max_stuck_steps"),
            ("/v1/chat", b'{"message": "hi", "max_stuck_steps": -3, "wait": false}', "max_stuck_steps"),
            ("/v1/chat", b'{"message": "hi", "session": ["s_1"], "wait": false}', "session must be text")):
        response = page.request.post(base + path, data=body,
                                     headers={"Content-Type": "application/json", "Origin": base})
        assert response.status == 400 and error in response.json()["error"], f"{path} {body}: {response.text()}"


def test_a_blank_session_opened_while_a_run_works_is_live_at_once_in_a_browser_of_its_own(page):
    """Each session has its own browser, so a run never holds the screen another session needs."""
    working = start_session(page, "Go to wikipedia.org, search for Austin, Texas, open the article, then open "
                                  "the article of each of the ten largest cities in Texas one by one, and tell "
                                  "me the population in each infobox and the name of each mayor.")
    expect(status(page)).to_have_text("Working", timeout=RUN_TIMEOUT)

    blank = open_blank_session(page)
    expect(status(page)).to_have_text("New")
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false", timeout=30000)
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-connected", "true", timeout=30000)
    assert api_get(page, f"/v1/sessions/{working}")["state"] == "working"
    assert api_get(page, f"/v1/sessions/{blank}")["browser"]["viewer"] != \
        api_get(page, f"/v1/sessions/{working}")["browser"]["viewer"]


def test_two_sessions_work_at_the_same_time(page):
    base = page.url.split("/console")[0]
    first = start_session(page, "Open example.com and tell me the page's title.")
    second = start_session(page, "Open example.org and tell me the page's title.")
    deadline = time.time() + RUN_SECONDS
    while sorted(api_get(page, f"/v1/sessions/{s}")["state"] for s in (first, second)) != ["working", "working"]:
        assert time.time() < deadline, "the two sessions never worked at once"
        time.sleep(1)
    assert len(page.request.get(base + "/v1/status").json()["running"]) >= 2
    for session in (first, second):
        list_item(page, session).click()
        expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)


def test_at_the_limit_a_session_waits_for_a_browser_and_a_finished_one_gives_its_up(page):
    """At most `limit` session browsers are open. A new session waits while every one is
    in use, and takes the browser of the least recently used finished session."""
    base = page.url.split("/console")[0]
    limit = api_get(page, "/v1/status")["session_browsers"]["limit"]
    finished = start_session(page, "Open example.com and tell me the page's title.")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    # Blank sessions are in use (the user browses in them); they keep their browsers.
    blanks = [open_blank_session(page) for _ in range(limit - 1)]
    assert api_get(page, "/v1/status")["session_browsers"]["open"] == limit

    # A new session takes the finished one's browser (not while the console shows it live,
    # so look at another session first). The finished one keeps its last screenshot.
    list_item(page, blanks[0]).click()
    taker = start_session(page, "Open example.org and tell me the page's title.")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    gave_up = api_get(page, f"/v1/sessions/{finished}")
    assert gave_up["browser"] is None and gave_up["state"] == "done", gave_up
    list_item(page, finished).click()
    expect(page.get_by_test_id("still-view")).to_be_visible()
    expect(page.locator("#s-still-text")).to_contain_text("closed to make room")

    # Every browser held by a blank session: the next session waits, and says so, until one closes.
    page.request.post(f"{base}/v1/sessions/{taker}/close", headers={"Origin": base})
    blanks.append(page.request.post(base + "/v1/sessions", headers={"Origin": base}).json()["session"])
    deadline = time.time() + 60
    while not api_get(page, f"/v1/sessions/{blanks[-1]}")["in_front"]:
        assert time.time() < deadline, "the blank session never got the taker's browser"
        time.sleep(1)
    waiting = page.request.post(base + "/v1/sessions", headers={"Origin": base}).json()["session"]
    page.goto(f"{base}/console#{waiting}")
    expect(page.get_by_test_id("now-text")).to_contain_text("Waiting for a browser", timeout=15000)
    assert api_get(page, f"/v1/sessions/{waiting}")["waiting_for_browser"]
    page.request.post(f"{base}/v1/sessions/{blanks[0]}/close", headers={"Origin": base})
    deadline = time.time() + 60
    while not api_get(page, f"/v1/sessions/{waiting}")["in_front"]:
        assert time.time() < deadline, "the waiting session never got the browser that closed"
        time.sleep(1)


def test_a_cookie_set_in_a_sessions_browser_reaches_the_main_browser_when_it_closes(page):
    """Signed in inside a session, signed in from then on: its new cookies go back to the main profile."""
    session = open_blank_session(page)
    base = page.url.split("/console")[0]
    page.request.post(base + "/v1/browser/navigate", headers={"Origin": base},
                      data={"url": "https://example.com/", "session": session})
    marker = f"kept-{int(time.time())}"
    own = session_browser(api_get(page, f"/v1/sessions/{session}"))
    try:
        tab = next(item for item in own.contexts[0].pages if "example.com" in item.url)
        tab.evaluate(f"document.cookie = 'stealthbox_test={marker}; max-age=3600; path=/'")
    finally:
        own.close()
    page.request.post(f"{base}/v1/sessions/{session}/close", headers={"Origin": base})
    main = stealthbox_browser()
    try:
        deadline = time.time() + 30
        while not any(c["name"] == "stealthbox_test" and c["value"] == marker
                      for c in main.contexts[0].cookies("https://example.com/")):
            assert time.time() < deadline, "the session's cookie never reached the main browser"
            time.sleep(1)
    finally:
        main.close()


def test_the_x_archives_a_session_into_a_closed_archive_at_the_bottom_and_restore_brings_it_back(page):
    first = open_blank_session(page)
    second = open_blank_session(page)
    expect(archive_toggle(page)).to_have_count(0)

    # The button shows on hover, like a mail app's; archiving the session on screen moves off it.
    expect(page.locator(f'[data-archive="{first}"]')).to_have_css("opacity", "0")
    list_item(page, first).hover()
    expect(page.locator(f'[data-archive="{first}"]')).to_have_css("opacity", "1")
    archive_from_the_list(page, second)
    expect(list_item(page, second)).to_have_count(0)
    page.wait_for_function("second => location.hash.slice(1) !== second", arg=second)
    assert api_get(page, f"/v1/sessions/{second}")["archived"] is True

    # The archive is one closed line at the bottom of the list.
    expect(archive_toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(archive_toggle(page)).to_contain_text("Archived")
    expect(archive_toggle(page).locator(".count")).to_have_text("1")
    rail = page.locator("nav.rail").bounding_box()
    toggle = archive_toggle(page).bounding_box()
    assert toggle["y"] > rail["y"] + rail["height"] / 2, (rail, toggle)

    archive_toggle(page).click()
    expect(archive_toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_test_id("archive").locator(f'[data-session="{second}"]')).to_be_visible()

    # Still archived after a reload, and the archive starts closed again.
    page.reload()
    expect(page.get_by_test_id("events-state")).to_have_text("Live updates on", timeout=30000)
    expect(archive_toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(list_item(page, second)).to_have_count(0)
    expect(list_item(page, first)).to_be_visible()

    # An archived session opens from the archive, and Unarchive puts it back in the list.
    archive_toggle(page).click()
    list_item(page, second).click()
    page.get_by_test_id("unarchive").click()
    expect(archive_toggle(page)).to_have_count(0)
    expect(list_item(page, second)).to_be_visible()
    assert api_get(page, f"/v1/sessions/{second}")["archived"] is False

    # The restore button on an archived row does the same.
    archive_from_the_list(page, first)
    archive_toggle(page).click()
    page.locator(f'[data-unarchive="{first}"]').click()
    expect(archive_toggle(page)).to_have_count(0)
    expect(list_item(page, first)).to_be_visible()


def row_of(item):
    return item.locator("xpath=..")


def x_of(item):
    return row_of(item).locator(".row-action")


def x_showing(item):
    return x_of(item).evaluate("button => getComputedStyle(button).opacity") == "1"


def mouse_away(page):
    """The mouse on the header, over no row."""
    page.mouse.move(700, 20)


def test_the_x_shows_only_while_the_mouse_is_over_its_row(page):
    """It used to stay on the chosen session, and on a row after it was clicked."""
    first = open_blank_session(page)
    second = open_blank_session(page)
    mouse_away(page)
    assert not x_showing(list_item(page, first)) and not x_showing(list_item(page, second)), "an X shows with no mouse on it"

    list_item(page, first).hover()
    expect(x_of(list_item(page, first))).to_have_css("opacity", "1")
    assert not x_showing(list_item(page, second))

    # Clicked, the row is chosen and has the focus; neither keeps its X once the mouse leaves.
    list_item(page, first).click()
    expect(list_item(page, first)).to_have_attribute("aria-current", "true")
    mouse_away(page)
    expect(x_of(list_item(page, first))).to_have_css("opacity", "0")
    assert not x_showing(list_item(page, second))

    list_item(page, second).hover()
    expect(x_of(list_item(page, second))).to_have_css("opacity", "1")
    assert not x_showing(list_item(page, first))

    # An archived row's restore button works the same way.
    x_of(list_item(page, first)).click()
    archive_toggle(page).click()
    restore = page.locator(f'[data-unarchive="{first}"]')
    mouse_away(page)
    expect(restore).to_have_css("opacity", "0")
    list_item(page, first).hover()
    expect(restore).to_have_css("opacity", "1")
    restore.click()
    expect(page.locator(f'[data-archive="{first}"]')).to_be_attached()


def test_the_x_stays_under_a_still_mouse_while_the_list_refreshes(page):
    """The list was rebuilt on every refresh; under a mouse that did not move, the new
    row was not hovered, so the X went away until the mouse moved. A row must not even
    be moved out and back in while others change around it."""
    base = page.url.split("/console")[0]
    hovered = open_blank_session(page)
    # Newer, so listed above the hovered row; archived, it moves past it.
    moving = open_blank_session(page)
    list_item(page, hovered).hover()
    expect(x_of(list_item(page, hovered))).to_have_css("opacity", "1")
    page.evaluate("""id => {
        window.hoveredRow = document.querySelector(`[data-session='${id}']`).parentElement;
        window.hoveredRowRemoved = false;
        new MutationObserver((records) => records.forEach((record) => record.removedNodes.forEach((node) => {
            if (node === window.hoveredRow) window.hoveredRowRemoved = true;
        }))).observe(document.getElementById('groups'), { childList: true, subtree: true });
    }""", hovered)
    # Refreshes, a new session above it, and one moving past it into the archive.
    page.request.post(base + "/v1/sessions", headers={"Origin": base})
    page.request.post(f"{base}/v1/sessions/{moving}/archive", headers={"Origin": base})
    page.wait_for_timeout(7000)
    assert page.evaluate("id => !window.hoveredRowRemoved && "
                         "document.querySelector(`[data-session='${id}']`).parentElement === window.hoveredRow", hovered), \
        "the row under the mouse was replaced or moved"
    assert x_showing(list_item(page, hovered)), "the X went away under a still mouse"


def test_the_browser_entry_has_an_x_that_closes_its_tab(browser, page):
    """The Browser entry stands for a tab no session shows (another program's, or one
    opened by hand); it looked like a session with no X."""
    driver = stealthbox_browser()
    tab = None
    try:
        tab = driver.contexts[0].new_page()
        tab.goto("https://example.com/", wait_until="domcontentloaded")
        tab.bring_to_front()
        entry = page.get_by_test_id("browser-item")
        expect(entry).to_contain_text("example.com", timeout=15000)
        mouse_away(page)
        expect(x_of(entry)).to_have_css("opacity", "0")
        entry.hover()
        expect(x_of(entry)).to_have_css("opacity", "1")
        expect(x_of(entry)).to_have_attribute("title", "Close tab")
        with tab.expect_event("close", timeout=15000):
            x_of(entry).click()
        expect(page.get_by_test_id("browser-item")).not_to_contain_text("example.com", timeout=15000)
    finally:
        # Left open, the tab would be the Browser entry of the tests after this one.
        if tab is not None and not tab.is_closed():
            tab.close()
        driver.close()
    base = page.url.split("/console")[0]
    response = page.request.post(base + "/v1/browser/close", data={"tab": "not-a-tab"}, headers={"Origin": base})
    assert response.status == 404, response.text()


def test_the_browser_entry_x_on_an_archived_sessions_tab_closes_the_tab_and_the_session(page):
    """Archived, a session in a main-browser tab leaves the list, so the Browser entry stands for its tab."""
    base = page.url.split("/console")[0]
    driver = stealthbox_browser()
    try:
        lent = driver.contexts[0].new_page()
        lent.goto("https://example.com/", wait_until="domcontentloaded")
        lent.bring_to_front()
        tab = driver.contexts[0].new_cdp_session(lent).send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        driver.close()
    run = page.request.post(base + "/v1/chat", headers={"Origin": base},
                            data={"message": "Tell me this page's title.", "tab": tab, "wait": False}).json()
    page.request.post(f"{base}/v1/runs/{run['run']}/cancel", headers={"Origin": base})
    session = run["session"]
    # A run stops after its current step; its tab cannot close while it works.
    deadline = time.time() + RUN_SECONDS
    while api_get(page, f"/v1/sessions/{session}")["running_run"]:
        assert time.time() < deadline, "the run never stopped"
        time.sleep(1)
    list_item(page, session).wait_for()
    archive_from_the_list(page, session)
    entry = page.get_by_test_id("browser-item")
    expect(entry).to_be_visible(timeout=10000)
    assert api_get(page, "/v1/status")["front_tab"]["id"] == tab
    entry.hover()
    x_of(entry).click()
    expect(page.get_by_test_id("archive")).to_have_count(0, timeout=15000)
    assert page.request.get(f"{base}/v1/sessions/{session}").status == 404
    assert api_get(page, "/v1/status")["front_tab"]["id"] != tab


def test_the_archive_button_shows_without_hover_on_a_phone(browser, link):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    phone = context.new_page()
    sign_in(phone, link)
    base = link.split("/console")[0]
    session = context.request.post(base + "/v1/sessions").json()["session"]
    try:
        phone.get_by_test_id("sidebar-toggle").click()
        expect(phone.locator(f'[data-archive="{session}"]')).to_have_css("opacity", "1", timeout=15000)
        phone.locator(f'[data-archive="{session}"]').tap()
        expect(archive_toggle(phone)).to_be_visible()
        assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the phone page scrolls sideways"
    finally:
        context.request.post(f"{base}/v1/sessions/{session}/close")
        context.close()


def test_a_message_to_an_archived_session_brings_it_back(page):
    session = open_blank_session(page)
    archive_from_the_list(page, session)
    archive_toggle(page).click()
    list_item(page, session).click()
    page.get_by_test_id("reply").fill("Open example.com and tell me the page's title.")
    page.get_by_test_id("send").click()
    expect(archive_toggle(page)).to_have_count(0)
    expect(list_item(page, session)).to_be_visible()
    assert api_get(page, f"/v1/sessions/{session}")["archived"] is False
    page.get_by_test_id("stop").click()
    expect(status(page)).to_have_text("Stopped", timeout=RUN_TIMEOUT)


# ---------------------------------------------------------------- needs you


def test_a_sign_in_hands_off_with_a_session_link_that_opens_anywhere(browser, page, link):
    session = start_session(page, "Go to github.com and open my notifications")
    expect(status(page)).to_have_text("Needs you", timeout=RUN_TIMEOUT)
    expect(list_item(page, session)).to_have_attribute("data-state", "needs")
    expect(page.locator(".group").first).to_have_text("Needs you")

    card = page.get_by_test_id("needs-card")
    expect(card).to_be_visible()
    expect(card).to_contain_text(re.compile("sign in", re.I))
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false")

    session_link = page.get_by_test_id("share-link").inner_text()
    assert session_link == link + "#" + session

    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.get_by_test_id("copy-link").click()
    expect(page.get_by_test_id("copy-link")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == session_link

    # The link the agent sends lands a phone straight on this session.
    phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    phone_page = phone.new_page()
    sign_in(phone_page, session_link)
    expect(phone_page.get_by_test_id("needs-card")).to_be_visible(timeout=30000)
    assert phone_page.evaluate("location.hash.slice(1)") == session
    assert phone_page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the phone page scrolls sideways"

    # Done, carry on: Stealthbox picks up in the same tab. Nobody signed in, so it asks again.
    phone_page.get_by_test_id("carry-on").click()
    expect(phone_page.get_by_test_id("message-you").last.locator(".bubble")).to_have_text("I’m done, carry on")
    expect(phone_page.get_by_test_id("session-status")).to_have_text(
        re.compile("Waiting|Working"), timeout=30000)
    expect(phone_page.get_by_test_id("session-status")).to_have_text("Needs you", timeout=RUN_TIMEOUT)
    expect(phone_page.get_by_test_id("message-stealthbox")).to_have_count(2)
    assert_on_screen(phone_page, phone_page.get_by_test_id("message-stealthbox").last)
    assert_on_screen(phone_page, phone_page.get_by_test_id("reply"))
    phone.close()
    expect(status(page)).to_have_text("Needs you")

    page.get_by_test_id("close-needs").click()
    expect(list_item(page, session)).to_have_count(0)


# ---------------------------------------------------------------- tabs


def test_each_session_shows_its_own_browser_live_and_close_closes_it(page):
    first = start_session(page, "Open example.com and tell me the page's title.")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    second = start_session(page, "Open example.org and tell me the page's title.")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)

    for session, site in ((first, "example.com"), (second, "example.org")):
        list_item(page, session).click()
        expect(page.get_by_test_id("live-view")).to_be_visible()
        expect(page.get_by_test_id("live-view")).to_have_attribute("data-connected", "true", timeout=30000)
        expect(page.get_by_test_id("url")).to_have_value(re.compile(site), timeout=15000)

    open_before = api_get(page, "/v1/status")["session_browsers"]["open"]
    page.get_by_test_id("close").click()
    expect(list_item(page, second)).to_have_count(0)
    deadline = time.time() + 30
    while api_get(page, "/v1/status")["session_browsers"]["open"] != open_before - 1:
        assert time.time() < deadline, "the closed session's browser stayed open"
        time.sleep(1)
    list_item(page, first).click()
    page.get_by_test_id("close").click()
    expect(list_item(page, first)).to_have_count(0)


def test_the_live_browser_view_follows_a_program_driving_the_browser_over_cdp(browser, page):
    """A scraper on its own CDP connection makes no session; the console still shows its tab, live."""
    driver = stealthbox_browser()
    tab = driver.contexts[0].new_page()
    try:
        tab.goto("https://example.com/", wait_until="domcontentloaded")
        tab.bring_to_front()
        page.get_by_test_id("browser-item").click()
        expect(page.get_by_test_id("session-title")).to_have_text("example.com", timeout=15000)
        expect(page.get_by_test_id("live-view")).to_be_visible()

        tab.goto("https://www.iana.org/help/example-domains", wait_until="domcontentloaded")
        expect(page.get_by_test_id("session-title")).to_have_text("iana.org", timeout=15000)
        expect(page.get_by_test_id("url")).to_have_value(re.compile("iana.org/help/example-domains"))

        page.reload()
        expect(page.get_by_test_id("browser-item")).to_have_attribute("aria-current", "true", timeout=30000)
        expect(page.get_by_test_id("session-title")).to_have_text("iana.org", timeout=15000)
    finally:
        tab.close()
        driver.close()


def test_a_session_whose_browser_closed_with_its_last_tab_says_so_and_a_message_opens_it_again(browser, page):
    """Chrome quits with its last tab. Closed outside the console (in the viewer, or by
    another program), the session's browser is gone until its next message."""
    session = open_blank_session(page)
    view = api_get(page, f"/v1/sessions/{session}")
    own = session_browser(view)
    try:
        own.new_browser_cdp_session().send("Target.closeTarget", {"targetId": view["tab"]})
    except Exception:  # noqa: BLE001 - the browser can quit before it answers
        pass
    finally:
        own.close()
    expect(page.get_by_test_id("now-text")).to_contain_text("closed with its last tab", timeout=30000)
    base = page.url.split("/console")[0]
    response = page.request.post(f"{base}/v1/sessions/{session}/focus", headers={"Origin": base})
    assert response.status == 404 and "no browser open" in response.json()["error"], response.text()

    page.get_by_test_id("reply").fill("Open example.com and tell me the page's title.")
    page.get_by_test_id("send").click()
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    expect(stealthbox_messages(page).last.locator(".bubble")).to_contain_text(re.compile("example|domain", re.I))


def front_tab_url(page):
    return page.request.get(page.url.split("/console")[0] + "/v1/status").json()["front_tab"]["url"]


def test_the_address_bar_opens_what_is_typed_in_the_tab_in_front(page):
    page.get_by_test_id("browser-item").click()
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    url.type("example.org")
    url.press("Enter")
    expect(url).to_have_value(re.compile(r"^https://example\.org/?$"), timeout=15000)
    expect(page.get_by_test_id("session-title")).to_have_text("example.org", timeout=15000)
    assert "example.org" in front_tab_url(page)

    # Words are a search, as in Chrome's own address bar.
    url.click()
    url.press("ControlOrMeta+a")
    url.type("stealthbox address bar")
    url.press("Enter")
    expect(url).to_have_value(re.compile(r"^https://www\.bing\.com/search\?q=stealthbox"), timeout=15000)
    # The tab reports its new address once the navigation commits.
    deadline = time.time() + 15
    while not front_tab_url(page).startswith("https://www.bing.com/search"):
        assert time.time() < deadline, front_tab_url(page)
        time.sleep(0.5)


def test_full_screen_covers_the_window_and_the_button_leaves_it(page):
    page.get_by_test_id("browser-item").click()
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-connected", "true", timeout=30000)
    before = page.locator("#live canvas").bounding_box()
    page.get_by_test_id("fullscreen").click()
    expect(page.get_by_test_id("fullscreen")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("fullscreen")).to_have_text("Exit full screen")
    page.wait_for_timeout(1000)
    frame = page.locator("#s-browser").bounding_box()
    size = page.viewport_size
    assert abs(frame["width"] - size["width"]) < 2 and abs(frame["height"] - size["height"]) < 2, frame
    after = page.locator("#live canvas").bounding_box()
    assert after["width"] * after["height"] > before["width"] * before["height"] * 1.2, (before, after)

    page.get_by_test_id("fullscreen").click()
    expect(page.get_by_test_id("fullscreen")).to_have_attribute("aria-pressed", "false")
    page.wait_for_timeout(500)
    assert page.locator("#s-browser").bounding_box()["width"] < size["width"] - 50


def test_the_sidebar_button_hides_the_sessions_and_it_stays_hidden_after_a_refresh(page):
    toggle = page.get_by_test_id("sidebar-toggle")
    rail = page.locator("nav.rail")
    expect(toggle).to_have_attribute("aria-label", "Hide sidebar")
    narrow = page.locator("#s-browser").bounding_box()["width"]
    toggle.click()
    expect(rail).to_be_hidden()
    expect(toggle).to_have_attribute("aria-label", "Show sidebar")
    page.wait_for_timeout(500)
    assert page.locator("#s-browser").bounding_box()["width"] > narrow + 200

    page.reload()
    expect(page.get_by_test_id("events-state")).to_have_text("Live updates on", timeout=30000)
    expect(rail).to_be_hidden()
    toggle.click()
    expect(rail).to_be_visible()
    expect(toggle).to_have_attribute("aria-label", "Hide sidebar")


def typed_into_the_browser(browser, marker):
    driver = stealthbox_browser()
    try:
        tab = next(item for item in driver.contexts[0].pages if marker in item.url)
        return tab.evaluate("document.getElementById('t').value")
    finally:
        driver.close()


def test_typing_in_the_live_browser_reaches_the_page(browser, page):
    marker = f"typing-{int(time.time())}"
    page.get_by_test_id("browser-item").click()
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    url.type(f"data:text/html,<title>{marker}</title><textarea id=t style='width:100vw;height:100vh;font-size:40px'></textarea>")
    url.press("Enter")
    expect(page.get_by_test_id("live-view")).to_have_attribute("data-view-only", "false", timeout=15000)
    page.wait_for_timeout(1500)
    page.locator("#live canvas").click()
    # The click reaches the remote page over VNC; let it focus the box before typing.
    page.wait_for_timeout(700)
    page.keyboard.type("hello stealthbox")
    deadline = time.time() + 10
    while typed_into_the_browser(browser, marker) != "hello stealthbox":
        assert time.time() < deadline, typed_into_the_browser(browser, marker)
        time.sleep(0.5)


def test_a_message_from_the_browser_view_works_in_the_tab_in_front(page):
    page.get_by_test_id("browser-item").click()
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    url.type("example.com")
    url.press("Enter")
    expect(page.get_by_test_id("session-title")).to_have_text("example.com", timeout=15000)
    tab = page.request.get(page.url.split("/console")[0] + "/v1/status").json()["front_tab"]["id"]
    expect(page.get_by_test_id("browser-hint")).to_be_visible()

    page.get_by_test_id("reply").fill("What is this page's title? Answer in a few words.")
    page.get_by_test_id("reply").press("Enter")
    page.wait_for_function("location.hash.startsWith('#s_')")
    session = page.evaluate("location.hash.slice(1)")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    view = page.request.get(page.url.split("/console")[0] + f"/v1/sessions/{session}").json()
    assert view["tab"] == tab
    expect(stealthbox_messages(page).last.locator(".bubble")).to_contain_text(re.compile("example|domain", re.I))


def test_the_sidebar_shows_a_live_preview_of_the_browser(page):
    base = page.url.split("/console")[0]
    preview = page.get_by_test_id("browser-item").locator("img")
    expect(preview).to_have_attribute("src", re.compile("/v1/browser/screenshot"))
    page.wait_for_function("() => { const i = document.querySelector('[data-testid=browser-item] img'); "
                           "return i && i.complete && i.naturalWidth >= 300; }")
    first = preview.get_attribute("src")
    before = page.request.get(base + "/v1/browser/screenshot").body()
    page.get_by_test_id("browser-item").click()
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    url.type("www.iana.org/help/example-domains")
    url.press("Enter")
    page.wait_for_timeout(4000)
    assert page.request.get(base + "/v1/browser/screenshot").body() != before
    expect(preview).not_to_have_attribute("src", first)



def test_the_live_view_shows_the_screen_while_it_connects(page):
    # Hold the live view's websocket open without connecting, as on a slow share link.
    page.route_web_socket("**/viewer/websockify", lambda websocket: None)
    page.reload()
    page.get_by_test_id("browser-item").click()
    poster = page.get_by_test_id("poster")
    expect(poster).to_be_visible()
    expect(poster).to_have_attribute("src", re.compile(r"/v1/browser/screenshot\?width=\d+"))
    page.wait_for_function("() => { const i = document.querySelector('[data-testid=poster]'); "
                           "return i.complete && i.naturalWidth > 320; }")
    expect(page.get_by_test_id("live-view")).not_to_have_attribute("data-connected", "true")

POPUP_PAGE = ("data:text/html,<title>Course list</title><button style='font-size:48px;margin:80px' onclick=\"setTimeout(()=>{"
              "const w=window.open('','course','width=900,height=650');w.document.write('<title>Course player</title>"
              "<h1 style=font-size:64px>Course player ready</h1>')},1500)\">Open course</button>")


def front_tab(page):
    return page.request.get(page.url.split("/console")[0] + "/v1/status").json()["front_tab"]


def open_in_browser_view(page, address):
    if page.get_by_test_id("browser-item").count():
        page.get_by_test_id("browser-item").click()
    url = page.get_by_test_id("url")
    url.click()
    url.press("ControlOrMeta+a")
    url.type(address)
    url.press("Enter")


def test_the_agent_works_in_a_popup_its_click_opens_and_keeps_it_in_front(page):
    """A course player opens in a popup window a moment after the click (LRN does this)."""
    open_in_browser_view(page, POPUP_PAGE)
    page.wait_for_timeout(2000)
    page.get_by_test_id("reply").fill("Click the Open course button, then tell me the big heading in the window that opens.")
    page.get_by_test_id("reply").press("Enter")
    page.wait_for_function("location.hash.startsWith('#s_')")
    session = page.evaluate("location.hash.slice(1)")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    view = page.request.get(page.url.split("/console")[0] + f"/v1/sessions/{session}").json()
    assert "course player ready" in (view["messages"][-1]["text"] or "").lower()
    front = front_tab(page)
    assert front["title"] == "Course player" and view["tab"] == front["id"]

    # One entry for that tab: the session, not a Browser entry beside it.
    expect(page.get_by_test_id("browser-item")).to_have_count(0)


def test_the_address_bar_suggests_from_history_and_completes_in_place(page):
    open_in_browser_view(page, "example.org")
    expect(page.get_by_test_id("url")).to_have_value(re.compile(r"^https://example\.org/?$"), timeout=15000)
    url = page.get_by_test_id("url")
    # Chrome writes its history every few seconds.
    deadline = time.time() + 40
    while True:
        url.click()
        url.press("ControlOrMeta+a")
        url.press("Backspace")
        url.type("exampl", delay=60)
        page.wait_for_timeout(800)
        if page.get_by_test_id("url-suggestions").is_visible():
            break
        assert time.time() < deadline, "no suggestions from history"
        page.wait_for_timeout(4000)
    assert url.input_value().startswith("example.")
    assert page.evaluate("(() => { const i = document.getElementById('s-url'); return i.value.slice(i.selectionStart, i.selectionEnd); })()") \
        == url.input_value()[len("exampl"):]
    url.press("Enter")
    expect(url).to_have_value(re.compile(r"^https://(www\.)?example\."), timeout=15000)


# ---------------------------------------------------------------- when things go wrong


# A failure a test puts in on purpose; the page fixture does not count it.
FAULT = {"status": 502, "body": "", "headers": {"x-test-fault": "injected"}}


def idle_seconds(page, session):
    return next(s["idle_seconds"] for s in api_get(page, "/v1/status")["sessions"] if s["session"] == session)


def test_browsing_focusing_or_archiving_a_session_keeps_it_from_expiring(page):
    """Idle sessions expire and their tabs close; a blank session the user browses
    in, focuses or archives is in use, though no run ever touched it."""
    base = page.url.split("/console")[0]
    headers = {"Origin": base}
    session = open_blank_session(page)
    for use in (
            lambda: page.request.post(base + "/v1/browser/navigate", headers=headers,
                                      data={"url": "example.com", "session": session}),
            lambda: page.request.post(f"{base}/v1/sessions/{session}/focus", headers=headers),
            lambda: page.request.post(f"{base}/v1/sessions/{session}/archive", headers=headers),
            lambda: page.request.post(f"{base}/v1/sessions/{session}/unarchive", headers=headers)):
        time.sleep(3)
        assert idle_seconds(page, session) >= 3
        assert use().ok
        assert idle_seconds(page, session) <= 1


def test_two_consoles_closing_one_session_at_once_get_one_close_and_one_not_found(page):
    base = page.url.split("/console")[0]
    session = open_blank_session(page)
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in page.context.cookies())
    answers = []

    def close():
        request = urllib.request.Request(f"{base}/v1/sessions/{session}/close", method="POST", data=b"",
                                         headers={"Cookie": cookie, "Origin": base})
        try:
            answers.append(urllib.request.urlopen(request, timeout=20).status)
        except urllib.error.HTTPError as error:
            answers.append(error.code)

    threads = [threading.Thread(target=close) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answers) == [200, 404]
    expect(list_item(page, session)).to_have_count(0)


def test_an_error_from_the_refresh_clears_when_the_next_refresh_works(page):
    """One blip through the share used to leave its error over the session for good."""
    page.route("**/v1/status", lambda route: route.fulfill(**FAULT))
    expect(page.get_by_test_id("error")).to_have_text("Stealthbox did not answer GET /v1/status (HTTP 502).",
                                                      timeout=10000)
    page.unroute("**/v1/status")
    expect(page.get_by_test_id("error")).to_be_hidden(timeout=10000)

    # An action's error stays until the next action, whatever the refresh does.
    page.route("**/v1/browser/navigate", lambda route: route.fulfill(**FAULT))
    page.get_by_test_id("browser-item").click()
    open_in_browser_view(page, "example.com")
    error = page.get_by_test_id("error")
    expect(error).to_have_text("Stealthbox did not answer POST /v1/browser/navigate (HTTP 502).", timeout=10000)
    page.wait_for_timeout(5000)
    expect(error).to_be_visible()
    page.unroute("**/v1/browser/navigate")


def needs_you(page, session, share_url):
    """The console shows `session` as waiting on the user, with or without a share link."""
    def answer(route):
        response = route.fetch()
        sessions = response.json()
        for item in sessions["sessions"]:
            if item["session"] == session:
                item.update(state="needs", task="Sign in to carry on.",
                            needs_user={"task": "Sign in to carry on.", "share_url": share_url})
        route.fulfill(response=response, json=sessions)
    page.route("**/v1/sessions", answer)


def test_the_phone_link_shows_only_while_there_is_a_share(page):
    session = open_blank_session(page)
    needs_you(page, session, None)
    expect(page.get_by_test_id("needs-card")).to_be_visible(timeout=10000)
    expect(page.get_by_test_id("phone")).to_be_hidden()

    page.unroute("**/v1/sessions")
    needs_you(page, session, "https://example.trycloudflare.com/console#" + session)
    expect(page.get_by_test_id("phone")).to_be_visible(timeout=10000)
    expect(page.get_by_test_id("share-link")).to_have_text("https://example.trycloudflare.com/console#" + session)
    page.unroute("**/v1/sessions")


def test_the_address_bar_keys_walk_the_suggestions_and_escape_puts_back_what_was_typed(page):
    open_in_browser_view(page, "example.org")
    url = page.get_by_test_id("url")
    expect(url).to_have_value(re.compile(r"^https://example\.org/?$"), timeout=15000)
    suggestions = page.get_by_test_id("url-suggestions")
    deadline = time.time() + 40
    while True:
        url.click()
        url.press("ControlOrMeta+a")
        url.press("Backspace")
        url.type("exampl", delay=60)
        page.wait_for_timeout(800)
        if suggestions.is_visible():
            break
        assert time.time() < deadline, "no suggestions from history"
        page.wait_for_timeout(4000)
    # Delete takes off the completion, and a deletion does not complete again.
    url.press("Delete")
    page.wait_for_timeout(500)
    assert url.input_value() == "exampl"
    url.press("ArrowDown")
    first = suggestions.locator("li").first
    expect(first).to_have_attribute("aria-selected", "true")
    assert url.input_value() == first.locator(".address").inner_text()
    url.press("ArrowUp")
    expect(url).to_have_value("exampl")
    url.press("Escape")
    expect(suggestions).to_be_hidden()
    expect(url).to_have_value("exampl")
    expect(url).to_be_focused()


def test_the_console_reconnects_after_its_updates_stop(page):
    page.route("**/v1/events/next*", lambda route: route.abort("aborted"))
    # The long poll in flight ends within its 25 seconds; the next one fails.
    expect(page.get_by_test_id("events-state")).to_have_text("Reconnecting…", timeout=40000)
    page.unroute("**/v1/events/next*")
    expect(page.get_by_test_id("events-state")).to_have_text("Live updates on", timeout=15000)
    # And it still hears about what happens.
    session = open_blank_session(page)
    expect(list_item(page, session)).to_be_visible(timeout=10000)


@pytest.mark.skipif(bool(CONSOLE_URL), reason="needs the share and this folder's Stealthbox")
def test_the_share_keeps_its_address_when_stealthbox_restarts(link):
    before = json.loads(stealthbox("status", "--json"))["share_url"]
    assert before
    subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "stealthbox"], cwd=ROOT, check=True,
                   capture_output=True)
    deadline = time.time() + 300
    while json.loads(stealthbox("status", "--json"))["container"] != "healthy":
        assert time.time() < deadline, "Stealthbox did not come back"
        time.sleep(3)
    assert json.loads(stealthbox("status", "--json"))["share_url"] == before


# ---------------------------------------------------------------- unshare


def test_unshare_expires_the_link_and_a_new_share_asks_again(browser, link):
    context = browser.new_context()
    page = context.new_page()
    sign_in(page, link)

    stealthbox("unshare")
    gone = browser.new_context()
    response = gone.new_page().goto(link)
    assert response.status >= 400, "the old link still answers after unshare"
    gone.close()

    fresh = share_link()
    assert fresh != link
    wait_reachable(browser, fresh)
    # A new share is a new address: the cookie from the old one does not go there.
    page.goto(fresh)
    expect(page.get_by_role("heading", name="Sign in to the console")).to_be_visible(timeout=60000)
    sign_in(page, fresh)
    context.close()
