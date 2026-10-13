"""Enter a site from Bing: search the domain, click the organic result."""

import os
import random
import sys
import time
from urllib.parse import urlparse

from patchright.sync_api import sync_playwright

import humanize

CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
BING = "https://www.bing.com/"
SEARCH_BOXES = (
    'textarea[name="q"]',
    'input[name="q"]',
    "#sb_form_q",
)
SEARCH_BUTTONS = (
    "#search_icon",
    "#sb_form_go",
    'button[aria-label="Search"]',
)
CONSENT_BUTTONS = (
    "#bnp_btn_accept",
    'button:has-text("Accept")',
)

###############################################################################

def _host(url):
    name = (urlparse(url).hostname or "").lower()
    if name.startswith("www."):
        return name[4:]
    return name

###############################################################################

def _same_site(domain, href):
    host = _host(href)
    domain = domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)

###############################################################################

def _brand_site(domain, href, title):
    """A host that carries the domain's brand: some brands live on another host.

    Some stores redirect to a subdomain of their parent company's site (a host
    label is the brand), and some sites live on a longer name that ends with the
    brand (and the page is titled with it). A brand inside another name is not
    enough: a railway called "Canyon Example" at canyonexample.com is not
    example.com.
    """
    brand = domain.lower().removeprefix("www.").split(".")[0]
    labels = _host(href).split(".")
    if brand in labels:
        return True
    return any(label.endswith(brand) for label in labels) and (title or "").strip().lower().startswith(brand)

###############################################################################

def _visible(page, selector):
    locator = page.locator(selector).first
    if locator.count() == 0:
        return None
    if not locator.is_visible():
        return None
    return locator

###############################################################################

def _first_visible(page, selectors):
    for selector in selectors:
        locator = _visible(page, selector)
        if locator is not None:
            return locator
    return None

###############################################################################

def _dismiss_consent(page):
    button = _first_visible(page, CONSENT_BUTTONS)
    if button is None:
        return
    humanize.human_click(page, button)
    humanize.pause(0.4, 0.9)

###############################################################################

def _cite_host(link):
    """The host a result's cite shows: "https://www.example.com › intl › en" is example.com."""
    block = link.locator("xpath=ancestor::li[contains(@class,'b_algo')][1]")
    cite = block.locator("cite").first
    text = cite.inner_text().split("›")[0].strip() if cite.count() else ""
    return _host(text if "://" in text else "https://" + text), cite.inner_text() if cite.count() else ""

###############################################################################

def _cites_domain(link, domain):
    # By host, not substring: "myexample.com" contains "example.com".
    return _same_site(domain, "https://" + _cite_host(link)[0])

###############################################################################

def _front_page_rank(link, domain):
    """Sort key: the domain's own host before other subdomains, then the shallowest page.

    From the cite: "https://www.example.com › intl › en" is www.example.com, two deep.
    """
    host, text = _cite_host(link)
    return host != domain.lower().removeprefix("www."), text.count("›")

###############################################################################

def _organic_link(page, domain, skip=0):
    links = page.locator("#b_results li.b_algo:not(.b_ad) h2 a")
    # Under load Bing sometimes answers with the search bar and an empty result
    # list, filled in a few seconds later or not at all; give it the time.
    deadline = time.time() + 10
    while links.count() == 0 and time.time() < deadline:
        time.sleep(0.5)
    matches = []
    for index in range(links.count()):
        link = links.nth(index)
        href = link.get_attribute("href") or ""
        if _same_site(domain, href) or _cites_domain(link, domain):
            matches.append(link)
    if not matches:
        matches = [links.nth(index) for index in range(links.count())
                   # The href is Bing's click tracker; the cite shows where it goes.
                   if _brand_site(domain, "https://" + _cite_host(links.nth(index))[0], links.nth(index).inner_text())]
    if not matches:
        raise RuntimeError(f"No organic Bing result for {domain}")
    # The site's own front page before a deep link or another subdomain: for an
    # airline, Bing ranked a regional page (a booking form that never opens its
    # airport picker) and a staff sign-in subdomain above the www front page.
    matches.sort(key=lambda link: _front_page_rank(link, domain))
    return matches[min(skip, len(matches) - 1)]

###############################################################################

def _is_rebates_link(url):
    """A Bing Rebates wrapper ("Up to $8 cash back"): `/rebates/welcome?url=...`.

    Bing shows it in place of the plain organic link to some profiles (a fresh
    one got it for a retail site, an older one did not). Clicking it opens Bing's
    rebates sign-up rather than the site, so it never navigates.
    """
    return "/rebates/welcome" in (url or "")


def _resolve_bing_redirect(url):
    """Decode a Bing click-tracker or rebates URL to the page it points at.

    Bing's redirect can stall as a top-level navigation (it expects the click it
    was built for), which made ~30% of entries fail. The destination is in the
    `u` parameter: base64url with an `a1` prefix. A rebates link carries it
    plainly in `url`.
    """
    import base64
    from urllib.parse import parse_qs

    if _is_rebates_link(url):
        destination = (parse_qs(urlparse(url).query).get("url") or [""])[0]
        return destination if destination.startswith("http") else None
    if "bing.com/ck/a" not in (url or ""):
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:  # noqa: BLE001
        return None
    raw = (params.get("u") or [""])[0]
    if not raw:
        return None
    if raw.startswith("a1"):
        raw = raw[2:]
    raw += "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    return decoded if decoded.startswith("http") else None


def _click_and_follow(page, link, domain=None):
    """Click a result and return the page it landed on.

    Bing usually opens the target in a new tab through a `bing.com/ck/a`
    redirect, and that tab can take several seconds to leave `about:blank`.
    Waiting a fixed 0.8-1.6s and checking `context.pages` once misses it, so
    the caller ends up still on Bing. Poll for the new tab, then wait for it
    to reach a real URL.

    The polls wait through Playwright (`wait_for_timeout`), not time.sleep: its
    sync API takes in browser events only during its own calls, so with a sleep
    the new tab never appeared in `context.pages` and no URL ever changed. The
    click looked lost, the result was clicked again 12s later from the Bing tab
    (a second tab, and the screen jumping back to Bing), and each wait below ran
    to its deadline.
    """
    context = page.context
    before = set(context.pages)
    start_url = page.url

    landed = None
    # A rebates link never navigates when clicked; go straight to the fallback.
    clicks = 0 if _is_rebates_link(link.get_attribute("href")) else 3
    for attempt in range(clicks):
        humanize.human_click(page, link)
        deadline = time.time() + 12
        while time.time() < deadline:
            page.wait_for_timeout(random.randint(250, 450))
            opened = [item for item in context.pages if item not in before]
            if opened:
                landed = opened[-1]
                break
            if page.url != start_url:
                landed = page
                break
        if landed is not None:
            break
        # Bing sometimes swallows the first click; click again.
        humanize.pause(0.6, 1.1)

    if landed is None and domain is not None:
        # Last resort: follow Bing's own click-tracking redirect. The click just
        # never fires on some results, so resolve the destination Bing encoded in
        # the ck/a link and go there. This stays on the Bing path.
        href = link.get_attribute("href") or ""
        target = _resolve_bing_redirect(href) or href
        if target.startswith("http"):
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=30000)
                landed = page
            except Exception:  # noqa: BLE001
                landed = page
    if landed is None:
        landed = page

    # If we are sitting on a bing.com/ck/a redirect, resolve it and move on.
    deadline = time.time() + 8
    while time.time() < deadline and domain is not None:
        if _same_site(domain, landed.url or ""):
            break
        if "bing.com/ck/a" not in (landed.url or ""):
            break
        landed.wait_for_timeout(random.randint(300, 500))
    if domain is not None and "bing.com/ck/a" in (landed.url or ""):
        resolved = _resolve_bing_redirect(landed.url)
        if resolved:
            try:
                landed.goto(resolved, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass

    # Wait for the tab to leave about:blank / the Bing redirect.
    deadline = time.time() + 20
    while time.time() < deadline:
        url = landed.url or ""
        if url and url != "about:blank" and "chrome://" not in url:
            if domain is None or _same_site(domain, url) or "bing.com" not in _host(url):
                break
        landed.wait_for_timeout(random.randint(300, 500))

    try:
        landed.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:  # noqa: BLE001 - a slow site is not a failure
        pass
    # A click that looked lost can still open its tab late, after the next click
    # opened another: two tabs of one site, the run driving the one behind while the
    # screen (screenshots and real input) showed the other. Keep only the landed tab.
    for extra in [item for item in context.pages if item not in before and item != landed]:
        extra.close()
    landed.bring_to_front()
    humanize.pause(0.8, 1.6)
    return landed

###############################################################################

def _enter(page, domain, skip=0):
    """One attempt: search the domain on Bing and click the organic result."""
    box = _first_visible(page, SEARCH_BOXES)
    if box is None:
        raise RuntimeError("Bing search box not found")
    humanize.human_click(page, box)
    humanize.pause(0.2, 0.5)
    humanize.human_type(page, domain)
    humanize.pause(0.4, 0.9)
    button = _first_visible(page, SEARCH_BUTTONS)
    if button is None:
        raise RuntimeError("Bing search button not found")
    humanize.human_click(page, button)
    page.wait_for_load_state("domcontentloaded")
    humanize.pause(0.8, 1.6)
    link = _organic_link(page, domain, skip=skip)
    return _click_and_follow(page, link, domain=domain)

###############################################################################

def open_via_bing(page, domain, query=None, attempts=4):
    """Enter `domain` through Bing, retrying if the first click does not land.

    Raises RuntimeError if it never reaches the domain, so callers can tell a
    failed entry apart from a site that simply had no challenge.
    """
    if query is None:
        query = domain
    landed = page
    for attempt in range(attempts):
        try:
            landed.goto(BING, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001 - Bing redirects mid-load sometimes
            pass
        humanize.pause(0.8, 1.6)
        _dismiss_consent(landed)
        try:
            # Later attempts ask for the official site: Bing can bury a brand's own
            # domain under same-named results.
            landed = _enter(landed, query if attempt < 2 else f"{query} official site", skip=attempt % 2)
        except RuntimeError:
            if attempt == attempts - 1:
                raise
            # An empty result page is Bing throttling; searching again at once
            # gets another one. Measured: three agents sharing an IP saw it.
            time.sleep(5 * (attempt + 1))
            continue
        if _same_site(domain, landed.url) or _brand_site(domain, landed.url, landed.title()):
            return landed
        humanize.pause(0.6, 1.2)
    raise RuntimeError(f"never reached {domain}; last url {landed.url[:120]}")

###############################################################################

def main():
    domain = sys.argv[1] if len(sys.argv) > 1 else "example.com"
    query = sys.argv[2] if len(sys.argv) > 2 else None
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].new_page()
        landed = open_via_bing(page, domain, query)
        print(f"Page title: {landed.title()}")
        print(f"URL: {landed.url}")
        print("The tab stays open - look for it in the viewer.")

###############################################################################

if __name__ == "__main__":
    main()
