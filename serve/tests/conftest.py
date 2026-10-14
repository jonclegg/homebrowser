"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path_factory, monkeypatch):
    """Keep the suite away from the developer's real ``~/.stealthbox``.

    ``monkeypatch`` means a test setting its own ``STEALTHBOX_CACHE_DIR`` still wins.
    """
    monkeypatch.setenv(
        "STEALTHBOX_CACHE_DIR",
        str(tmp_path_factory.mktemp("stealthbox_launcher-cache")),
    )
