"""Unit tests for the address bar's history suggestions (stealthbox_launcher/history.py).

No running Stealthbox needed. A writer stands in for Chrome: it holds the History
file with an exclusive lock, a truncated rollback journal and a write transaction
that is almost always open, the way Chrome's history backend does.

    pytest serve/tests/test_history.py
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from stealthbox_launcher import history

URLS_TABLE = ("CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, title LONGVARCHAR, "
              "visit_count INTEGER DEFAULT 0 NOT NULL, typed_count INTEGER DEFAULT 0 NOT NULL, "
              "last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0 NOT NULL)")
VISITS_TABLE = "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER NOT NULL, visit_time INTEGER NOT NULL)"


class Chrome:
    """Writes History the way Chrome does: exclusive lock, TRUNCATE journal, small cache."""

    def __init__(self, path):
        self.database = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.database.execute("PRAGMA page_size=4096")
        self.database.execute("PRAGMA locking_mode=EXCLUSIVE")
        self.database.execute("PRAGMA journal_mode=TRUNCATE")
        # A small cache spills changed pages into the file before the commit.
        self.database.execute("PRAGMA cache_size=8")
        self.database.execute(URLS_TABLE)
        self.database.execute(VISITS_TABLE)
        self.visits = 0
        self.visit("https://www.example.com/", "Example Domain", typed=3)
        self.visit("https://github.com/notifications", "Notifications", typed=1)
        self.database.execute("BEGIN")
        self.browse(300)
        self.database.execute("COMMIT")
        # Chrome's next transaction, not committed yet: its journal is hot.
        self.database.execute("BEGIN")
        self.visit("https://uncommitted.example.net/", "Not committed")
        self.browse(300)

    def visit(self, url, title, typed=0):
        self.visits += 1
        self.database.execute(
            "INSERT INTO urls(url, title, visit_count, typed_count, last_visit_time) VALUES (?, ?, 1, ?, ?)",
            (url, title, typed, self.visits))
        self.database.execute("INSERT INTO visits(url, visit_time) VALUES (last_insert_rowid(), ?)", (self.visits,))

    def browse(self, pages):
        for _ in range(pages):
            self.visit(f"https://news.example.org/story/{self.visits}?" + "x" * 300, f"Story {self.visits} " + "y" * 300)


@pytest.fixture
def profile(tmp_path):
    (tmp_path / "Default").mkdir()
    return str(tmp_path)


@pytest.fixture
def chrome(profile):
    writer = Chrome(os.path.join(profile, "Default", "History"))
    yield writer
    writer.database.close()


def test_suggestions_rank_a_matching_host_first_then_typed_then_visited(profile, chrome):
    found = history.suggestions(profile, "exam")
    assert found[0] == {"url": "https://www.example.com/", "address": "example.com/", "title": "Example Domain"}
    assert all("example" in suggestion["url"] for suggestion in found)
    assert len(found) == 8


def test_no_history_file_yet_means_no_suggestions(profile):
    assert history.suggestions(profile, "example") == []


def test_the_copy_reads_what_chrome_has_committed_while_it_holds_a_transaction_open(profile, chrome):
    assert os.path.getsize(os.path.join(profile, "Default", "History-journal")) > 0
    assert history.suggestions(profile, "github")[0]["address"] == "github.com/notifications"
    assert history.suggestions(profile, "uncommitted") == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_profile_it_cannot_read_is_an_error_not_an_empty_history(profile, chrome):
    """The Dell's 502: the API ran as the data folder's owner and could not see into
    Chrome's root-only profile, so it read an empty database (`no such table: urls`)."""
    os.chmod(os.path.join(profile, "Default"), 0)
    try:
        with pytest.raises(PermissionError):
            history.suggestions(profile, "github")
    finally:
        os.chmod(os.path.join(profile, "Default"), 0o700)
