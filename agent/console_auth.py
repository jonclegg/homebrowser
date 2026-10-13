"""The console password, and the devices signed in with it.

The console (what `homebrowser share` links) asks for a password. The user sets it
on the setup page beside the OpenRouter key, or on a one-time password link an
agent asks for (`homebrowser password`) when they have none yet or forgot it. Only a
scrypt hash is kept, in <data>/console-password, mode 600.

Signing in gives that browser its own random cookie, good for 30 days on the host
it signed in through. The devices file keeps only a hash of each cookie. A new
password signs every device out; `unshare` signs out the ones that came through
that tunnel, whose address will not come back.

Wrong passwords count for the whole install, not per address: everything through
the tunnel arrives from cloudflared on localhost. After 5 in a row, sign-in stops
for 15 minutes; a new password lifts that.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime

MIN_LENGTH = 8
# Strength 0 is refused and 1 ("Weak") is shown but refused too.
MIN_STRENGTH = 2
# Refused anywhere in the password; the setup pages get the same list.
GUESSABLE = ("password", "homebrowser", "qwerty", "123456", "abcdef", "letmein", "iloveyou")
DEVICE_SECONDS = 30 * 24 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
           ("Windows", "Windows"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))
BROWSERS = (("Edg", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"),
            ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))


class SignInRefused(Exception):
    pass


def strength(password):
    """0 to 4, and the words the meter shows. FIELDS_SCRIPT runs the same rules in the page."""
    lowered = password.lower()
    if len(password) < MIN_LENGTH:
        return 0, f"Too short: at least {MIN_LENGTH} characters"
    if len(set(lowered)) < 5 or any(word in lowered for word in GUESSABLE):
        return 0, "Too easy to guess"
    pool = sum(size for pattern, size in ((r"[a-z]", 26), (r"[A-Z]", 26), (r"[0-9]", 10), (r"[^A-Za-z0-9]", 33))
               if re.search(pattern, password))
    bits = len(password) * math.log2(pool)
    if bits < 50:
        return 1, "Weak: make it longer, or mix in capitals, numbers and symbols"
    if bits < 65:
        return 2, "Fair"
    if bits < 85:
        return 3, "Strong"
    return 4, "Very strong"


def check_new(password, again):
    """Raise ValueError unless `password` can be the console password."""
    if password != again:
        raise ValueError("The two passwords don't match.")
    score, words = strength(password)
    if score < MIN_STRENGTH:
        raise ValueError(f"That password is too weak ({words.split(':')[0].lower()}).")


def write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Password:
    def __init__(self, path):
        self.path = path
        self.failures = 0
        self.locked_until = 0.0
        self.lock = threading.Lock()

    def is_set(self):
        return os.path.exists(self.path)

    def set(self, password):
        """Store a new password (already checked with check_new) and lift any lockout."""
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, **SCRYPT)
        write_private(self.path, json.dumps({"scrypt": SCRYPT, "salt": salt.hex(), "hash": digest.hex()}) + "\n")
        with self.lock:
            self.failures = 0
            self.locked_until = 0.0

    def check(self, password):
        """Return if `password` is right; raise SignInRefused with what to tell the user."""
        with self.lock:
            wait = self.locked_until - time.time()
            if wait > 0:
                raise SignInRefused(f"Too many wrong passwords. Try again in {math.ceil(wait / 60)} minutes, "
                                    "or ask your agent for a password reset link.")
            with open(self.path) as fh:
                stored = json.load(fh)
            digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(stored["salt"]),
                                    maxmem=64 * 1024 * 1024, **stored["scrypt"])
            if hmac.compare_digest(digest.hex(), stored["hash"]):
                self.failures = 0
                return
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                self.failures = 0
                self.locked_until = time.time() + LOCK_SECONDS
                raise SignInRefused(f"Wrong password. Sign-in is locked for {LOCK_SECONDS // 60} minutes.")
            left = MAX_FAILURES - self.failures
            raise SignInRefused(f"Wrong password. {left} {'try' if left == 1 else 'tries'} left, "
                                f"then sign-in locks for {LOCK_SECONDS // 60} minutes.")


def describe_device(user_agent):
    agent = user_agent or ""
    system = next((name for marker, name in SYSTEMS if marker in agent), "Unknown device")
    browser = next((name for marker, name in BROWSERS if marker in agent), "a browser")
    return f"{system} · {browser}"


def iso_time(seconds):
    return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    """Signed-in browsers, saved in <data>/console-devices.json so they outlive a restart."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.devices = []
        if os.path.exists(path):
            with open(path) as fh:
                self.devices = json.load(fh)

    def _save(self):
        now = time.time()
        self.devices = [device for device in self.devices if device["expires"] > now]
        write_private(self.path, json.dumps(self.devices, indent=2) + "\n")

    def sign_in(self, host, user_agent):
        """A new device on `host`; returns the cookie value, which is never stored."""
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.devices.append({"id": secrets.token_hex(6), "token_sha256": token_hash(token), "host": host,
                                 "label": describe_device(user_agent), "signed_in": time.time(),
                                 "expires": time.time() + DEVICE_SECONDS})
            self._save()
        return token

    def find(self, token, host):
        """The device this cookie belongs to, if it is current and came in through `host`."""
        if not token:
            return None
        digest = token_hash(token)
        with self.lock:
            return next((device for device in self.devices
                         if hmac.compare_digest(device["token_sha256"], digest)
                         and device["host"] == host and device["expires"] > time.time()), None)

    def view(self, current):
        with self.lock:
            return [{"device": device["id"], "label": device["label"], "host": device["host"],
                     "signed_in": iso_time(device["signed_in"]), "expires": iso_time(device["expires"]),
                     "current": current is not None and device["id"] == current["id"]}
                    for device in sorted(self.devices, key=lambda device: -device["signed_in"])
                    if device["expires"] > time.time()]

    def sign_out(self, device_id=None, host=None):
        """Sign out one device, every device on one host, or (with neither) all of them."""
        with self.lock:
            before = len(self.devices)
            self.devices = [device for device in self.devices
                            if (device_id is not None and device["id"] != device_id)
                            or (host is not None and device["host"] != host)]
            self._save()
            return before - len(self.devices)


FIELDS = """<label for="password">{label}</label>
<p class="hint before" id="password-hint">{hint}</p>
<div class="field">
  <input id="password" name="password" type="password" autocomplete="new-password"
         aria-describedby="password-hint password-verdict password-advice" {required}>
  <button class="show" type="button" aria-controls="password again" aria-pressed="false">Show</button>
</div>
<div class="strength"><div class="segments" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
  <span class="verdict" id="password-verdict" aria-live="polite"></span></div>
<p class="hint advice" id="password-advice"></p>
<label for="again">Confirm password</label>
<div class="field">
  <input id="again" name="again" type="password" autocomplete="new-password" aria-describedby="again-error" {required}>
  <span class="tick" aria-hidden="true">✓</span>
</div>
<p class="hint bad" id="again-error" aria-live="polite"></p>
<script>
(() => {{
  const MIN_LENGTH = {min_length}, MIN_STRENGTH = {min_strength}, GUESSABLE = {guessable};
  const COLORS = ['var(--bad)', 'var(--bad)', 'var(--fair)', 'var(--good)', 'var(--good)'];
  // The same rules as console_auth.strength().
  function strength(password) {{
    const chars = [...password], lowered = password.toLowerCase();
    if (chars.length < MIN_LENGTH) return [0, `Too short: at least ${{MIN_LENGTH}} characters`];
    if (new Set([...lowered]).size < 5 || GUESSABLE.some((word) => lowered.includes(word))) return [0, 'Too easy to guess'];
    const pool = [[/[a-z]/, 26], [/[A-Z]/, 26], [/[0-9]/, 10], [/[^A-Za-z0-9]/u, 33]]
      .reduce((sum, [pattern, size]) => sum + (pattern.test(password) ? size : 0), 0);
    const bits = chars.length * Math.log2(pool);
    if (bits < 50) return [1, 'Weak: make it longer, or mix in capitals, numbers and symbols'];
    if (bits < 65) return [2, 'Fair'];
    if (bits < 85) return [3, 'Strong'];
    return [4, 'Very strong'];
  }}
  const password = document.getElementById('password'), again = document.getElementById('again');
  const segments = document.querySelectorAll('.segments i'), verdict = document.getElementById('password-verdict');
  const advice = document.getElementById('password-advice'), tick = document.querySelector('.tick');
  const error = document.getElementById('again-error'), show = document.querySelector('.show');
  // "Doesn't match" waits until the second box has been left once.
  let left = false;
  function update() {{
    const empty = !password.value && !again.value && !password.required;
    const [score, text] = strength(password.value);
    const [words, tip = ''] = password.value ? text.split(': ') : ['', ''];
    const filled = password.value ? Math.max(score, 1) : 0;
    segments.forEach((segment, index) => segment.style.background = index < filled ? COLORS[score] : '');
    verdict.textContent = words;
    verdict.style.color = COLORS[score];
    advice.textContent = tip && tip[0].toUpperCase() + tip.slice(1);
    const same = password.value === again.value;
    tick.classList.toggle('on', !!again.value && same);
    const wrong = left && !!again.value && !same;
    error.textContent = wrong ? 'Doesn’t match the password above' : '';
    again.setAttribute('aria-invalid', wrong);
    // Looked up here: this script runs before the form's button is parsed.
    password.form.querySelector('button[type=submit]').disabled = !empty && (score < MIN_STRENGTH || !same);
  }}
  show.addEventListener('click', () => {{
    const hidden = password.type === 'password';
    password.type = again.type = hidden ? 'text' : 'password';
    show.textContent = hidden ? 'Hide' : 'Show';
    show.setAttribute('aria-pressed', hidden);
  }});
  password.addEventListener('input', update);
  again.addEventListener('input', update);
  again.addEventListener('blur', () => {{ left = true; update(); }});
  document.addEventListener('DOMContentLoaded', update);
}})();
</script>"""


def password_fields(label, hint, required):
    """The new-password inputs with the strength meter and the match check, for the setup pages."""
    return FIELDS.format(label=label, hint=html.escape(hint), required="required" if required else "",
                         min_length=MIN_LENGTH, min_strength=MIN_STRENGTH, guessable=json.dumps(GUESSABLE))


SIGN_IN = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Homebrowser</title>
<style>
  :root {{ color-scheme: dark; --ground: #0E1014; --panel: #161920; --raised: #1D212A; --line: #2A2F3A;
          --ink: #E8EAEF; --muted: #9AA1AD; --soft: #C3C8D1; --accent: #7AA7FF; --accent-strong: #3D6FF0; --bad: #FF8A80; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--ground); color: var(--ink);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px 16px; }}
  main {{ width: 100%; max-width: 22rem; }}
  .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 17px; margin-bottom: 22px; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  p {{ color: var(--muted); margin: 0 0 18px; }}
  label {{ display: block; font-weight: 600; font-size: 13px; margin: 0 0 6px; }}
  input {{ width: 100%; min-height: 44px; padding: 0 12px; border-radius: 8px; border: 1px solid var(--line);
          background: var(--panel); color: var(--ink); font: inherit; }}
  input[aria-invalid=true] {{ border-color: var(--bad); }}
  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  button {{ margin-top: 18px; width: 100%; min-height: 48px; border-radius: 8px; border: 0; background: var(--accent-strong);
           color: #fff; font: inherit; font-weight: 600; font-size: 15px; cursor: pointer; }}
  .error {{ color: var(--bad); font-size: 13px; margin: 8px 0 0; }}
  .foot {{ margin-top: 22px; font-size: 12px; color: #8A919E; }}
  code {{ background: var(--raised); padding: 1px 5px; border-radius: 4px; color: var(--soft); }}
</style></head>
<body><main>
  <div class="brand">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a2.5 2.5 0 0 1 2.5 2.5c0 1.4-1.2 2-2.5 3.2V10"/><path d="M12 10 3 17.5h18L12 10Z"/></svg>
    Homebrowser
  </div>
  {body}
</main></body></html>"""

SIGN_IN_FORM = """<h1>Sign in to the console</h1>
<p>The live browser and what Homebrowser is doing in it.</p>
<form method="post" action="/console/sign-in" data-testid="sign-in">
  <input type="text" name="username" value="homebrowser" autocomplete="username" hidden>
  <input type="hidden" name="session" id="session" value="{session}">
  <label for="password">Console password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required autofocus
         aria-invalid="{invalid}"{describedby}>
  {error}
  <button type="submit">Sign in</button>
</form>
<p class="foot">This device stays signed in for 30 days. Forgot the password? Ask your agent for a reset link
(<code>homebrowser password</code>). It signs out every device.</p>
<script>if (location.hash) document.getElementById('session').value = decodeURIComponent(location.hash.slice(1));</script>"""

NO_PASSWORD = """<h1>No console password yet</h1>
<p>The console opens only with a password, and none is set. On the computer running Homebrowser,
open <code>{local_console}</code> and choose one there. Or ask your agent for a password link.</p>"""


def sign_in_page(password_set, error=None, session="", local_console=""):
    """The sign-in form. `session` carries a session link's #session through a wrong try.
    With no password yet, where to set one: `local_console`, the console on this computer."""
    if not password_set:
        return SIGN_IN.format(body=NO_PASSWORD.format(local_console=html.escape(local_console)))
    message = f'<p class="error" id="error" role="alert">{html.escape(error)}</p>' if error else ""
    return SIGN_IN.format(body=SIGN_IN_FORM.format(
        invalid="true" if error else "false", describedby=' aria-describedby="error"' if error else "",
        error=message, session=html.escape(session)))
