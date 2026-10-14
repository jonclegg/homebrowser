"""Setup: pages where the user types the OpenRouter key and the console password,
so neither goes through an agent's chat.

`stealthbox key` asks the API for a one-time link (`/setup?code=...`, 15 minutes).
The user opens it in their own browser, or in Stealthbox's browser through the
viewer when they are away from the machine, pastes the key and chooses the
console password. Stealthbox checks the key with OpenRouter and writes
<data>/openrouter.key, mode 600. The model reads that file on every call, so
nothing restarts. Once a password is set, the page can leave it as it is. Saving
signs that browser in to the console and opens it, except in Stealthbox's own
browser, which the user is already watching through the console.

The page takes no bearer token (the user has none), so it is guarded instead:
the one-time code, and a Host header that must be localhost, which stops another
website in the user's browser from swapping in a key of its own.

`stealthbox password` gives a one-time link (`/password?code=...`) to the password
alone, for an install that has none yet or a user who forgot it. That one works
through the share too, since the user is often away; the code is what guards it.
"""

from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

import steps as agent
import console_auth

CODE_SECONDS = 15 * 60
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class SetupCodes:
    """One-time setup codes, and what happened to the latest one.

    `progress` lets the agent that sent the link tell "not opened yet" from
    "opened but not submitted", "rejected by OpenRouter" and "expired", without
    ever seeing the key: issued -> opened -> rejected (with the reason) | saved.
    """

    def __init__(self):
        self.codes = {}
        self.lock = threading.Lock()
        self.latest = None
        self.progress = None

    def issue(self):
        code = secrets.token_urlsafe(16)
        with self.lock:
            now = time.time()
            self.codes = {key: expiry for key, expiry in self.codes.items() if expiry > now}
            self.codes[code] = now + CODE_SECONDS
            self.latest = code
            self.progress = {"state": "issued", "expires_in_seconds": CODE_SECONDS}
        return code

    def note(self, code, state, error=None):
        """Record what happened to `code`, if it is the latest one."""
        with self.lock:
            if code == self.latest:
                self.progress = {"state": state, "error": error} if error else {"state": state}

    def status(self):
        with self.lock:
            if self.progress is None:
                return None
            progress = dict(self.progress)
            expiry = self.codes.get(self.latest)
            if progress["state"] != "saved":
                if expiry is None or expiry <= time.time():
                    progress["state"] = "expired"
                else:
                    progress["expires_in_seconds"] = int(expiry - time.time())
            return progress

    def valid(self, code):
        with self.lock:
            return bool(code) and self.codes.get(code, 0) > time.time()

    def consume(self, code):
        with self.lock:
            self.codes.pop(code, None)


def local_host(host_header):
    """True when the request was addressed to this machine by a loopback name."""
    host = (host_header or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in LOCAL_HOSTS


def check_key(key):
    """Ask OpenRouter about the key. Returns its info, or raises ValueError."""
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response).get("data") or {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError("OpenRouter does not recognise that key.") from exc
        raise ValueError(f"OpenRouter answered {exc.code}; try again in a moment.") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach OpenRouter to check the key: {exc.reason}") from exc


def save_key(key):
    tmp = agent.KEY_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    os.replace(tmp, agent.KEY_PATH)


def describe(info):
    """One line about a checked key, without the key."""
    if info.get("limit") is None:
        return "This key has no credit limit set."
    return f"Credit left on this key: ${info.get('limit_remaining') or 0:,.2f} of ${info['limit']:,.2f}."


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stealthbox setup</title>
<style>
  :root {{ --bg: #f6f5f2; --card: #fff; --ink: #1d1d1b; --muted: #6b6a66; --line: #dddbd5;
          --accent: #2f5d50; --good: #2f6b3a; --fair: #9a6b12; --bad: #9a2f2f; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #171716; --card: #222220; --ink: #ecebe6; --muted: #a3a29c; --line: #3a3936;
            --accent: #8cc2ae; --good: #8fcf98; --fair: #d6a84a; --bad: #ec8f8f; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
         font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  main {{ max-width: 34rem; margin: 10vh auto; padding: 0 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 28px; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 6px; }}
  p {{ margin: 0 0 14px; color: var(--muted); }}
  label {{ display: block; font-weight: 600; margin: 18px 0 6px; }}
  input {{ width: 100%; padding: 10px 12px; font: inherit; color: var(--ink); background: var(--bg);
          border: 1px solid var(--line); border-radius: 6px; }}
  button {{ margin-top: 16px; padding: 10px 18px; font: inherit; font-weight: 600; color: var(--card);
           background: var(--accent); border: 0; border-radius: 6px; cursor: pointer; }}
  button:disabled {{ opacity: .45; cursor: default; }}
  h2 {{ font-size: 1.05rem; margin: 26px 0 4px; padding-top: 20px; border-top: 1px solid var(--line); }}
  .hint {{ font-size: 13px; margin: 6px 0 0; }}
  .hint.before {{ margin: -2px 0 8px; }}
  .field {{ position: relative; }}
  .field input {{ padding-right: 68px; }}
  .field .show {{ position: absolute; right: 6px; top: 50%; transform: translateY(-50%); margin: 0; padding: 4px 10px;
                 font-size: 14px; color: var(--accent); background: none; border-radius: 4px; }}
  .field .show:hover {{ background: color-mix(in srgb, var(--accent) 12%, transparent); }}
  .field .tick {{ position: absolute; right: 14px; top: 50%; transform: translateY(-50%); color: var(--good);
                 font-weight: 700; visibility: hidden; }}
  .field .tick.on {{ visibility: visible; }}
  .strength {{ display: flex; align-items: center; gap: 10px; margin-top: 8px; min-height: 20px; }}
  .segments {{ flex: 1; display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }}
  .segments i {{ height: 4px; border-radius: 2px; background: var(--line); transition: background .15s; }}
  .verdict {{ font-size: 13px; font-weight: 600; min-width: 6.5em; text-align: right; }}
  .advice {{ margin-top: 2px; }}
  .good {{ color: var(--good); }} .bad {{ color: var(--bad); }}
  a {{ color: var(--accent); }}
</style></head>
<body><main><div class="card">{body}</div></main></body></html>"""

FORM = """<h1>Set up Stealthbox</h1>
<p>Two things, both kept on this machine. Your agent never sees either one.</p>
{message}
<form method="post" action="/setup">
  <input type="hidden" name="code" value="{code}">
  {in_browser}
  <input type="text" name="username" value="stealthbox" autocomplete="username" hidden>
  <label for="key">OpenRouter key</label>
  <input id="key" name="key" type="password" autocomplete="off" spellcheck="false"
         placeholder="sk-or-v1-..." required autofocus>
  <p class="hint">Stealthbox drives the browser with DeepSeek through OpenRouter. It keeps the key in
  <code>~/.stealthbox/data/openrouter.key</code>. Create one at
  <a href="https://openrouter.ai/keys" target="_blank" rel="noopener">openrouter.ai/keys</a>.</p>
  <h2>Console password</h2>
  <p>You type this when you open a share link, for example on your phone, to watch or take over the
  browser. Anyone with the link <em>and</em> this password can use your signed-in browser.</p>
  {password_fields}
  <button type="submit">Check and save</button>
</form>"""

PASSWORD_FORM = """<h1>{title}</h1>
<p>You type this when you open a share link to the Stealthbox console, for example on your phone.
{effect}</p>
{message}
<form method="post" action="/password">
  <input type="hidden" name="code" value="{code}">
  <input type="hidden" name="session" value="{session}">
  <input type="text" name="username" value="stealthbox" autocomplete="username" hidden>
  {password_fields}
  <button type="submit">Save password</button>
</form>"""

SAVED = """<h1 class="good">Saved</h1>
<p>OpenRouter accepted the key. {detail}</p>
<p>{password}</p>
<p>You can close this tab and go back to your agent.</p>"""

EXPIRED = """<h1>This link has expired</h1>
<p>Setup links work once and last 15 minutes. Ask your agent for a new one
(<code>{command}</code>).</p>"""


def form_page(code, password_set, in_browser, error=None):
    """`in_browser`: the page is in Stealthbox's own browser, so saving stays on it
    instead of going on to the console."""
    message = f'<p class="bad">{html.escape(error)}</p>' if error else ""
    fields = console_auth.password_fields(
        "New password" if password_set else "Password",
        "Leave both empty to keep the password you have." if password_set
        else f"At least {console_auth.MIN_LENGTH} characters. Your password manager can save it.",
        required=not password_set)
    return PAGE.format(body=FORM.format(
        message=message, code=html.escape(code), password_fields=fields,
        in_browser='<input type="hidden" name="in_browser" value="1">' if in_browser else ""))


def saved_page(info, password_changed):
    password = ("The console password is set. Every device that was signed in is signed out." if password_changed
                else "The console password is as it was.")
    return PAGE.format(body=SAVED.format(detail=html.escape(describe(info)), password=password))


def password_page(code, session, password_set, error=None):
    message = f'<p class="bad">{html.escape(error)}</p>' if error else ""
    fields = console_auth.password_fields(
        "New password" if password_set else "Password",
        f"At least {console_auth.MIN_LENGTH} characters. Your password manager can save it.", required=True)
    return PAGE.format(body=PASSWORD_FORM.format(
        title="Choose a new console password" if password_set else "Choose a console password",
        effect="Saving it signs out every device that is signed in now, and signs in this one." if password_set
        else "Saving it signs in this browser for 30 days.",
        message=message, code=html.escape(code), session=html.escape(session), password_fields=fields))



def expired_page(command):
    return PAGE.format(body=EXPIRED.format(command=html.escape(command)))
