---
name: stealthbox
description: Drive a stealth Chromium on the user's Mac (OrbStack) or Linux machine (Docker Engine), over CDP, with a persistent profile that keeps cookies and logins. Use when a site bot-walls your cloud browser, when a session needs to survive between runs, or when the user says "use stealthbox", "do X with stealthbox", or "share the viewer". Talk to Stealthbox with `stealthbox chat` and it drives the browser itself with DeepSeek, working bot checks as they come up, and replies. Enter sites through a Bing organic result and humanize Playwright input; do not open the target with page.goto. If a site asks for a two-factor code, you (Grok Bot or Muse) get it and type it in; Stealthbox never reads Messages. When the user wants the viewer on a phone or away from home, run `stealthbox share` and send the HTTPS URL immediately. Do not ask them to confirm. Do not use Tailscale.
---

# Stealthbox skill

Stealthbox is a local CLI: `stealthbox`, or `./stealthbox` from this repo. It is plain bash and needs only Docker; `chat`, `run`, `notes`, `key` and `smoke` run inside the container, so from any shell (PowerShell too) they also work as `docker exec stealthbox stealthbox <command>`.
It controls a stealth Chromium (Chromium with [Fury](https://github.com/furyteamtop/fury-antidetect-browser)'s patches, from [`browser/`](../../browser), launched by [`serve/cdp-server`](../../serve/cdp-server)) in OrbStack on a Mac, Docker Engine on Linux, or Docker Desktop on Windows. The browser, the viewer, and the saved profile stay on the machine.

```text
Your agent ──► stealthbox ───────────────► start / stop / status / share
     │
     └───────► CDP  http://127.0.0.1:9222 ─► Chromium (Fury), headed (OrbStack on Mac, Docker Engine on Linux)
                                              ├─ saved profile: cookies and logins persist
                                              └─ viewer: http://127.0.0.1:6080
     stealthbox share ──► private HTTPS link to the console (sessions + live browser, any device)
```

The browser keeps running in the background (as long as OrbStack or Docker Engine runs) until `stealthbox stop`. Stopping keeps the profile.

## Playbook

1. **Check readiness**

   ```bash
   stealthbox status --json
   ```

   | Field | Value | What you do |
   | --- | --- | --- |
   | `docker_running` | `false` | Run `stealthbox start`. On a Mac with OrbStack that opens OrbStack; otherwise it uses the running Docker engine (Docker Desktop, Colima). If there's no engine at all, re-run the installer: it says exactly what to install. On Linux, Docker Engine must already be installed and `docker info` must succeed. |
   | `ready` | `false` | Run `stealthbox start` (first run downloads ~1 GB; allow up to 10 minutes). |
   | `openrouter_key` | `false` | Needed only for `stealthbox chat`. Run `stealthbox key`: it opens the setup page on this computer, where the user pastes the key and chooses the console password, and waits until they are saved (it says if OpenRouter rejected the key or the link expired). Away from the computer: `stealthbox share` (with no password yet, its link sets one first), then `stealthbox key --in-browser`. See [The OpenRouter key](#the-openrouter-key). Never ask for the key in chat. |
   | `console_password` | `false` | The console (`stealthbox share`) needs it. `stealthbox key` sets it with the key; with the key already set, run `stealthbox password`. See [The console password](#the-console-password). Never ask for it in chat. |

2. **Drive the browser over CDP**

   Connect, then enter the site with Bing (step 3). [`examples/hello.py`](../../examples/hello.py) runs that path for `example.com`. The steps live in [`examples/bing_first.py`](../../examples/bing_first.py); mouse and typing live in [`examples/humanize.py`](../../examples/humanize.py).

   ```python
   from patchright.sync_api import sync_playwright
   import bing_first  # examples/bing_first.py

   with sync_playwright() as pw:
       browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
       page = browser.contexts[0].new_page()
       # Bing gateway, then the organic result. Not page.goto of the target.
       landed = bing_first.open_via_bing(page, "example.com")
       print(landed.title())
   ```

   Use [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright), which has Playwright's API, not stock Playwright or Puppeteer. Those enable the `Runtime` domain on every tab they attach to; some bot-check vendors detect it, and a page that has to issue a new bot-check token stays blank. Same entry rule either way: Bing first, then humanized input on the site.

   If Patchright isn't installed, `pip install patchright` is enough. Skip `patchright install`: you attach to the running browser, so no browser download is needed.

   `browser.contexts[0]` is the persistent profile: cookies and logins from earlier runs are already there. Don't create a new context unless you want a clean session. `browser.close()` only disconnects you; the browser keeps running.

3. **Entering a website**

   Default for every new site:

   1. `page.goto("https://www.bing.com")` — this is the gateway, not the destination.
   2. Search the site name or domain. Click the search button.
   3. Click the **organic** result for that site. Prefer the official domain. Skip ads (`li.b_algo` is Bing's organic block; skip `.b_ad`).
   4. Stay on that site. Use its own search box and links.

   Do **not** open the target with `page.goto` as the first navigation. Do **not** jump straight to a search, listing, or product URL (`/trade/search`, deep product SERPs, and the same kind of hot scrape URL on other sites). Those direct loads are what get challenged.

   Google-first search from this CDP session often lands on Google `/sorry/` (an unusual-traffic check). **Bing is the search gateway** for Stealthbox. Do not start at Google.

   The one direct-URL exception: the user handed you one exact page to open (a login URL they named, a doc they linked). You may open that URL. Still humanize everything after it loads. If that URL is challenged, work the challenge (`stealthbox chat`) rather than retreating; fall back to Bing and the site's own UI only if the challenge won't clear.

4. **Humanize interactions**

   Drive the page like a person whenever you are on Bing or on the destination site. [`examples/humanize.py`](../../examples/humanize.py) is the default implementation (`pause`, `human_move`, `human_click`, `click_at`, `human_type`, `press`, `scroll`, `hold`, `drag`). Import it in ad-hoc scripts rather than calling `page.mouse` / `page.keyboard`: it delivers real X11 mouse and keyboard events through cdp-server's `POST /input` endpoint instead of CDP's synthetic input.

   - Short random pauses between actions (a few tenths of a second up to a couple of seconds).
   - Before each click, `mouse.move` along a short curved path with a little jitter, then click. A single instant jump to the target reads as a script.
   - Click the input with the mouse before typing. Do not `fill()` on a bot-sensitive site.
   - Type character by character with jittered delays, including a slightly longer pause after some spaces.
   - Prefer clicking real buttons and links over `page.goto` once you are on the site. In-site search goes through the site's search box, then its Search button:

   ```python
   import humanize  # examples/humanize.py

   box = page.locator("input[type='search']").first
   humanize.human_click(page, box)
   humanize.human_type(page, "your query")
   humanize.human_click(page, page.get_by_role("button", name="Search"))
   ```

   This is how you avoid needless challenges. If a challenge does appear, `stealthbox chat` works it (see [`stealthbox chat`](#stealthbox-chat-stealthbox-drives-you-talk)), or the user finishes it in the viewer.

5. **Bring in the user when needed**

   Whenever the user has to act in the page (sign in, pay, clear a check that won't clear), run `stealthbox share --json` and send them the `url` in the same turn, with what they need to do. Don't assume they are at the machine running Stealthbox; the share link works from anywhere. Send the share link, not http://127.0.0.1:6080. Wait for them to say they're done, then continue. Never ask for a password in chat.

   `stealthbox chat` does this itself: a run that needs the user ends `needs_user` with the share link already started (see [Handing off to the user](#handing-off-to-the-user)). Send the link it returns.

6. **Two-factor codes**

   You fetch codes. Stealthbox does not. It never reads Messages, `chat.db`, or Full Disk Access. When a site asks for a code, read it yourself (for example from Messages) and type it into the page. If you can't read it, ask the user to type it in the viewer. Do not ask them to paste it into chat, and never repeat the code.

## The console (phone, remote, or just watching)

User wants to see what Stealthbox is doing, the browser on a phone, away from home, or says "remote VNC" / "share the browser".

Run this immediately. Do not ask the user to approve, confirm, or opt in. There is no confirmation step in the CLI. Send `url` in the same turn.

```bash
stealthbox share --json
```

That one command starts the browser if it is not already running, then starts the `share` service, a Cloudflare quick tunnel to the console in a container of its own, and prints the URL. It does not prompt, and the host needs nothing installed for it: `cloudflared` is in the image. Because the tunnel has its own container, restarting or updating Stealthbox keeps its address; it restarts with Docker until `stealthbox unshare`.

```json
{"event":"share_ready","url":"https://….trycloudflare.com/console","local_url":"http://127.0.0.1:8423/console","console_password":true}
```

The console is one page, built for a phone as much as a desktop: sessions on one side, grouped
**Needs you**, **In progress** and **Earlier**; the chosen session on the other, with its live
browser beside its conversation. From it the user can start a session, message one (a message
to a busy session queues behind its run), **Take over** a working run (it holds after its
current step and the live view takes their clicks), **Hand back**, **Stop**, press **Done,
carry on** on one that needs them, **Show it live** for a tab that is in the background, and
**Close** a session. It updates live from the event stream. While a session works, its message
shows what the current step is doing (looking at the page, thinking, or the action it chose),
the model's reason, and how long it has taken; each finished step keeps its reason and the
model's reasoning text. The **Browser** entry, with a live preview, shows the tab in front
whoever drives it: the user can click and type in it, type an address in its address bar, go
full screen, and message Stealthbox about that tab (it continues the tab's session, or starts
one in the tab). The sidebar icon hides the sessions. **Password & devices** changes the console password (it asks for
the current one, and signs out every other device), lists the browsers signed in, and signs
one or all of them out. Saving the setup page or a password link opens the console, signed in.

The console asks for the console password. Signing in keeps that browser signed in for 30
days, but only on this link. The link keeps its address through restarts and updates of
Stealthbox; only a new share (after `stealthbox unshare`, or a reboot of the computer) has a
new address, and the cookie does not go there. So leave the share running.
`stealthbox unshare` stops the link and signs out the devices that used it; run it when the user
wants sharing off. `stealthbox status --json` includes `share_url` while the tunnel is running
(`null` when it is not).

With no console password yet (`"console_password": false`), `url` is a one-time link that sets
one first, then opens the console. Anyone who opens it before the user can take the console, so
send it only to them.

### The console password

The user chooses it on the `stealthbox key` page, beside the OpenRouter key. **Never ask for it in
chat**, and never read `~/.stealthbox/data/console-password` (it holds only a scrypt hash anyway).
If the user forgot it, or it was never set, give them a one-time link to set a new one:

| The user is | You run | They do |
| --- | --- | --- |
| At this machine | `stealthbox password` (opens the page here, waits until it is saved) | Type the new password twice. |
| Away | `stealthbox password --share --json`, then send them `password_url` | Open it on their phone and type the new password twice. |

The page shows how strong the password is and whether the two match, and refuses one that is
weaker than "Fair". Saving it signs out every device and signs in the one they used. Each link
works once and lasts 15 minutes; a `--share` link works from anywhere, so send it only to the
user. Five wrong passwords in a row lock sign-in for 15 minutes; a new password lifts that.

Do **not** use Tailscale. Do **not** Funnel or port-forward 6080 or 9222 yourself. The only remote path is `stealthbox share`, and it tunnels the console only (the API on 8423, which proxies the viewer itself). Never point a tunnel at port 9222. Do not add an approval gate of your own.

## Commands

Every command takes `--json`.

| Command | What it does |
| --- | --- |
| `stealthbox start` / `stop` | Start or stop the browser. The profile is kept. |
| `stealthbox status --json` | Is the container engine up, is the browser ready, and the viewer, CDP, and share URLs. Exits non-zero in text mode when not ready. |
| `stealthbox chat "<message>"` | Talk to Stealthbox; it drives the browser with DeepSeek and replies. See below. |
| `stealthbox run <id>` / `cancel <id>` | A run's steps, reply and files; or stop it. |
| `stealthbox notes [site]` | Stealthbox's guidance (the general rules, plus any site guidance installed in the data folder) and what it has learned, per site. |
| `stealthbox share --json` | Start an HTTPS link to the console, behind the console password, and print it. |
| `stealthbox unshare` | Stop that link. It stops working immediately, and the devices that used it are signed out. |
| `stealthbox password` | A one-time link to set or reset the console password. `--share` makes it work from anywhere. |

`start` / `stop` run [`start.sh`](../../start.sh) / [`stop.sh`](../../stop.sh), which also work on their own. `share` / `unshare` call the chat API (`POST /v1/share`, `POST /v1/unshare`).

## `stealthbox chat` (Stealthbox drives, you talk)

Instead of scripting Playwright yourself, talk to Stealthbox. It works the browser with
DeepSeek and replies when it is finished or needs you:

```bash
stealthbox chat "go to example-shop.com and search for paper towels"
stealthbox chat --session s_... "now sort by price and tell me the cheapest"
stealthbox chat --json "find the order status page on example-shop.com"
stealthbox run r_...            # every step, the reply, saved files
stealthbox notes example.com    # what it has learned about a site
```

The chat API runs inside the container ([`agent/server.py`](../../agent/server.py)), so
CDP never leaves loopback and the host needs nothing but Docker. Each
message is one run: screenshot, ask DeepSeek (via OpenRouter) for one action, execute it
humanized, repeat until the model replies. However many steps that takes, as long as the page keeps changing: it
gives up only after `--max-stuck-steps` steps in a row (default 12) change nothing on the screen (a hard cap of 400
steps guards against a page whose animation never stops).

| Reply status | Meaning | What you do |
| --- | --- | --- |
| `done` | Finished. The reply has what you asked for. | Use it. |
| `needs_input` | It needs something you can answer: a two-factor code, a choice. | Answer with `--session`. Codes: you fetch them, as always. |
| `needs_user` | The user has to act in the page: sign in, pay, or clear a bot check it could not. | Send them `needs_user.task` and `needs_user.share_url` now. When they're done, continue with `--session`. See [Handing off to the user](#handing-off-to-the-user). |
| `failed` / `step_limit` | It did not get there. | Read `stealthbox run <id>`; rephrase, or raise `--max-stuck-steps`. |

A **session** is one tab and its conversation. Without `--session` you get a new tab. Pass
the session id from the last reply to follow up in the same tab. Sessions expire after 30
idle minutes. One browser has one mouse, so runs queue.

**What it can do beyond clicking:** `read` (the page's text and links, for prices and listing
URLs), `back`, `goto` (only on a site it has already entered; other sites go through
Bing-first `open`), and `save_images` (the photo gallery of the item on the page, downloaded
through the browser session, one file per photo at its largest size). Saved files are in
`~/.stealthbox/data/runs/<run>/files/`, and the reply lists them.

**Sharing the browser.** Other CDP clients can use the same browser while a run is going.
If one shrinks the window or closes Stealthbox's tab, the run maximizes the window or
moves to another tab on the same site, and lists that under `repairs` in `stealthbox run <id>
--json`. Avoid driving the session's tab yourself while a run is working it.

**It starts with guidance.** [`agent/guidance/general.md`](../../agent/guidance/general.md) ships with
Stealthbox: general rules for each kind of bot check, in every prompt. Edit it to change what
every install starts with. Site guidance does not ship with Stealthbox; when installed
separately, it lives in `~/.stealthbox/data/guidance/sites/<site>.md`, one note per site (what
the check looks like, what beat it, whether it re-arms, and which ones are not worth spending
steps on). A site's note is added when the browser is on that site or a subdomain of it.

**It keeps notes for itself.** During a run it can `remember` facts it will need later in
that message (old steps scroll out of its prompt). When it learns something about a site
(a bot check and what cleared it, where a feature lives), it writes a `note` to that site's
notebook. Every run is logged per site, and a run that hit trouble ends with one extra call
that turns the step log into lessons. The notebook for the site on screen goes into every
prompt, so the next run starts knowing. All of it is plain files under
`~/.stealthbox/data/notes/sites/` (`<site>.md` for lessons, `<site>.jsonl` for the run log).

**The model** is `deepseek/deepseek-v4.1-flash` (set `STEALTHBOX_MODEL` in `.env` to change
it). Of OpenRouter's DeepSeek models only the Flash line accepts images; V3.x, V4 and V4 Pro
reject image input. It needs an OpenRouter key, set up as below; until then `stealthbox chat`
answers 412 and says so.

### The OpenRouter key

The user gives the key to Stealthbox, not to you. **Never ask for it in chat**, never read
`~/.stealthbox/data/openrouter.key`, and never echo it if the user pastes it anyway (tell them
to revoke it at https://openrouter.ai/keys and use the page instead).

| The user is | You run | They do |
| --- | --- | --- |
| At this machine | `stealthbox key --json`, then send them `setup_url` | Open it here and paste the key. |
| Away (phone, another computer) | `stealthbox share --json` and send the share URL, then `stealthbox key --in-browser` | Sign in to the console (or, with no password yet, set one through the link), then fill in the page that is open in Stealthbox's browser. |

The same page asks for the console password (see [The console password](#the-console-password)):
required the first time, and left as it is when both password fields are empty. The page checks
the key with OpenRouter, saves it to `~/.stealthbox/data/openrouter.key` (mode 600), and shows
the credit left if the key has a limit. Each link works once and lasts 15 minutes, and answers
only on `localhost`. Nothing restarts: the next `stealthbox chat` uses the key. `stealthbox
status --json` shows `"openrouter_key": true` once it is set. To replace a key, run `stealthbox
key` again.

**The API itself**, if you would rather call it than the CLI: `POST
http://127.0.0.1:8423/v1/chat` with `{"message": "...", "session": null}` and
`Authorization: Bearer $(cat ~/.stealthbox/data/api-token)`. The other routes are listed at
the top of [`agent/server.py`](../../agent/server.py).

If your own script is stuck on a bot check in a tab it drives over CDP, send that tab's
target id as `"tab"` (instead of `"session"`). Stealthbox works the check in your tab, so
there is no second tab and no second trip through Bing, and it leaves the tab open when the
session ends.

### Handing off to the user

Stealthbox never types a password, payment detail or personal detail that the conversation
didn't give it. When the page needs the user (a sign-in, a payment, or a bot check it can't
clear), the model replies with status `needs_user` and says what they need to do. A `type`
aimed at a password field becomes that reply even if the model didn't choose it. The server
then starts the share (or reuses the one already running), and the run carries the link:

```json
{"run": "r_...", "session": "s_...", "status": "needs_user",
 "reply": "Sign in to example-shop.com in the viewer, then tell me to carry on.",
 "needs_user": {"task": "Sign in to example-shop.com in the viewer, then tell me to carry on.",
                "share_url": "https://….trycloudflare.com"}}
```

`share_url` opens the console on that session, after the console password (a device signed in
on this link in the last 30 days goes straight there). With no password yet, it is a one-time
link that sets one first; send that only to the user. `needs_user` is `null` on every other run,
and `run.finished` carries it too. Send the user `task` and `share_url` in the same turn. The
session's tab stays where it was. The user signs in to the site in the console's live browser
and presses **Done, carry on**, which sends "I’m done, carry on" to the session, so Stealthbox
picks up in the same tab, now signed in, with the login saved in the profile for later runs.
If they tell you instead, send that message yourself with `--session`. Leave the share running
afterwards, so their phone stays signed in for the next hand-off.

**Following progress without polling:** `GET /v1/events` is a Server-Sent Events stream of
every state change, in order: `run.queued`, `run.started`, `run.step` (the step, its
screenshot URL, and any files it saved), `run.finished` (status, reply, files, cost),
`run.cancel_requested`, `session.created`, `session.expired`, `session.tab_lost`,
`run.phase` (looking, thinking or acting, inside a step), `run.paused`, `run.resumed`,
`session.focused`, `session.closed`, `browser.connected`,
`share.started`, `share.stopped`, `smoke.*`, `key.*`, `password.*`, `console.signed_in` and
`console.sign_in_refused`. `?run=<id>` or `?session=<id>` narrows it. To
resume after a dropped connection, send the last `id` back as `Last-Event-ID` (or
`?since=<id>`); if events were missed (the server restarted, or the reader fell more than
5000 events behind) the stream says so with a `stream.gap` event, and you re-read the run
with `GET /v1/runs/<id>`.

```bash
curl -N -H "Authorization: Bearer $(cat ~/.stealthbox/data/api-token)" "http://127.0.0.1:8423/v1/events?run=<run>"
```

**How it targets things.** Screenshots are captured at CSS scale, so one screenshot pixel is
one CSS pixel, and the model is told the exact image size. Stealthbox also reads the visible
interactive elements out of the DOM and hands the model their centre coordinates as hints.
That is what makes clicks land: on a retail site's press-and-hold check the model picks the
button centre to the pixel.

**Bot checks.** A run reports bot checks in its steps (`blocked`, `block_type`) and works
them, picking the action by kind. It detects them from the page's wording, challenge URLs,
generic captcha markers, and the model's own view of the screen. Vendor widget signatures are
not built in; an optional `~/.stealthbox/data/guidance/bot-checks.json` adds them.

| `block_type` | Action | Notes |
| --- | --- | --- |
| `press_and_hold` | `hold` | Press-and-hold checks. 8s or more, at the button centre. |
| `slider` | `drag` | Slider checks. Eased path with jitter, not a teleport — these score the path, not just the endpoints. |
| `checkbox` | `click` | Checkbox challenges. Often just a click. |
| `image_captcha` | `click` per tile | Best-effort: the model reads the prompt and clicks matching tiles one step at a time. Harder than the rest, and some of these run in cross-origin frames. |

Two things to know about the hold:

- **The challenge has to be fresh.** A press-and-hold check that has been sitting on
  screen goes inert: holding it produces no progress and never clears. Hold a freshly served
  challenge, not one that has been open for a minute.
- **Target the button precisely.** A hold a few pixels off the button fails silently, with no
  error and no progress ring. The DOM hints exist for this reason.

One retail site serves this challenge as a page of its own after repeated automated entry, and
also as an inline overlay on a normal-looking search page. Passing it once sets a clearance
cookie, and the site stops challenging for a while.

## Rules

- Never print, log, or store the user's password or two-factor codes in chat or files.
- Never ask for the OpenRouter key or the console password in chat. `stealthbox key` and `stealthbox password` give the user a page for them.
- Stealthbox does not read iMessage. You do, if a code is needed.
- Ports 9222, 6080 and 8423 stay on `127.0.0.1`.
- Whenever the user has to act in the page, send a `stealthbox share` link, never the localhost viewer address. Don't assume they're at the machine running Stealthbox.
- Remote access is one command: `stealthbox share`, which links the console. Run it and send the URL immediately. Do not ask for approval first, and do not add a confirmation step. The console asks for the console password; leave the share running so signed-in devices stay signed in, and run `stealthbox unshare` when the user wants it off. Do not use Tailscale, Funnel, or port forwards. Never tunnel 9222.
- Enter sites through Bing: search the name or domain, click the organic result, then use the site's own UI. Do not `page.goto` the target, and do not open search, listing, or product URLs as the first navigation. Google-first often hits `/sorry/` from Stealthbox CDP; Bing is the gateway.
- Humanize Playwright input by default: random pauses, a curved `mouse.move` before clicks, click a field before typing, type character by character. Do not `fill()` bot-sensitive forms. Helpers: [`examples/humanize.py`](../../examples/humanize.py), entry: [`examples/bing_first.py`](../../examples/bing_first.py).
- When a challenge appears, `stealthbox chat` works it (press-and-hold and the like). The user can also finish it in the viewer.

## Reference

### Install

`install.sh` (macOS, Linux) and `install.ps1` (Windows) install and update the same way. Run them again any time to update.

- The app is downloaded as a GitHub tarball (zip on Windows) into `~/.stealthbox/app` and replaced wholesale on every run, so local edits never block an update. `.env` carries over; the previous copy is kept in `~/.stealthbox/app.previous`. No git needed. Don't edit files in `~/.stealthbox/app`; put settings in `.env`.
- `install.sh` is POSIX `sh` (dash, BusyBox ash, bash, zsh) and refuses to run under `sudo`. It links `stealthbox` into `/usr/local/bin` when writable or `~/.local/bin` (added to `~/.profile`, `~/.zprofile`, `~/.zshrc`, `~/.bashrc`), then runs `stealthbox start`.
- **No container engine:** the installer changes nothing and exits 3 after a line `STEALTHBOX_NEEDS: docker` naming what it would install. Ask the user; with their yes, run it again with `STEALTHBOX_INSTALL_DOCKER=yes` (PowerShell: `$env:STEALTHBOX_INSTALL_DOCKER = "yes"`). Exit 4 after `STEALTHBOX_NEEDS: user` is a step only the user can do (a sudo password, Docker Desktop's first-run setup, logging in again); relay it and run the installer again afterwards.
- **Mac:** uses OrbStack if it's installed, else a Docker engine that's already running (Docker Desktop, Colima). With neither and consent, it installs OrbStack on macOS 14+ (Homebrew, or opens https://orbstack.dev/download and waits); on older macOS it explains how to install Colima or Docker Desktop. Stealthbox selects OrbStack per command (`DOCKER_CONTEXT`) and never changes your default Docker context.
- **Linux (amd64 or arm64):** uses Docker Engine with the Compose plugin. With consent it installs it with Docker's script (`get.docker.com`), starts it, adds the user to the `docker` group, and grants the socket for the current session (installing `acl` if needed) so no re-login is required.
- **Windows:** `irm https://raw.githubusercontent.com/jonclegg/stealthbox/main/install.ps1 | iex` in PowerShell. Needs Docker Desktop running Linux containers (WSL 2); with consent it installs it with `winget` and starts it. Works in Windows PowerShell 5.1 and PowerShell 7. <!-- pragma: allowlist secret -->
- **Image:** `stealthbox start` builds it on this computer from this folder (nothing is published), from [`image/Dockerfile`](../../image/Dockerfile) with the repository as context. The browser core is prebuilt ([`browser/cloud`](../../browser/cloud)) and downloaded from this repository's release. The first start downloads a GeoIP database into the `cache` volume.
- `STEALTHBOX_TARBALL_URL` (`STEALTHBOX_ZIP_URL` on Windows) installs from another archive, such as a fork or a tag.

### Settings

Optional knobs live in `.env` (created from [`.env.example`](../../.env.example) on first start): proxy, fingerprint seed, timezone, and ports. Edit `.env`, then `stealthbox start`. `stealthbox status` and `stealthbox share` read the ports from `.env` too. The browser's timezone and language come from GeoIP of the IP its traffic leaves from (the proxy's, when one is set). `TZ` in `.env` sets only the container's own clock.

### Limits

- Bot checks change and new ones appear. `stealthbox chat` works the kinds it implements — press-and-hold today, and checks like it; others land as they come up. When it can't clear one, the user finishes it in the viewer and the agent carries on from there.
- A loaded homepage is not a login or a checkout. The profile carries cookies and logins across runs, but a challenge can still appear later in a flow.
- Traffic leaves from this computer's connection or `STEALTHBOX_PROXY`. A home connection is the strongest; a flagged IP, VPN, or datacenter proxy draws more challenges.
- Grok Bot's **Route traffic through this computer** setting alone clears many IP blocks. Stealthbox is for sites that also fingerprint the browser (e.g. a retail site's press-and-hold page), or when the user wants a persistent local profile they can watch.
- Stealthbox has no site-specific scripts. Use it only with the user's own accounts and within each site's terms.

### Security

- Port 9222 is full control of a signed-in browser, and the viewer has no password. Neither is published beyond `127.0.0.1`.
- The profile lives in the `profile` Docker volume. `docker compose down -v` deletes it and signs the user out of everything.
- Stealthbox does not store passwords or codes. `.env` (proxy password) is git-ignored.
- The chat API (8423) drives the same signed-in browser, so it takes a bearer token: `~/.stealthbox/data/api-token`, created on first start, mode 600.
- The OpenRouter key is in `~/.stealthbox/data/openrouter.key`, mode 600, outside the install folder, so reinstalling and updating keep it. Only the setup page writes it. The page has no bearer token, so it requires a one-time code and a `localhost` Host header; another website open in the user's browser cannot replace the key.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| "OrbStack did not become ready" | User opens OrbStack from Applications, finishes first-run setup (may ask for the Mac password), then `stealthbox start`. |
| "permission denied: ./stealthbox" | `chmod +x stealthbox start.sh stop.sh install.sh`, or `bash stealthbox …`. |
| "port is already allocated" | Something else uses 9222 or 6080 (often a Chrome with remote debugging). Close it, or change the ports in `.env`. |
| "`docker info` failed" / permission denied | On Linux, start Docker (`sudo systemctl enable --now docker`) and add the user to the `docker` group (`sudo usermod -aG docker "$USER"`), then log in again. |
| Tabs crash ("Aw, Snap!") or slow | Quit heavy apps and `stealthbox start`. Memory settings are in the OrbStack app. |
| Need logs | `docker compose logs -f stealthbox` from the repo. |
| Direct URL shows a challenge, homepage would not | Do not reload the hot search or product URL. Enter through Bing, click the organic homepage, then search with the site's own box using humanized input. If a challenge is already up, `stealthbox chat` works it, or the user solves it in the viewer. |

### Linux

Browser and viewer run on Linux with Docker Engine (amd64 or arm64). The image is already headed on Xvfb, so a server does not need a monitor.

Prerequisites:

- [Docker Engine](https://docs.docker.com/engine/install/)
- The Compose plugin: `docker compose version` must succeed. On Debian or Ubuntu: `sudo apt-get install docker-compose-plugin`
- `docker info` must succeed for this user. If it says permission denied: `sudo usermod -aG docker "$USER"`, then log in again.

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/jonclegg/stealthbox/main/install.sh | sh
```

From a clone, once Docker is working:

```bash
./stealthbox start
```

CDP stays `http://127.0.0.1:9222`. The Bing-first playbook above is unchanged. Ports stay on `127.0.0.1`. `stealthbox share` tunnels the console (8423) only, never CDP.

The browser runs headed on Xvfb inside the image, so a server needs no monitor. WebGL and WebGPU come from SwiftShader (`--enable-unsafe-swiftshader`), and the persona reports the GPU of the machine it was captured on. A home machine uses that machine's IP; a datacenter IP draws more challenges.

### Windows

Browser and viewer run on Windows with Docker Desktop (WSL 2, Linux containers).

1. Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), restart, wait for **Engine running**.
2. In PowerShell: `irm https://raw.githubusercontent.com/jonclegg/stealthbox/main/install.ps1 | iex` <!-- pragma: allowlist secret -->
3. Later: `& "$HOME\.stealthbox\app\start.ps1"` to start, `& "$HOME\.stealthbox\app\stop.ps1"` to stop. (In a fresh session where scripts are blocked: `powershell -ExecutionPolicy Bypass -File "$HOME\.stealthbox\app\start.ps1"`.)
4. Commands: `docker exec stealthbox stealthbox status | key | smoke | chat "..."`. `key` prints a link to open on this machine; `smoke` writes its report to `~\.stealthbox\data\smoke\`.

For the console on a phone: `docker exec stealthbox stealthbox share`.

### Internals

- **`stealthbox` container:** the image `stealthbox:local`: [`cdp-server`](../../serve/cdp-server), the Fury core in `/opt/fury`, x11vnc, noVNC and the chat API. `cdp-server` launches the core through [`image/launch-browser`](../../image/launch-browser), which turns the seed, timezone and locale into a persona config: a real machine from Fury's catalogue whose screen is the container's display (a Mac's 2880x1800 on a Mac host). `stealthbox start` builds it from this folder ([`docker-compose.yml`](../../docker-compose.yml), [`image/Dockerfile`](../../image/Dockerfile)). Runs `cdp-server` headed on a virtual display (passes more bot checks; it's what the viewer shows). `shm_size: 2gb` (Chromium crashes at the 64 MB default). Healthcheck on `/json/version`.
- **Volumes:** `profile` (cookies and logins) and `cache` (the GeoIP database). [`image/stealthbox-serve.sh`](../../image/stealthbox-serve.sh) keeps `cdp-server` from deleting the profile on exit.
- **cdp-server:** the default identity's seed is saved in the profile (`.cdp-server-seed`), so the fingerprint survives restarts with the cookies. Timezone and locale come from GeoIP of the egress IP (`--geoip`). The window is sized so the geometry pages read stays on screen, GPU/CPU/memory come from one coherent profile per seed, `POST /input` gives real X11 input, and `GET /history?q=` reads the default identity's history for the console's address bar (Chrome's profile is root's alone).
- **Persona:** on a Mac, `start.sh` adds `COMPOSE_FILE=docker-compose.yml:docker-compose.mac.yml` to `.env`, so the browser presents as a Mac (Apple GPU, a 1440x900 Retina screen, the Mac's own fonts mounted read-only). On an Apple Silicon host that persona clears some bot checks that the Windows persona fails. Linux hosts keep the Windows persona. Delete that line from `.env` to go back to Windows.
- **Extra identities:** add `?fingerprint=<seed>` to the CDP URL, e.g. `http://127.0.0.1:9222?fingerprint=11111&timezone=Europe/Berlin`. Only the default identity is saved to `profile`. Idle extra identities close after 5 minutes, and at most 8 browsers run at once (the least recently used idle one closes to make room). Reconnecting to a running identity with different settings returns `409`; `POST /fingerprint/<seed>/close` first.
- **Updates:** run the installer again; `stealthbox start` rebuilds the image. `STEALTHBOX_IMAGE` in `.env` names another local image (a machine that cannot build, like one without buildx, runs an image built elsewhere and copied with `docker save | docker load`).
- **Smoke test:** `stealthbox smoke <site> [<site> ...]` enters each site it is given (with none, the domains in `~/.stealthbox/data/smoke-sites.txt`, one per line; without that file it exits with an error asking for sites) from Bing on the API's browser worker, works any bot check when the OpenRouter key is set, and writes `report.html`, the screenshots and `result.json` to `~/.stealthbox/data/smoke/<id>/`.
- **Console:** [`agent/console.html`](../../agent/console.html), served by the API at `/console`. It polls `GET /v1/events/next` rather than reading the SSE stream, because Cloudflare quick tunnels hold an SSE response until it ends. `/viewer/...` proxies noVNC (its files, and its websocket byte for byte), so one tunnel carries the console and the live browser. Every route but the sign-in, setup and password pages needs the bearer token or a device cookie ([`agent/console_auth.py`](../../agent/console_auth.py)), and a cookie is accepted only on the host it signed in through, from a page on that same origin.
- **Console tests:** `pytest tests/test_console_auth.py` checks the password rules (and that the page's meter scores like the server), the lockout, and device cookies, with no Stealthbox running. `STEALTHBOX_CONSOLE_PASSWORD=… pytest tests/test_console.py` drives a headless Chromium (the host's Playwright) through the share link against the running Stealthbox: sign-in and the device cookie, signing out another device, a new session to its reply (with the live view, the newest message and the reply box all on screen without scrolling), a queued follow-up, take over / hand back / stop, a sign-in handoff opened on a phone-sized browser, show it live, close, and unshare. Runs use the real model: a few cents and about ten minutes.
- **Plain Compose:** `cp .env.example .env && docker compose up -d`, then `docker compose down`.

### Links

- Fury, upstream of `browser/`: https://github.com/furyteamtop/fury-antidetect-browser
- Where everything else came from: [NOTICE](../../NOTICE)
- OrbStack: https://orbstack.dev/
- Docker Engine: https://docs.docker.com/engine/install/
