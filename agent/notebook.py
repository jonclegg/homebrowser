"""Homebrowser's notebook: what it knows about each site.

General guidance ships with Homebrowser in agent/guidance/general.md: how bot
checks look and what beats them. Guidance for single sites does not ship; a site
pack installed separately puts it in <data>/guidance/sites/<site>.md. Both are
read-only to Homebrowser.

What Homebrowser learns itself is kept between runs, two kinds of entry per site,
under <data>/notes/sites/:

* <site>.md     lessons the model wrote itself ("example.com: the photo gallery
                opens from the main image; Esc closes it").
* <site>.jsonl  the run log, written by Homebrowser after every run that touched
                the site: the message, outcome, bot checks seen, steps, cost.

Lessons that are not about one site go to <data>/notes/general.md. Lookup is by
exact domain: the browser's current host names the site, so there is nothing to
search. A subdomain (m.example.com) also gets its parent site's entries.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

NOTES_SHOWN = 30
GENERAL_SHOWN = 15
RUNS_SHOWN = 5


GUIDANCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guidance")


def lookup_names(site):
    """example.com for example.com; m.example.com, then example.com."""
    labels = site.split(".")
    return [".".join(labels[index:]) for index in range(len(labels) - 1)]


class Notebook:
    def __init__(self, data_dir, guidance_dir=GUIDANCE_DIR):
        self.guidance_dir = guidance_dir
        self.site_guidance_dir = os.path.join(data_dir, "guidance", "sites")
        os.makedirs(self.site_guidance_dir, exist_ok=True)
        self.root = os.path.join(data_dir, "notes")
        self.sites_dir = os.path.join(self.root, "sites")
        os.makedirs(self.sites_dir, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, site, suffix):
        if site == "general":
            return os.path.join(self.root, "general.md")
        if not re.fullmatch(r"[a-z0-9.-]+", site):
            raise ValueError(f"not a site name: {site!r}")
        return os.path.join(self.sites_dir, site + suffix)

    def add_note(self, site, text, run_id):
        line = f"- {time.strftime('%Y-%m-%d')} ({run_id}): {' '.join(text.split())}\n"
        with self.lock, open(self._path(site, ".md"), "a") as fh:
            fh.write(line)

    def log_run(self, site, entry):
        with self.lock, open(self._path(site, ".jsonl"), "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def notes_for(self, site, limit=NOTES_SHOWN):
        path = self._path(site, ".md")
        if not os.path.exists(path):
            return ""
        with self.lock, open(path) as fh:
            return "".join(fh.readlines()[-limit:]).strip()

    def runs_for(self, site, limit=RUNS_SHOWN):
        path = self._path(site, ".jsonl")
        if not os.path.exists(path):
            return []
        with self.lock, open(path) as fh:
            return [json.loads(line) for line in fh.readlines()[-limit:]]

    def guidance_for(self, site):
        """Shipped general.md for "general", else the site pack's file for the site or its parent."""
        if site == "general":
            paths = [os.path.join(self.guidance_dir, "general.md")]
        else:
            paths = [os.path.join(self.site_guidance_dir, name + ".md") for name in lookup_names(site)]
        for path in paths:
            if os.path.exists(path):
                with open(path) as fh:
                    return fh.read().strip()
        return ""

    def guided_sites(self):
        return sorted(name.removesuffix(".md") for name in os.listdir(self.site_guidance_dir) if name.endswith(".md"))

    def _learned_site(self, site):
        """The site name learned entries are filed under: the site, or a parent with entries."""
        for name in lookup_names(site):
            if os.path.exists(self._path(name, ".md")) or os.path.exists(self._path(name, ".jsonl")):
                return name
        return site

    def context_for(self, site):
        """The notebook section of the prompt for the site the browser is on."""
        parts = []
        guidance = self.guidance_for("general")
        if guidance:
            parts.append(f"Guidance that ships with Homebrowser:\n{guidance}")
        general = self.notes_for("general", GENERAL_SHOWN)
        if general:
            parts.append(f"Your general notes:\n{general}")
        if site:
            guidance = self.guidance_for(site)
            if guidance:
                parts.append(f"Guidance for {site}:\n{guidance}")
            learned = self._learned_site(site)
            notes = self.notes_for(learned)
            if notes:
                parts.append(f"Your notes on {learned}:\n{notes}")
            runs = self.runs_for(learned)
            if runs:
                lines = [
                    f"- {run['time'][:16]} {run['status']}: {run['message'][:80]!r}"
                    + (f" bot checks: {', '.join(run['blocks'])}" if run["blocks"] else "")
                    for run in runs
                ]
                parts.append(f"Recent runs on {site}:\n" + "\n".join(lines))
        return "\n\n".join(parts) or "(nothing yet)"

    def sites(self):
        names = {
            name.rsplit(".", 1)[0]
            for name in os.listdir(self.sites_dir)
            if name.endswith((".md", ".jsonl"))
        }
        return sorted(names)
