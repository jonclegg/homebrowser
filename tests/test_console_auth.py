"""Unit tests for the console password and signed-in devices (agent/console_auth.py).

No running Homebrowser needed. The strength test also runs the setup page's own
JavaScript in Node, so the meter and the server never disagree.

    pytest tests/test_console_auth.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))

import console_auth  # noqa: E402

SAMPLES = ["", "short", "abcdefghij", "aaaaaaaaaaaa", "MyPassword123", "homebrowser-2026!", "sunshinees7",
           "sunshinee", "Summer2024!", "correct horse battery staple", "Tr0ub4dor&3xyz", "ñandú-über-straße"]


def test_strength_refuses_short_and_guessable_passwords():
    assert console_auth.strength("short")[0] == 0
    assert console_auth.strength("password1234")[0] == 0
    assert console_auth.strength("homebrowser-2026!")[0] == 0
    assert console_auth.strength("aaaaaaaaaaaa")[0] == 0
    assert console_auth.strength("sunshinees7") == (2, "Fair")
    assert console_auth.strength("correct horse battery staple")[0] == 4


def test_check_new_wants_a_match_and_at_least_fair():
    with pytest.raises(ValueError, match="don't match"):
        console_auth.check_new("correct horse battery staple", "correct horse battery stapler")
    with pytest.raises(ValueError, match="too weak"):
        console_auth.check_new("sunshinee", "sunshinee")
    with pytest.raises(ValueError, match="too weak"):
        console_auth.check_new("abcdeghijk", "abcdeghijk")
    console_auth.check_new("sunshinees7", "sunshinees7")


def test_the_page_meter_scores_like_the_server():
    page = console_auth.password_fields("Password", "hint", required=True)
    script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    rules = script[:script.index("  const password = document")]
    program = (rules.replace("(() => {", "", 1)
               + f"\nconsole.log(JSON.stringify({json.dumps(SAMPLES)}.map(strength)));")
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [list(console_auth.strength(sample)) for sample in SAMPLES]


def test_password_checks_and_locks_after_five_wrong_tries(tmp_path):
    password = console_auth.Password(str(tmp_path / "console-password"))
    assert not password.is_set()
    password.set("correct horse battery staple")
    assert password.is_set()
    assert oct(os.stat(password.path).st_mode & 0o777) == "0o600"
    assert "correct horse" not in open(password.path).read()
    password.check("correct horse battery staple")

    for left in (4, 3, 2, 1):
        with pytest.raises(console_auth.SignInRefused, match=f"{left} tr"):
            password.check("wrong")
    with pytest.raises(console_auth.SignInRefused, match="locked"):
        password.check("wrong")
    with pytest.raises(console_auth.SignInRefused, match="Too many"):
        password.check("correct horse battery staple")

    password.set("another long passphrase")
    password.check("another long passphrase")


def test_a_device_cookie_works_only_on_its_own_host(tmp_path):
    devices = console_auth.Devices(str(tmp_path / "devices.json"))
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"
    token = devices.sign_in("one.trycloudflare.com", iphone)
    device = devices.find(token, "one.trycloudflare.com")
    assert device["label"] == "iPhone · Safari"
    assert devices.find(token, "two.trycloudflare.com") is None
    assert devices.find("not-a-token", "one.trycloudflare.com") is None
    assert devices.find("", "one.trycloudflare.com") is None
    assert token not in open(devices.path).read()

    reloaded = console_auth.Devices(devices.path)
    assert reloaded.find(token, "one.trycloudflare.com")["id"] == device["id"]

    device["expires"] = 0
    assert devices.find(token, "one.trycloudflare.com") is None


def test_sign_out_one_device_one_host_or_all(tmp_path):
    devices = console_auth.Devices(str(tmp_path / "devices.json"))
    first = devices.sign_in("one.trycloudflare.com", "Macintosh Chrome/140")
    second = devices.sign_in("one.trycloudflare.com", "Windows Edg/140")
    local = devices.sign_in("127.0.0.1:8423", "Macintosh Safari/605")
    view = devices.view(devices.find(first, "one.trycloudflare.com"))
    assert [entry["current"] for entry in view].count(True) == 1
    assert {entry["label"] for entry in view} == {"Mac · Chrome", "Windows · Edge", "Mac · Safari"}

    assert devices.sign_out(device_id=devices.find(second, "one.trycloudflare.com")["id"]) == 1
    assert devices.find(second, "one.trycloudflare.com") is None
    assert devices.sign_out(host="one.trycloudflare.com") == 1
    assert devices.find(first, "one.trycloudflare.com") is None
    assert devices.find(local, "127.0.0.1:8423") is not None
    assert devices.sign_out() == 1
    assert devices.view(None) == []
