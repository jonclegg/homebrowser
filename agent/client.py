"""`homebrowser chat`, `run`, `cancel`, `notes`, `key`, `password`, `smoke`, `share`, `status`: a client for the chat API.

Standard library only. It runs inside the container (`docker exec homebrowser
homebrowser <command>`), so the host needs nothing but Docker.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser

API_URL = os.environ.get("HOMEBROWSER_API_URL") or f"http://127.0.0.1:{os.environ.get('HOMEBROWSER_API_PORT') or 8423}"
DATA_DIR = os.environ.get("HOMEBROWSER_DATA_DIR") or os.path.expanduser("~/.homebrowser/data")


def token():
    path = os.path.join(DATA_DIR, "api-token")
    if not os.path.exists(path):
        raise SystemExit(f"No API token at {path}. Is Homebrowser running? Try: homebrowser start")
    with open(path) as fh:
        return fh.read().strip()


def call(method, path, body=None, timeout=None):
    request = urllib.request.Request(
        API_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Homebrowser API {exc.code}: {exc.read().decode()[:500]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach the Homebrowser API at {API_URL}: {exc.reason}. "
                         f"Try: homebrowser start") from exc


SHARE_NOTE = "The console asks for the console password, which only the user knows."
PASSWORD_LINK_WARNING = ("No console password yet, so this is a one-time link that sets one first. "
                         "Anyone who opens it before the user can take the console: send it only to them.")


def share_note(link):
    return SHARE_NOTE if link["console_password"] else PASSWORD_LINK_WARNING


def print_run(run):
    print(run["reply"] or "(no reply)")
    print()
    if run.get("needs_user"):
        print(f"Needs the user: {run['needs_user']['task']}")
        print(f"  {run['needs_user']['share_url']}")
        print("  It opens the console on this session" + (", after setting a console password."
              if "/password?" in run["needs_user"]["share_url"] else "; it asks for the console password."))
        print("  When they are done, continue the session.")
        print()
    print(f"status:  {run['status']}   steps: {run['steps']}   cost: ${run['cost_usd']:.4f}")
    if run.get("files"):
        where = run.get("host_dir") and os.path.join(run["host_dir"], "files")
        print(f"files:   {len(run['files'])} saved" + (f" in {where}" if where else ""))
    for note in run.get("notes_written") or []:
        print(f"noted:   {note['site']}: {note['text']}")
    print(f"run:     {run['run']}")
    print(f"session: {run['session']}   (continue with: homebrowser chat --session {run['session']} \"...\")")


PAGES = {
    "key": {"field": "key_setup", "again": "homebrowser key --wait",
            "opened": "The page is open; waiting for the key to be submitted.",
            "rejected": "That was not saved",
            "saved": "Saved. OpenRouter accepted the key."},
    "password": {"field": "password_setup", "again": "homebrowser password",
                 "opened": "The page is open; waiting for the password to be submitted.",
                 "rejected": "That was not saved",
                 "saved": "Console password saved. Every device that was signed in is signed out."},
}


def wait_for_page(page):
    """Report what happens to the latest `homebrowser key` or `homebrowser password` link until it is settled."""
    messages = PAGES[page]
    print("Waiting for the page (the link lasts 15 minutes)...", flush=True)
    last = None
    while True:
        progress = call("GET", "/v1/status")[messages["field"]] or {}
        state = progress.get("state")
        if state != last:
            if state == "opened":
                print(messages["opened"], flush=True)
            elif state == "rejected":
                print(f"{messages['rejected']}: {progress.get('error')} The page is still open for another try.",
                      flush=True)
            elif state == "saved":
                print(messages["saved"])
                return 0
            elif state == "expired":
                print(f"The link expired before anything was saved. Run `{messages['again']}` for a new one.")
                return 1
            last = state
        time.sleep(3)


def main():
    parser = argparse.ArgumentParser(prog="homebrowser")
    commands = parser.add_subparsers(dest="command", required=True)

    chat = commands.add_parser("chat", help="send Homebrowser a message; it works the browser and replies")
    chat.add_argument("message")
    chat.add_argument("--session", help="continue this session (same tab, same conversation)")
    chat.add_argument("--max-stuck-steps", type=int, default=12,
                      help="give up after this many steps in a row that change nothing on the page")
    chat.add_argument("--no-wait", action="store_true", help="return the run id at once")
    chat.add_argument("--json", action="store_true")

    runs = commands.add_parser("run", help="show a run: status, steps, files")
    runs.add_argument("run")
    runs.add_argument("--json", action="store_true")

    cancel = commands.add_parser("cancel", help="stop a run")
    cancel.add_argument("run")

    notes = commands.add_parser("notes", help="what Homebrowser has learned, per site")
    notes.add_argument("site", nargs="?")

    key = commands.add_parser("key", help="open a page to paste the OpenRouter key into")
    key.add_argument("--in-browser", action="store_true",
                     help="open it in Homebrowser's own browser, for use through the viewer")
    key.add_argument("--json", action="store_true", help="print the link instead of opening it")
    key.add_argument("--wait", action="store_true",
                     help="after printing the link, wait until the key is saved, rejected, or the link expires")
    key.add_argument("--watch", action="store_true",
                     help="wait on the link already issued (by `key --json`) without making a new one")

    password = commands.add_parser("password", help="a one-time link to set or reset the console password")
    password.add_argument("--share", action="store_true",
                          help="a link through the share, for a user away from this computer")
    password.add_argument("--json", action="store_true", help="print the link and return at once")

    smoke = commands.add_parser("smoke", help="reach a few sites and screenshot them")
    smoke.add_argument("sites", nargs="*", help="domains (default: the list in <data>/smoke-sites.txt)")
    smoke.add_argument("--json", action="store_true")

    share = commands.add_parser("share", help="an HTTPS link to the console")
    share.add_argument("--json", action="store_true")

    unshare = commands.add_parser("unshare", help="stop the share link")
    unshare.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="browser, model, key, and queue")
    status.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "chat":
        body = {"message": args.message, "session": args.session,
                "max_stuck_steps": args.max_stuck_steps, "wait": not args.no_wait}
        run = call("POST", "/v1/chat", body)
        if args.json:
            print(json.dumps(run, indent=2))
        elif args.no_wait:
            print(f"run {run['run']} queued in session {run['session']}; check it with: homebrowser run {run['run']}")
        else:
            print_run(run)
        return 0 if run["status"] in ("done", "needs_input", "needs_user", "queued") else 1

    if args.command == "run":
        run = call("GET", f"/v1/runs/{args.run}")
        if args.json:
            print(json.dumps(run, indent=2))
            return 0
        for step in run.get("trace") or []:
            flag = f" [bot check: {step['block_type']}]" if step.get("blocked") else ""
            print(f"[{step['step']}] {step.get('url', '')[:90]}{flag}")
            print(f"     {step.get('observation') or ''}")
            print(f"     -> {step.get('did', '')}")
        print()
        print_run(run)
        return 0

    if args.command == "cancel":
        print(json.dumps(call("POST", f"/v1/runs/{args.run}/cancel"), indent=2))
        return 0

    if args.command == "key":
        if args.watch:
            return wait_for_page("key")
        link = call("POST", "/v1/setup-link", {"in_browser": args.in_browser})
        if args.json and not args.wait:
            print(json.dumps(link, indent=2))
            return 0
        if args.wait:
            if args.in_browser:
                print("Opened the setup page in Homebrowser's browser; the user fills it in there "
                      "through the console (a `homebrowser share` link).", flush=True)
            else:
                print(f"Paste the OpenRouter key into: {link['setup_url']}", flush=True)
            return wait_for_page("key")
        if args.in_browser:
            print("Opened the key page in Homebrowser's browser. Paste the key there "
                  "(through the viewer, or `homebrowser share` from a phone).")
        elif webbrowser.open(link["setup_url"]):
            print(f"Opened {link['setup_url']}\nPaste the key there; it is checked and saved on this machine.")
        else:
            print(f"Open this on this machine and paste the key there:\n  {link['setup_url']}")
        print("The link works once and lasts 15 minutes.")
        return 0

    if args.command == "password":
        link = call("POST", "/v1/password-link", {"share": args.share}, timeout=90 if args.share else None)
        if args.json:
            print(json.dumps(link, indent=2))
            return 0
        print(f"Set the console password at: {link['password_url']}", flush=True)
        if args.share:
            print("It works once, for 15 minutes, from anywhere. Anyone who opens it first can set the "
                  "password, so send it only to the user.", flush=True)
        return wait_for_page("password")

    if args.command == "smoke":
        print("Visiting " + (", ".join(args.sites) or "the sites in smoke-sites.txt")
              + " from Bing, the way a person would. This takes a few minutes.", file=sys.stderr, flush=True)
        result = call("POST", "/v1/smoke", {"sites": args.sites})
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for item in result["results"]:
                mark = "ok  " if item["ok"] else "FAIL"
                cleared = " (cleared a bot check)" if item["worked_challenge"] and item["ok"] else ""
                print(f"{mark} {item['site']:16} {item.get('title') or item.get('error') or ''}{cleared}")
            print(f"\n{result['passed']} of {result['total']} reached. Report: {result['host_report'] or result['report']}")
        return 0 if result["passed"] == result["total"] else 1

    if args.command == "share":
        # The tunnel can take a while to come up; give it longer than the server waits.
        link = call("POST", "/v1/share", timeout=90)
        if args.json:
            print(json.dumps({"event": "share_ready", **link}))
            return 0
        print(f"{link['url']}\n\nThe Homebrowser console: every session, the live browser, and what needs you.\n"
              f"{share_note(link)}\nThe link stays the same when Homebrowser restarts or updates.\n"
              "Stop it with: homebrowser unshare. The next share has a new address, so devices sign in again.")
        return 0

    if args.command == "unshare":
        stopped = call("POST", "/v1/unshare")["stopped"]
        if args.json:
            print(json.dumps({"event": "share_stopped", "stopped": stopped}))
        else:
            print("Stopped sharing. The old link no longer works, and the devices signed in "
                  "through it are signed out." if stopped else "Nothing is being shared.")
        return 0

    if args.command == "status":
        result = call("GET", "/v1/status")
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"browser connected: {result['browser_connected']}\nmodel: {result['model']}\n"
                  f"openrouter key: {'set' if result['openrouter_key'] else 'missing (run: homebrowser key)'}\n"
                  f"queue: {result['queue_depth']}")
        return 0

    if args.command == "notes":
        if args.site:
            result = call("GET", f"/v1/notes/{args.site}")
            print("Shipped guidance:\n" + (result["guidance"] or "(none)"))
            print("\nLearned:\n" + (result["notes"] or "(none yet)"))
            print()
            for run in result["runs"]:
                blocks = f"  bot checks: {', '.join(run['blocks'])}" if run["blocks"] else ""
                print(f"{run['time'][:16]}  {run['status']:<11} {run['message'][:70]}{blocks}")
        else:
            result = call("GET", "/v1/notes")
            print("Learned notes: " + (", ".join(result["sites"]) or "none yet"))
            print("Shipped guidance: " + ", ".join(result["guided_sites"]))
            print("\nGeneral guidance:\n" + result["guidance"])
            if result["general"]:
                print("\nGeneral:\n" + result["general"])
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
