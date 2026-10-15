"""Address-bar suggestions from the browser's own history.

Chrome keeps an identity's history in a SQLite file it holds with an exclusive
lock, so this reads a copy of it. cdp-server serves it (GET /history): it runs as
root beside Chrome, whose profile (mode 700) the API cannot read on a Linux host,
where the API runs as the data folder's owner.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile


def suggestions(profile, text, limit=8):
    """Addresses from the history in Chrome's `profile` folder matching `text`, as
    Chrome's address bar ranks them: a host that starts with it first, then typed,
    then visited most."""
    history = os.path.join(profile, "Default", "History")
    text = text.strip().lower()
    with tempfile.TemporaryDirectory() as folder:
        copy = os.path.join(folder, "History")
        try:
            shutil.copy(history, copy)
        except FileNotFoundError:
            # A new profile: Chrome writes History after the first page it loads.
            return []
        # Chrome holds the file open; a copy with its journal reads cleanly.
        for suffix in ("-journal", "-wal"):
            if os.path.exists(history + suffix):
                shutil.copy(history + suffix, copy + suffix)
        database = sqlite3.connect(copy)
        try:
            rows = database.execute(
                "SELECT url, title, typed_count, visit_count FROM urls WHERE hidden = 0 "
                "AND (url LIKE ? OR title LIKE ?) ORDER BY last_visit_time DESC LIMIT 400",
                (f"%{text}%", f"%{text}%")).fetchall()
        finally:
            database.close()

    def address(url):
        return re.sub(r"^https?://(www\.)?", "", url)

    rows = [row for row in rows if row[0].startswith(("http://", "https://"))]
    rows.sort(key=lambda row: (not address(row[0]).lower().startswith(text), -row[2], -row[3]))
    found, seen = [], set()
    for url, title, _typed, _visits in rows:
        if address(url) not in seen:
            seen.add(address(url))
            found.append({"url": url, "address": address(url), "title": title})
    return found[:limit]
