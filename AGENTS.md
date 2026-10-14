# Agent instructions

Stealthbox is a stealth browser (Chromium with [Fury](https://github.com/furyteamtop/fury-antidetect-browser)'s fingerprint patches, in [`browser/`](browser)) with a persistent profile, running in a container on the user's own computer. You give it goals with `stealthbox chat`, or drive it yourself over CDP. Use it when a site blocks your cloud browser, when cookies and logins need to persist between runs, or when the user asks you to do something "with stealthbox". [`skills/stealthbox/SKILL.md`](skills/stealthbox/SKILL.md) is the full reference.

## Installing Stealthbox for the user

When the user points you at this repo, install it for them. They should not have to run anything themselves. Go through these steps in order, and tell the user in one short line what you're doing at each one.

**1. Already installed?** Run `stealthbox status --json`. If it prints `"ready": true`, `"openrouter_key": true` and `"console_password": true`, skip to step 5. If only `console_password` is `false`, run `stealthbox password` (the user sets it on the page it opens), then skip to step 5.

**2. Run the installer.** It downloads Stealthbox, builds its image on this computer, and starts it. The build downloads the prebuilt browser core from this repository's private release with the GitHub CLI's login, so `gh` must be signed in (`gh auth status`).

macOS or Linux:

```bash
gh api repos/jonclegg/stealthbox/contents/install.sh -H 'Accept: application/vnd.github.raw' | sh
```

Windows (PowerShell):

```powershell
gh api repos/jonclegg/stealthbox/contents/install.ps1 -H 'Accept: application/vnd.github.raw' | iex
```

The first run takes several minutes. It builds the image (about 2 GB, browser included), and on first start the container downloads a GeoIP database.

**3. If it stops with `STEALTHBOX_NEEDS: docker`** (exit code 3), the computer has no container engine, and the installer changed nothing. **Ask the user** whether to install one, and say which one the output names: OrbStack on a Mac, Docker Engine on Linux, Docker Desktop on Windows. If they agree, run the installer again with consent:

```bash
gh api repos/jonclegg/stealthbox/contents/install.sh -H 'Accept: application/vnd.github.raw' | STEALTHBOX_INSTALL_DOCKER=yes sh
```

```powershell
$env:STEALTHBOX_INSTALL_DOCKER = "yes"; gh api repos/jonclegg/stealthbox/contents/install.ps1 -H 'Accept: application/vnd.github.raw' | iex
```

If it stops with `STEALTHBOX_NEEDS: user` (exit code 4), it needs something only the user can do. Examples: typing a sudo password in their own terminal, finishing Docker Desktop's first-run setup, or logging out and back in. Relay the message as it is, wait until they say it's done, then run the installer again.

**4. The OpenRouter key and the console password.** Stealthbox drives the browser with DeepSeek through OpenRouter, so it needs the user's key. The console password protects the share link (step 6). **Never ask for either in chat.** Run `stealthbox key`. It opens a local setup page in this computer's browser (it also prints the link) and then waits. Tell the user to paste their key into that page, choose a console password (the page shows its strength and whether the two copies match), and click the button. If they don't have a key, they create one at https://openrouter.ai/keys. The command returns when both are saved, which means OpenRouter accepted the key. It also reports what happens along the way:

- **The page opened but nothing was submitted.** Remind the user to click the button.
- **Not saved.** OpenRouter rejected the key, or the password was too weak or the copies didn't match. The page stays open for another try. Relay the reason.
- **The link expired** (after 15 minutes). Run `stealthbox key` again.

The page answers only on this computer (`127.0.0.1`). If the user isn't at this computer, or it has no screen, run `stealthbox share` and send them the link: with no password yet it is a one-time link that sets one first, so send it only to them. Then run `stealthbox key --in-browser`, and they paste the key into the page in Stealthbox's own browser, through the console. For a link without waiting, use `stealthbox key --json`. `stealthbox status --json` shows how far it has got under `key_setup`.

**5. Show it working.** Run `stealthbox smoke` with two or three sites the user uses: ask them which, or use the ones they mentioned (`stealthbox smoke example.com other.com`). Stealthbox enters each from Bing, works any bot check, takes a screenshot of each, and opens a report. Tell the user how many it reached, and show them the screenshots: `stealthbox smoke --json` lists each one's `host_screenshot` path.

**6. Done.** Tell them it's ready, how to watch the browser (the `viewer_url` from `stealthbox status --json`, normally http://127.0.0.1:6080), and that they can now ask you to do things on sites "with Stealthbox".

The ports in these instructions (9222 CDP, 6080 viewer, 8423 API) are the defaults. If something else on the computer already uses them, the user sets other ports in `~/.stealthbox/app/.env`. `stealthbox status --json` always reports the real URLs.

On Windows there is no `stealthbox` command on the host. Use `docker exec stealthbox stealthbox <command>` for `status`, `key`, `password`, `smoke`, `chat`, `run`, `notes`, `share` and `unshare`. Start and stop with `~\.stealthbox\app\start.ps1` and `stop.ps1`. For the key, run `docker exec stealthbox stealthbox key --json`, open the `setup_url` for the user with `Start-Process <url>`, then wait on that same link with `docker exec stealthbox stealthbox key --watch`.

## Using Stealthbox

```bash
stealthbox status --json          # is it ready, viewer and API URLs, key set?
stealthbox start                  # if not ready
stealthbox chat "<message>"       # Stealthbox drives the browser with DeepSeek and replies
stealthbox share --json           # console link (sessions + live browser); send the url immediately
stealthbox password               # one-time link to set or reset the console password (--share: from anywhere)
stealthbox unshare                # stop the share when the user wants it off
```

**Prefer talking to Stealthbox.** `stealthbox chat "search example-shop.com for paper towels"` sends a message to the chat API inside the container. Stealthbox screenshots the page, DeepSeek picks the next action, and Stealthbox carries it out as real mouse and keyboard input, press-and-hold included. When it's done it replies with a status: `done`, `needs_input`, `needs_user` or `failed`. Follow up with `--session <id>` to stay in the same tab. It can read pages and save a listing's photos, and it keeps per-site notes so the next run starts out knowing more. See [`skills/stealthbox/SKILL.md`](skills/stealthbox/SKILL.md#stealthbox-chat-stealthbox-drives-you-talk).

**Or drive it yourself** with [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (`pip install patchright`), which has Playwright's API: `connect_over_cdp("http://127.0.0.1:9222")`, then use `browser.contexts[0]`, which is the saved profile with its cookies. Don't use stock Playwright or Puppeteer: they enable the `Runtime` domain on every tab, some bot-check vendors detect it, and the page stays blank. Send mouse and keyboard input through [`examples/humanize.py`](examples/humanize.py), not `page.mouse` or `page.keyboard`. It delivers real X11 events through Stealthbox's `/input` endpoint.

**Enter sites from Bing, and humanize input.** Do not `page.goto` the target site, and do not open a search, listing or product URL as the first navigation. Open `https://www.bing.com`, search the site name, click the organic result (official domain, skip ads), then use that site's own search and links ([`examples/bing_first.py`](examples/bing_first.py)). Starting from Google often hits `/sorry/`. Pause between actions, click a field before typing, and type character by character. If a bot check still appears, `stealthbox chat` works it.

If a site asks for a two-factor code, **you** get it (for example from Messages) and type it into the page. Stealthbox does not read texts.

When the user wants to watch Stealthbox, see what its sessions are doing, or reach the browser from a phone or anywhere else, run `stealthbox share` and send the HTTPS URL in the same turn. Do not ask them to confirm first; the command does not prompt. The link opens the **Stealthbox console**: every session with its status and conversation, the live browser, what needs them, and buttons to message a session, take over the mouse, hand back, stop, or close it. The console asks for the console password, and keeps a device signed in for 30 days on that link. The share runs in a container of its own: it keeps its address when Stealthbox restarts or updates, so leave it running and the user's devices stay signed in. Only a new share (after `stealthbox unshare`, or a reboot) has a new address. In the console's **Browser** view the user can type in the live browser, type an address, and message Stealthbox about the tab in front. Run `stealthbox unshare` when the user wants sharing off; the link stops working and the devices that used it are signed out. If they forgot the password, run `stealthbox password --share --json` and send them `password_url`, a one-time link that sets a new one and signs out every device; send it only to them. Do **not** use Tailscale, Funnel, or port forwarding. Never expose port 9222. `stealthbox share` tunnels the console (the API on 8423, which serves the live viewer itself), never CDP, and ports 6080, 8423 and 9222 stay bound to `127.0.0.1`.

**When a run ends `needs_user`**, the page needs the user's own hands: a sign-in, a payment, or a bot check Stealthbox couldn't clear. Send the user `needs_user.task` and `needs_user.share_url` in the same turn; if `share_url` is `null`, nothing is being shared yet: run `stealthbox share --json` and send its `url`. The link opens the console on that session, after the console password (with no password yet, it sets one first; send that link only to the user): they act in the live browser there and press **Done, carry on**, which continues the session for you (watch `run.finished` on `GET /v1/events?session=<id>`, or the session's next reply). Don't assume they're at the machine running Stealthbox. If they tell you they're done instead, continue with `--session <id>` (same tab, now signed in). Leave the share running. Do the same yourself, with `stealthbox share --json`, whenever the user has to act in the page while you drive it over CDP.

Never ask the user for a password in chat, the console password included. They type site passwords in the console's live browser, through the share URL. Always send the share URL, not the localhost viewer address: the user is usually not at the machine running Stealthbox. If they're away and need to paste the OpenRouter key, run `stealthbox share` and `stealthbox key --in-browser`, and they paste it in the console's live browser.

On a Mac, the browser presents as a Mac: macOS persona, a Retina screen, and the Mac's own fonts. On Linux and Windows it presents as a Windows PC. Its timezone and language follow the IP its traffic leaves from.
