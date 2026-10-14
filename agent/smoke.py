"""`stealthbox smoke`: show that the install works by reaching a few sites.

The sites are the caller's, or with none named, the list in <data>/smoke-sites.txt
(one domain per line), which a site pack installs.

Each site is entered the normal way (Bing first, humanized), any bot check is
worked by the agent, and the landed page is screenshotted. The result is a JSON
summary plus report.html with the screenshots, in <data>/smoke/<id>/, which the
host CLI opens. It runs on the API's browser worker, so it never overlaps a chat.
"""

from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time

import steps as agent

import bing_first
import humanize

SITES_PATH = os.path.join(agent.DATA_DIR, "smoke-sites.txt")
CHALLENGE_STEPS = 12
CHALLENGE_MESSAGE = ("A bot check is blocking this page. Work it so the real page loads, "
                     "then reply done. Do not navigate anywhere else.")


def default_sites():
    """The domains in smoke-sites.txt, or [] when there is no list."""
    if not os.path.exists(SITES_PATH):
        return []
    with open(SITES_PATH) as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]


class Smoke:
    def __init__(self, sites, host_data_dir):
        self.id = f"smoke_{time.strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(3)}"
        self.sites = list(sites)
        self.dir = os.path.join(agent.DATA_DIR, "smoke", self.id)
        self.host_dir = host_join(host_data_dir, "smoke", self.id) if host_data_dir else None
        self.results = []
        self.status = "queued"
        self.done = threading.Event()

    def view(self):
        return {
            "smoke": self.id,
            "status": self.status,
            "passed": sum(1 for result in self.results if result["ok"]),
            "total": len(self.sites),
            "results": self.results,
            "report": os.path.join(self.dir, "report.html"),
            "host_report": host_join(self.host_dir, "report.html") if self.host_dir else None,
        }


def host_join(base, *parts):
    """Join a host path, which on Windows uses backslashes."""
    separator = "\\" if "\\" in base else "/"
    return separator.join([base.rstrip("\\/"), *parts])


def check_site(context, smoke, site, notebook, has_key):
    """Enter one site and screenshot it. Never raises: a failure is a result."""
    result = {"site": site, "ok": False, "challenge": [], "worked_challenge": False}
    before = set(context.pages)
    page = context.new_page()
    page.set_default_timeout(30000)
    page.bring_to_front()
    landed = page
    try:
        landed = bing_first.open_via_bing(page, site)
        humanize.pause(3.0, 4.5)
        humanize.reset_zoom(landed)
        blocks = agent.detect_block(landed)
        result["challenge"] = blocks
        if blocks and has_key:
            result["worked_challenge"] = True
            run_dir = os.path.join(smoke.dir, "work", site)
            turn = agent.Turn(f"{smoke.id}-{site}", context, CHALLENGE_MESSAGE, [], notebook,
                              run_dir, CHALLENGE_STEPS, agent.DEFAULT_MODEL)
            _, _, landed = agent.run_turn(landed, turn, lambda _turn: None, lambda *_phase: None)
            humanize.pause(2.0, 3.0)
            blocks = agent.detect_block(landed)
        result["blocked_by"] = blocks
        result["ok"] = not blocks
        result["title"] = (landed.title() or "")[:120]
        result["url"] = landed.url
        if blocks and not has_key:
            result["note"] = "A bot check is up; with the OpenRouter key set, Stealthbox works it."
    except Exception as exc:  # noqa: BLE001 - one site's failure is a result, not a crash
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    shot = f"{site}.png"
    if agent.safe_screenshot(landed, os.path.join(smoke.dir, shot)):
        result["screenshot"] = shot
        if smoke.host_dir:
            result["host_screenshot"] = host_join(smoke.host_dir, shot)
    # Every tab this check opened, not just the first and the last: Bing opens the
    # site in a new tab, and the agent may move to another while it works a check.
    # Left open, they piled up until the browser ran out of memory.
    for opened in set(context.pages) - before:
        if not opened.is_closed():
            opened.close()
    return result


def run(context, smoke, notebook, has_key):
    smoke.status = "running"
    os.makedirs(smoke.dir, exist_ok=True)
    for site in smoke.sites:
        smoke.results.append(check_site(context, smoke, site, notebook, has_key))
        write(smoke)
    smoke.status = "done"
    write(smoke)


def write(smoke):
    with open(os.path.join(smoke.dir, "result.json"), "w") as fh:
        json.dump(smoke.view(), fh, indent=2)
    with open(os.path.join(smoke.dir, "report.html"), "w") as fh:
        fh.write(report_html(smoke))


def report_html(smoke):
    cards = []
    for result in smoke.results:
        mark = "&#10003;" if result["ok"] else "&#10007;"
        state = "ok" if result["ok"] else "bad"
        detail = result.get("title") or result.get("error") or ""
        if result["worked_challenge"] and result["ok"]:
            detail = f"Cleared a bot check ({', '.join(result['challenge'])}). " + detail
        elif not result["ok"] and result.get("blocked_by"):
            detail = f"Stopped by a bot check ({', '.join(result['blocked_by'])}). " + result.get("note", "")
        image = (f'<img src="{html.escape(result["screenshot"])}" alt="{html.escape(result["site"])}">'
                 if result.get("screenshot") else '<div class="noshot">No screenshot</div>')
        cards.append(f"""<figure class="{state}">{image}<figcaption><strong><span class="mark">{mark}</span>
{html.escape(result["site"])}</strong><span>{html.escape(detail)}</span></figcaption></figure>""")
    passed = sum(1 for result in smoke.results if result["ok"])
    pending = "" if smoke.status == "done" else '<p class="muted">Still working; refresh in a minute.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stealthbox smoke test</title>
<style>
  :root {{ --bg: #f6f5f2; --card: #fff; --ink: #1d1d1b; --muted: #6b6a66; --line: #dddbd5;
          --good: #2f6b3a; --bad: #9a2f2f; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #171716; --card: #222220; --ink: #ecebe6; --muted: #a3a29c; --line: #3a3936;
            --good: #8fcf98; --bad: #ec8f8f; }}
  }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
         font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  main {{ max-width: 72rem; margin: 0 auto; padding: 32px 16px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
  .muted {{ color: var(--muted); margin: 0 0 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 30rem), 1fr)); gap: 16px; }}
  figure {{ margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
  img {{ display: block; width: 100%; height: auto; border-bottom: 1px solid var(--line); }}
  .noshot {{ padding: 64px 16px; text-align: center; color: var(--muted); }}
  figcaption {{ padding: 12px 16px; display: flex; flex-direction: column; gap: 2px; }}
  figcaption span {{ color: var(--muted); font-size: .9rem; }}
  .ok .mark {{ color: var(--good); }} .bad .mark {{ color: var(--bad); }}
</style></head>
<body><main>
<h1>Stealthbox reached {passed} of {len(smoke.sites)} sites</h1>
<p class="muted">Each site was entered from a Bing search, the way a person would, in your
Stealthbox browser. These are the pages it landed on.</p>
{pending}
<div class="grid">{''.join(cards)}</div>
</main></body></html>"""
