# Homebrowser

A browser for your AI agent that big websites don't block, and that stays logged in. It runs in a container on your own computer.

## Install

Tell your agent (Claude Code, Codex, Cursor, Grok, and so on):

```text
Install Homebrowser from https://github.com/jonclegg/homebrowser
```

That's it. Your agent will:

1. **Install Homebrowser** on your Mac, Linux, or Windows computer.
2. **Ask before installing Docker**, if your computer doesn't have it yet.
3. **Open a page for your OpenRouter key.** Homebrowser's built-in agent uses it to drive the browser. You paste the key into that page on your own computer, never into the chat. Get one at [openrouter.ai/keys](https://openrouter.ai/keys).
4. **Prove it works.** Your agent asks which two or three sites you use, and Homebrowser visits them the way a person would, and shows you a screenshot of each.

## Use it

Ask your agent to do things on websites "with Homebrowser", for example: *"With Homebrowser, find a 12-cup coffee maker under $50 at the store I usually use."* Homebrowser drives a real browser on your computer, works through bot checks, and remembers your logins between sessions.

Ask your agent to share a link to the Homebrowser console: every session it is running, the live browser, and anything that needs you, from your phone or any computer.

## Good to know

- **Sign in yourself.** When a site needs a password, Homebrowser stops and your agent sends you a private link to the browser, so you can sign in from your phone or any computer. Don't paste passwords into the chat. Your agent can type in texted sign-in codes for you.
- **It runs on your computer.** Traffic leaves from your own internet connection, and nothing is reachable from outside unless you share the console link, and the console asks for a password you choose when you set Homebrowser up.
- **The details** for agents and the curious are in [AGENTS.md](AGENTS.md) and [the agent guide](skills/homebrowser/SKILL.md). The browser is Chromium with [Fury](https://github.com/furyteamtop/fury-antidetect-browser)'s fingerprint patches, built for Linux from [`browser/`](browser).

## License

Homebrowser's own code is [MIT licensed](LICENSE). The browser is Chromium (BSD-3-Clause) with Fury's patches (BSD-3-Clause); see [NOTICE](NOTICE) for every component's license. Homebrowser is an independent project, not affiliated with the Fury authors or any website it visits.
