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
import time

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


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[f"--host-resolver-rules=MAP *.trycloudflare.com {cloudflare_address()}"])
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
    yield page
    # Close what this test opened, so the next one starts from the same list.
    for session in context.request.get(base + "/v1/sessions").json()["sessions"]:
        if session["session"] not in before and session["state"] not in ("working", "paused", "waiting"):
            context.request.post(f"{base}/v1/sessions/{session['session']}/close")
    context.close()


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
    assert cookie["httpOnly"] and cookie["secure"] and cookie["sameSite"] == "Lax"
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
    session = start_session(page, "Open example.com and tell me the page's main heading.")
    expect(list_item(page, session)).to_be_visible()
    expect(status(page)).to_have_text(re.compile("Waiting its turn|Working"), timeout=30000)

    # Progress inside the first step: what it is doing, for how long, and the live tab.
    now = page.get_by_test_id("now")
    expect(now).to_be_visible(timeout=RUN_TIMEOUT)
    expect(stealthbox_messages(page).last).to_have_attribute(
        "data-phase", re.compile("starting|looking|thinking|acting"))
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
    session = start_session(page, "Open example.com and tell me the page's main heading.")
    expect(status(page)).to_have_text(re.compile("Waiting its turn|Working"), timeout=30000)
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
    start_session(page, "Go to wikipedia.org, search for Austin, Texas, open the article, "
                        "and tell me the population in the infobox and the name of the mayor.")
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
        re.compile("Waiting its turn|Working"), timeout=30000)
    expect(phone_page.get_by_test_id("session-status")).to_have_text("Needs you", timeout=RUN_TIMEOUT)
    expect(phone_page.get_by_test_id("message-stealthbox")).to_have_count(2)
    assert_on_screen(phone_page, phone_page.get_by_test_id("message-stealthbox").last)
    assert_on_screen(phone_page, phone_page.get_by_test_id("reply"))
    phone.close()
    expect(status(page)).to_have_text("Needs you")

    page.get_by_test_id("close-needs").click()
    expect(list_item(page, session)).to_have_count(0)


# ---------------------------------------------------------------- tabs


def test_show_it_live_brings_a_background_session_to_the_front_and_close_forgets_it(page):
    first = start_session(page, "Open example.com and tell me the page's main heading.")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)
    second = start_session(page, "Open example.org and tell me the page's main heading.")
    expect(status(page)).to_have_text("Finished", timeout=RUN_TIMEOUT)

    list_item(page, first).click()
    expect(page.get_by_test_id("still-view")).to_be_visible()
    page.get_by_test_id("show-live").click()
    expect(page.get_by_test_id("live-view")).to_be_visible(timeout=30000)

    list_item(page, second).click()
    expect(page.get_by_test_id("still-view")).to_be_visible()

    page.get_by_test_id("close").click()
    expect(list_item(page, second)).to_have_count(0)
    list_item(page, first).click()
    page.get_by_test_id("close").click()
    expect(list_item(page, first)).to_have_count(0)


def test_the_live_browser_view_follows_a_program_driving_the_browser_over_cdp(browser, page):
    """A scraper on its own CDP connection makes no session; the console still shows its tab, live."""
    driver = browser.browser_type.connect_over_cdp(CDP_URL or json.loads(stealthbox("status", "--json"))["cdp_url"])
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
    assert front_tab_url(page).startswith("https://www.bing.com/search")


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
    driver = browser.browser_type.connect_over_cdp(CDP_URL or json.loads(stealthbox("status", "--json"))["cdp_url"])
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

    page.get_by_test_id("reply").fill("What is the main heading on this page? Answer in a few words.")
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
