"""Unit tests for cdp-server — parse_connection_params, parse_cli_args, URL rewriting, connection tracking."""

import asyncio
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

aiohttp = pytest.importorskip("aiohttp", reason="cdp-server requires aiohttp")

# Load cdp-server as a module (no .py extension).
_bin_path = str(Path(__file__).resolve().parents[1] / "cdp-server")
_loader = importlib.machinery.SourceFileLoader("cdp-server", _bin_path)
_spec = importlib.util.spec_from_file_location("cdp-server", _bin_path, loader=_loader)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cdp-server"] = _mod
_loader.exec_module(_mod)

parse_connection_params = _mod.parse_connection_params
parse_cli_args = _mod.parse_cli_args
ChromePool = _mod.ChromePool
_default_data_dir = _mod._default_data_dir
_external_host = _mod._external_host
_ws_scheme = _mod._ws_scheme
ChromeProcess = _mod.ChromeProcess
launch_conflicts = _mod.launch_conflicts
window_geometry_fits = _mod.window_geometry_fits
viewport_origin = _mod.viewport_origin
xdotool_commands = _mod.xdotool_commands
SAFE_SEED_RE = _mod.SAFE_SEED_RE
RESERVED_SEEDS = _mod.RESERVED_SEEDS


# ---------------------------------------------------------------------------
# parse_connection_params
# ---------------------------------------------------------------------------


class TestParseConnectionParams:
    def test_empty_query(self):
        result = parse_connection_params("")
        assert result["seed"] is None
        assert result["extra_args"] == []

    def test_fingerprint_seed(self):
        result = parse_connection_params("fingerprint=12345")
        assert result["seed"] == "12345"

    def test_timezone_and_locale(self):
        result = parse_connection_params("fingerprint=1&timezone=Asia/Tokyo&locale=ja-JP")
        assert result["timezone"] == "Asia/Tokyo"
        assert result["locale"] == "ja-JP"

    def test_proxy(self):
        result = parse_connection_params("proxy=http://proxy:8080")
        assert result["proxy"] == "http://proxy:8080"

    def test_geoip_true_variants(self):
        for val in ("true", "1", "yes", "True", "YES"):
            result = parse_connection_params(f"geoip={val}")
            assert result["geoip"] is True, f"geoip={val} should be True"

    def test_geoip_false(self):
        for val in ("false", "0", "no", "anything"):
            result = parse_connection_params(f"geoip={val}")
            assert result["geoip"] is False, f"geoip={val} should be False"

    def test_generic_fingerprint_params(self):
        qs = "fingerprint=1&platform=windows&hardware-concurrency=8&gpu-vendor=NVIDIA"
        result = parse_connection_params(qs)
        assert "--fingerprint-platform=windows" in result["extra_args"]
        assert "--fingerprint-hardware-concurrency=8" in result["extra_args"]
        assert "--fingerprint-gpu-vendor=NVIDIA" in result["extra_args"]

    def test_special_params_not_in_extra_args(self):
        qs = "fingerprint=1&timezone=UTC&locale=en-US&proxy=http://x:1&geoip=true"
        result = parse_connection_params(qs)
        assert result["extra_args"] == []

    def test_multiple_values_takes_first(self):
        result = parse_connection_params("fingerprint=111&fingerprint=222")
        assert result["seed"] == "111"


# ---------------------------------------------------------------------------
# parse_cli_args
# ---------------------------------------------------------------------------


class TestParseCliArgs:
    def test_defaults(self):
        config, passthrough = parse_cli_args([])
        assert config["port"] == 9222
        assert config["headless"] is True
        assert config["data_dir"] is not None
        assert config["idle_timeout"] == 300.0
        assert config["max_processes"] == 8
        assert config["geoip"] is False
        assert passthrough == []

    def test_fingerprint_flags_become_launch_defaults(self):
        config, passthrough = parse_cli_args(["--fingerprint-platform=macos", "--no-sandbox"])
        assert config["default_fingerprint_args"] == ["--fingerprint-platform=macos"]
        assert passthrough == ["--no-sandbox"]

    def test_geoip_and_max_processes(self):
        config, passthrough = parse_cli_args(["--geoip", "--max-processes=3"])
        assert config["geoip"] is True
        assert config["max_processes"] == 3
        assert passthrough == []

    def test_max_processes_must_be_positive(self):
        with pytest.raises(ValueError):
            parse_cli_args(["--max-processes=0"])

    def test_custom_port(self):
        config, _ = parse_cli_args(["--port=8080"])
        assert config["port"] == 8080

    def test_headless_false(self):
        config, passthrough = parse_cli_args(["--headless=false"])
        assert config["headless"] is False
        # Chromium treats any --headless switch as headless, so it is consumed
        assert "--headless=false" not in passthrough

    def test_strips_remote_debugging_flags(self):
        args = ["--remote-debugging-port=9999", "--remote-debugging-address=0.0.0.0", "--no-sandbox"]
        config, passthrough = parse_cli_args(args)
        assert passthrough == ["--no-sandbox"]

    def test_passthrough_args(self):
        args = ["--no-sandbox", "--disable-gpu", "--fingerprint=999"]
        config, passthrough = parse_cli_args(args)
        # --fingerprint=999 is consumed into config["default_seed"], not passed through
        assert passthrough == ["--no-sandbox", "--disable-gpu"]
        assert config["default_seed"] == "999"

    def test_port_not_in_passthrough(self):
        _, passthrough = parse_cli_args(["--port=9222", "--no-sandbox"])
        assert "--port=9222" not in passthrough
        assert "--no-sandbox" in passthrough

    def test_custom_data_dir(self):
        config, passthrough = parse_cli_args(["--data-dir=/custom/path", "--no-sandbox"])
        assert config["data_dir"] == "/custom/path"
        assert "--data-dir=/custom/path" not in passthrough

    def test_data_dir_not_in_passthrough(self):
        _, passthrough = parse_cli_args(["--data-dir=/tmp/test"])
        assert not any(a.startswith("--data-dir=") for a in passthrough)

    def test_idle_timeout_not_in_passthrough(self):
        config, passthrough = parse_cli_args(["--idle-timeout=30", "--no-sandbox"])
        assert config["idle_timeout"] == 30.0
        assert "--idle-timeout=30" not in passthrough
        assert "--no-sandbox" in passthrough

    @pytest.mark.parametrize("value", ["0", "off", "false", "none", "disabled"])
    def test_idle_timeout_disabled_values(self, value):
        config, _ = parse_cli_args([f"--idle-timeout={value}"])
        assert config["idle_timeout"] == 0.0

    def test_idle_timeout_env_default(self, monkeypatch):
        monkeypatch.setenv("STEALTHBOX_IDLE_TIMEOUT", "2.5")
        config, _ = parse_cli_args([])
        assert config["idle_timeout"] == 2.5

    def test_idle_timeout_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("STEALTHBOX_IDLE_TIMEOUT", "2.5")
        config, _ = parse_cli_args(["--idle-timeout=9"])
        assert config["idle_timeout"] == 9.0

    def test_idle_timeout_rejects_negative_values(self):
        with pytest.raises(ValueError):
            parse_cli_args(["--idle-timeout=-1"])

    @patch("os.path.exists", return_value=True)
    def test_default_data_dir_docker(self, _mock):
        assert _default_data_dir() == "/tmp/cdp-server"

    @patch("os.path.exists", return_value=False)
    def test_default_data_dir_bare_metal(self, _mock):
        result = _default_data_dir()
        assert result.endswith(".stealthbox/cdp-server")


# ---------------------------------------------------------------------------
# External host detection
# ---------------------------------------------------------------------------


class TestExternalHost:
    """Test public host selection for rewritten CDP WebSocket URLs."""

    class _Request:
        def __init__(self, headers, port=9222, scheme="http", query_string=""):
            self.headers = headers
            self.app = {"port": port}
            self.scheme = scheme
            self.query_string = query_string

    def test_forwarded_host_overrides_internal_host(self):
        request = self._Request({
            "Host": "localhost:8080",
            "X-Forwarded-Host": "cdp.example.com:443",
        })
        assert _external_host(request) == "cdp.example.com:443"

    def test_forwarded_host_uses_first_value(self):
        request = self._Request({
            "Host": "internal:9222",
            "X-Forwarded-Host": "public.example.com, internal:9222",
        })
        assert _external_host(request) == "public.example.com"

    def test_blank_forwarded_host_falls_back_to_host_header(self):
        request = self._Request({
            "Host": "internal:9222",
            "X-Forwarded-Host": "   ",
        })
        assert _external_host(request) == "internal:9222"

    def test_falls_back_to_host_header(self):
        request = self._Request({"Host": "localhost:9222"})
        assert _external_host(request) == "localhost:9222"

    def test_falls_back_to_app_port_without_host_header(self):
        request = self._Request({}, port=9333)
        assert _external_host(request) == "localhost:9333"

    def test_forwarded_proto_selects_wss(self):
        request = self._Request({"X-Forwarded-Proto": "https"}, scheme="http")
        assert _ws_scheme(request) == "wss"

    def test_forwarded_proto_uses_first_value(self):
        request = self._Request({"X-Forwarded-Proto": "https, http"}, scheme="http")
        assert _ws_scheme(request) == "wss"


class TestJsonHandlers:
    """Verify handlers rewrite CDP WebSocket URLs to the public cdp-server endpoint."""

    class _Request:
        def __init__(self, headers, query_string="fingerprint=seed1", port=9222, scheme="http"):
            self.headers = headers
            self.query_string = query_string
            self.scheme = scheme
            self.app = {"port": port, "pool": self._Pool()}

        class _Pool:
            async def get_or_launch(self, **_kwargs):
                return SimpleNamespace(cdp_port=5100)

    class _FakeResponse:
        def __init__(self, data):
            self._data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def json(self):
            return self._data

    class _FakeSession:
        def __init__(self, data):
            self._data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def get(self, *_args, **_kwargs):
            return TestJsonHandlers._FakeResponse(self._data)

    def _patch_session(self, monkeypatch, data):
        monkeypatch.setattr(
            _mod.aiohttp,
            "ClientSession",
            lambda *_args, **_kwargs: self._FakeSession(data),
        )

    def test_json_version_uses_forwarded_host_and_proto(self, monkeypatch):
        self._patch_session(monkeypatch, {
            "webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/browser-guid",
        })
        request = self._Request({
            "Host": "internal:9222",
            "X-Forwarded-Host": "cdp.example.com",
            "X-Forwarded-Proto": "https",
        })

        response = asyncio.run(_mod.handle_json_version(request))
        payload = json.loads(response.text)

        assert payload["webSocketDebuggerUrl"] == (
            "wss://cdp.example.com/fingerprint/seed1/devtools/browser/browser-guid"
        )

    def test_json_list_uses_forwarded_host_and_proto(self, monkeypatch):
        self._patch_session(monkeypatch, [{
            "webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/page/page-guid",
        }])
        request = self._Request({
            "Host": "internal:9222",
            "X-Forwarded-Host": "cdp.example.com",
            "X-Forwarded-Proto": "https",
        })

        response = asyncio.run(_mod.handle_json_list(request))
        payload = json.loads(response.text)

        assert payload[0]["webSocketDebuggerUrl"] == (
            "wss://cdp.example.com/fingerprint/seed1/devtools/page/page-guid"
        )


# ---------------------------------------------------------------------------
# URL rewriting logic (pure string manipulation, extracted from handlers)
# ---------------------------------------------------------------------------


class TestWebSocketOriginGuard:
    """Verify cdp-server rejects browser-origin CDP WebSocket hijacks."""

    def test_absent_origin_allowed_for_non_browser_cdp_clients(self):
        assert _mod._origin_is_allowed(None, "127.0.0.1:9555")

    def test_matching_origin_host_allowed(self):
        assert _mod._origin_is_allowed("http://127.0.0.1:9555", "127.0.0.1:9555")

    def test_chrome_devtools_origin_allowed(self):
        assert _mod._origin_is_allowed("devtools://devtools", "127.0.0.1:9555")
        assert _mod._origin_is_allowed("chrome-devtools://devtools", "127.0.0.1:9555")

    @pytest.mark.parametrize("origin", [
        "http://attacker.example",
        "https://attacker.example",
        "http://PUBLIC_HOST:9555",
        "http://attacker.example:9555",
        "http://127.0.0.1:9555/",
        "http://127.0.0.1:9555/path",
        "http://127.0.0.1:9555?q=1",
        "http://127.0.0.1:9555#fragment",
        "http://user@127.0.0.1:9555",
        "http://@127.0.0.1:9555",
        "http://:@127.0.0.1:9555",
        "http://127.0.0.1:",
        "null",
        "file://",
    ])
    def test_untrusted_browser_origins_rejected(self, origin):
        assert not _mod._origin_is_allowed(origin, "127.0.0.1:9555")

    def test_public_origin_matching_host_is_still_rejected(self):
        assert not _mod._origin_is_allowed("http://attacker.example:9555", "attacker.example:9555")

    @pytest.mark.parametrize("host", [
        "user@127.0.0.1:9555",
        "127.0.0.1:9555/path",
        "127.0.0.1:9555?x=1",
        "127.0.0.1:9555#fragment",
        "127.0.0.1:9555, attacker.example:9555",
        "@127.0.0.1:9555",
        ":@127.0.0.1:9555",
        "127.0.0.1:",
        "[::1]:",
    ])
    def test_malformed_host_is_rejected_even_when_hostname_is_loopback(self, host):
        assert not _mod._origin_is_allowed("http://127.0.0.1:9555", host)

    def test_request_scheme_controls_host_default_port(self):
        assert _mod._origin_is_allowed("https://localhost", "localhost", request_scheme="https")
        assert not _mod._origin_is_allowed("https://localhost", "localhost", request_scheme="http")

    def test_ws_handler_rejects_untrusted_origin_before_launching_chrome(self):
        class RejectingPool:
            async def get_or_launch(self, **_kwargs):
                raise AssertionError("untrusted origin should be rejected before launching Chrome")

        request = SimpleNamespace(
            headers={"Host": "127.0.0.1:9555", "Origin": "http://attacker.example"},
            app={"pool": RejectingPool()},
            match_info={"path": "browser/browser-guid"},
        )

        response = asyncio.run(_mod.handle_ws_default(request))

        assert response.status == 403
        assert "untrusted" in response.text.lower()

    def test_seed_ws_handler_rejects_untrusted_origin_before_launching_chrome(self):
        class RejectingPool:
            async def get_or_launch(self, **_kwargs):
                raise AssertionError("untrusted origin should be rejected before launching Chrome")

        request = SimpleNamespace(
            headers={"Host": "127.0.0.1:9555", "Origin": "http://attacker.example"},
            app={"pool": RejectingPool()},
            match_info={"seed": "abc123", "path": "page/page-guid"},
        )

        response = asyncio.run(_mod.handle_ws_seed(request))

        assert response.status == 403
        assert "untrusted" in response.text.lower()


class TestHandlerURLRewriting:
    """Verify handlers rewrite CDP WebSocket URLs to the public cdp-server endpoint."""

    def _rewrite_version(self, orig_ws: str, host: str, seed: str | None, scheme: str = "ws") -> str:
        """Replicate the URL rewrite logic from handle_json_version."""
        if seed:
            ws_path = f"fingerprint/{seed}/devtools/browser"
        else:
            ws_path = "devtools/browser"
        guid = orig_ws.rsplit("/", 1)[-1] if "/devtools/" in orig_ws else ""
        return f"{scheme}://{host}/{ws_path}/{guid}"

    def _rewrite_list_entry(self, orig_ws: str, host: str, seed: str | None, scheme: str = "ws") -> str:
        """Replicate the URL rewrite logic from handle_json_list."""
        ws_tail = orig_ws.split("/devtools/")[-1]
        if seed:
            return f"{scheme}://{host}/fingerprint/{seed}/devtools/{ws_tail}"
        else:
            return f"{scheme}://{host}/devtools/{ws_tail}"

    def test_version_rewrite_with_seed(self):
        orig = "ws://127.0.0.1:5100/devtools/browser/abc-123"
        result = self._rewrite_version(orig, "container:9222", "12345")
        assert result == "ws://container:9222/fingerprint/12345/devtools/browser/abc-123"

    def test_version_rewrite_no_seed(self):
        orig = "ws://127.0.0.1:5100/devtools/browser/abc-123"
        result = self._rewrite_version(orig, "container:9222", None)
        assert result == "ws://container:9222/devtools/browser/abc-123"

    def test_list_rewrite_page_with_seed(self):
        orig = "ws://127.0.0.1:5100/devtools/page/DEF-456"
        result = self._rewrite_list_entry(orig, "host:9222", "99")
        assert result == "ws://host:9222/fingerprint/99/devtools/page/DEF-456"

    def test_list_rewrite_page_no_seed(self):
        orig = "ws://127.0.0.1:5100/devtools/page/DEF-456"
        result = self._rewrite_list_entry(orig, "host:9222", None)
        assert result == "ws://host:9222/devtools/page/DEF-456"

    def test_list_rewrite_browser(self):
        orig = "ws://127.0.0.1:5100/devtools/browser/XYZ"
        result = self._rewrite_list_entry(orig, "host:9222", "seed1")
        assert result == "ws://host:9222/fingerprint/seed1/devtools/browser/XYZ"

    def test_wss_scheme_version(self):
        orig = "ws://127.0.0.1:5100/devtools/browser/abc-123"
        result = self._rewrite_version(orig, "host:443", "seed1", scheme="wss")
        assert result == "wss://host:443/fingerprint/seed1/devtools/browser/abc-123"

    def test_wss_scheme_list(self):
        orig = "ws://127.0.0.1:5100/devtools/page/DEF-456"
        result = self._rewrite_list_entry(orig, "host:443", "seed1", scheme="wss")
        assert result == "wss://host:443/fingerprint/seed1/devtools/page/DEF-456"


# ---------------------------------------------------------------------------
# Connection refcounting
# ---------------------------------------------------------------------------


class TestConnectionTracking:
    """Test ChromePool.connect() / disconnect() without real Chrome."""

    def _make_pool(self, idle_timeout: float = 0.0):
        return ChromePool(
            binary="/fake/chrome",
            global_args=[],
            headless=True,
            data_dir="/tmp/test-cdp-server",
            idle_timeout=idle_timeout,
        )

    def _track_process(self, pool, seed="seed1"):
        pool._processes[seed] = SimpleNamespace()

    def _track_live_process(self, pool, seed="seed1"):
        pool._processes[seed] = SimpleNamespace(
            process=SimpleNamespace(poll=lambda: None),
        )

    def test_connect_increments(self):
        pool = self._make_pool()
        pool.connect("seed1")
        assert pool._connections["seed1"] == 1
        pool.connect("seed1")
        assert pool._connections["seed1"] == 2

    def test_disconnect_decrements(self):
        pool = self._make_pool()
        pool.connect("seed1")
        pool.connect("seed1")
        pool.disconnect("seed1")
        assert pool._connections["seed1"] == 1

    def test_disconnect_to_zero_removes_key(self):
        pool = self._make_pool()
        pool.connect("seed1")
        pool.disconnect("seed1")
        assert "seed1" not in pool._connections

    def test_disconnect_below_zero_safe(self):
        pool = self._make_pool()
        pool.disconnect("nonexistent")
        assert "nonexistent" not in pool._connections

    def test_multiple_seeds_independent(self):
        pool = self._make_pool()
        pool.connect("a")
        pool.connect("b")
        pool.connect("a")
        pool.disconnect("a")
        assert pool._connections["a"] == 1
        assert pool._connections["b"] == 1

    def test_idle_cleanup_disabled_by_default(self):
        async def run():
            pool = self._make_pool()
            self._track_process(pool)

            pool.connect("seed1")
            pool.disconnect("seed1")

            await asyncio.sleep(0)
            assert pool._idle_tasks == {}

        asyncio.run(run())

    def test_disconnect_to_zero_schedules_idle_cleanup(self):
        async def run():
            pool = self._make_pool(idle_timeout=0.01)
            self._track_process(pool)
            cleaned = []

            async def fake_cleanup(seed):
                cleaned.append(seed)
                pool._processes.pop(seed, None)

            pool._cleanup_process = fake_cleanup
            pool.connect("seed1")
            pool.disconnect("seed1")

            assert "seed1" in pool._idle_tasks
            await asyncio.sleep(0.05)
            assert cleaned == ["seed1"]
            assert "seed1" not in pool._idle_tasks

        asyncio.run(run())

    def test_reconnect_cancels_pending_idle_cleanup(self):
        async def run():
            pool = self._make_pool(idle_timeout=0.03)
            self._track_process(pool)
            cleaned = []

            async def fake_cleanup(seed):
                cleaned.append(seed)
                pool._processes.pop(seed, None)

            pool._cleanup_process = fake_cleanup
            pool.connect("seed1")
            pool.disconnect("seed1")
            assert "seed1" in pool._idle_tasks

            pool.connect("seed1")
            await asyncio.sleep(0.06)

            assert cleaned == []
            assert pool._connections["seed1"] == 1
            assert "seed1" not in pool._idle_tasks

        asyncio.run(run())

    def test_discovery_refreshes_pending_idle_cleanup(self):
        async def run():
            pool = self._make_pool(idle_timeout=1.0)
            self._track_live_process(pool)

            pool.connect("seed1")
            pool.disconnect("seed1")
            first_task = pool._idle_tasks["seed1"]

            await pool.get_or_launch("seed1")
            second_task = pool._idle_tasks["seed1"]

            assert second_task is not first_task
            pool._cancel_idle_cleanup("seed1")
            await asyncio.sleep(0)
            assert "seed1" not in pool._idle_tasks

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Seed validation (CVE fix — path traversal via fingerprint param)
# ---------------------------------------------------------------------------


class TestSeedValidation:
    """Verify SAFE_SEED_RE rejects path traversal and reserved names."""

    @pytest.mark.parametrize("seed", [
        "../foo", "../../etc", "/etc/passwd", "..", ".", "foo/bar",
        "foo\\bar", "\x00evil", "", "a" * 129,
    ])
    def test_malicious_seeds_rejected(self, seed):
        assert not SAFE_SEED_RE.match(seed)

    @pytest.mark.parametrize("seed", [
        "__default__",
    ])
    def test_reserved_seeds_rejected(self, seed):
        assert seed in RESERVED_SEEDS

    @pytest.mark.parametrize("seed", [
        "12345", "my-seed_01", "ABC", "a" * 128, "0", "test-seed",
    ])
    def test_valid_seeds_accepted(self, seed):
        assert SAFE_SEED_RE.match(seed)
        assert seed not in RESERVED_SEEDS


# ---------------------------------------------------------------------------
# Path containment (_safe_rmtree)
# ---------------------------------------------------------------------------


class TestSafeRmtree:
    """Verify _safe_rmtree refuses to delete outside data_dir."""

    def _make_pool(self, data_dir: str):
        return ChromePool(
            binary="/fake/chrome",
            global_args=[],
            headless=True,
            data_dir=data_dir,
        )

    def test_refuses_path_outside_data_dir(self, tmp_path):
        data_dir = tmp_path / "profiles"
        data_dir.mkdir()
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "sentinel").touch()

        pool = self._make_pool(str(data_dir))
        pool._safe_rmtree(str(victim))

        assert victim.exists(), "Directory outside data_dir must not be deleted"

    def test_refuses_data_dir_itself(self, tmp_path):
        data_dir = tmp_path / "profiles"
        data_dir.mkdir()
        (data_dir / "sentinel").touch()

        pool = self._make_pool(str(data_dir))
        pool._safe_rmtree(str(data_dir))

        assert data_dir.exists(), "data_dir itself must not be deleted"

    def test_deletes_valid_subdirectory(self, tmp_path):
        data_dir = tmp_path / "profiles"
        data_dir.mkdir()
        subdir = data_dir / "seed-12345"
        subdir.mkdir()
        (subdir / "data").touch()

        pool = self._make_pool(str(data_dir))
        pool._safe_rmtree(str(subdir))

        assert not subdir.exists(), "Valid subdirectory should be deleted"

    def test_refuses_traversal_path(self, tmp_path):
        data_dir = tmp_path / "profiles"
        data_dir.mkdir()
        victim = tmp_path / "victim"
        victim.mkdir()

        traversal = str(data_dir / ".." / "victim")
        pool = self._make_pool(str(data_dir))
        pool._safe_rmtree(traversal)

        assert victim.exists(), "Traversal path must not be deleted"


# ---------------------------------------------------------------------------
# Default seed persistence, capacity, launch conflicts
# ---------------------------------------------------------------------------


def _live_process(**fields):
    defaults = dict(
        seed="1", process=SimpleNamespace(poll=lambda: None), cdp_port=5100,
        user_data_dir="/tmp/x", last_used=0.0,
    )
    defaults.update(fields)
    return ChromeProcess(**defaults)


class TestDefaultSeedPersistence:
    def _make_pool(self, tmp_path):
        return ChromePool(binary="/fake/chrome", global_args=[], headless=True, data_dir=str(tmp_path))

    def test_seed_created_once_and_reused(self, tmp_path):
        pool = self._make_pool(tmp_path)
        first = pool._persistent_default_seed(str(tmp_path))
        second = pool._persistent_default_seed(str(tmp_path))
        assert first == second
        assert (tmp_path / ".cdp-server-seed").read_text().strip() == first

    def test_existing_seed_file_wins(self, tmp_path):
        (tmp_path / ".cdp-server-seed").write_text("56492\n")
        assert self._make_pool(tmp_path)._persistent_default_seed(str(tmp_path)) == "56492"

    def test_invalid_seed_file_fails(self, tmp_path):
        (tmp_path / ".cdp-server-seed").write_text("../evil\n")
        with pytest.raises(RuntimeError):
            self._make_pool(tmp_path)._persistent_default_seed(str(tmp_path))


class TestLaunchConflicts:
    def test_bare_reconnect_has_no_conflict(self):
        proc = _live_process(timezone="Europe/Berlin", locale="de-DE")
        assert launch_conflicts(proc, None, None, None, None, False) == {}

    def test_same_values_have_no_conflict(self):
        proc = _live_process(timezone="Europe/Berlin", extra_args=("--fingerprint-platform=windows",))
        assert launch_conflicts(proc, ["--fingerprint-platform=windows"], "Europe/Berlin", None, None, False) == {}

    def test_different_values_conflict(self):
        proc = _live_process(timezone="Europe/Berlin")
        conflicts = launch_conflicts(proc, ["--fingerprint-taskbar-height=0"], "Asia/Tokyo", None, None, True)
        assert set(conflicts) == {"fingerprint_args", "timezone", "geoip"}

    def test_get_or_launch_rejects_conflicting_reconnect(self):
        async def run():
            pool = ChromePool(binary="/fake/chrome", global_args=[], headless=True, data_dir="/tmp/test-cdp-server")
            pool._processes["seed1"] = _live_process(seed="seed1", timezone="Europe/Berlin")
            with pytest.raises(aiohttp.web.HTTPConflict):
                await pool.get_or_launch("seed1", timezone="Asia/Tokyo")
            assert await pool.get_or_launch("seed1") is pool._processes["seed1"]

        asyncio.run(run())


class TestCapacity:
    def _make_pool(self, max_processes=2, default_seed=None):
        return ChromePool(
            binary="/fake/chrome", global_args=[], headless=True,
            data_dir="/tmp/test-cdp-server", max_processes=max_processes, default_seed=default_seed,
        )

    def test_evicts_least_recently_used_idle_seed(self):
        async def run():
            pool = self._make_pool()
            pool._processes["old"] = _live_process(seed="old", last_used=1.0)
            pool._processes["new"] = _live_process(seed="new", last_used=2.0)
            closed = []

            async def fake_cleanup(key):
                closed.append(key)
                pool._processes.pop(key)

            pool._cleanup_process = fake_cleanup
            await pool._make_room()
            assert closed == ["old"]

        asyncio.run(run())

    def test_busy_and_default_are_never_evicted(self):
        async def run():
            pool = self._make_pool(default_seed="home")
            pool._processes["home"] = _live_process(seed="home", last_used=0.0)
            pool._processes["busy"] = _live_process(seed="busy", last_used=1.0)
            pool._connections["busy"] = 1
            with pytest.raises(aiohttp.web.HTTPServiceUnavailable):
                await pool._make_room()

        asyncio.run(run())

    def test_default_identity_is_never_idle_reaped(self):
        async def run():
            pool = ChromePool(binary="/fake/chrome", global_args=[], headless=True,
                              data_dir="/tmp/test-cdp-server", idle_timeout=0.01)
            pool._processes["__default__"] = _live_process()
            pool.connect("__default__")
            pool.disconnect("__default__")
            assert pool._idle_tasks == {}

        asyncio.run(run())

    def test_settle_window_drops_start_maximized(self):
        pool = ChromePool(binary="/fake/chrome", global_args=["--start-maximized", "--no-sandbox"],
                          headless=False, settle_window=True)
        assert pool._global_args == ["--no-sandbox"]


# ---------------------------------------------------------------------------
# Window geometry and native input mapping
# ---------------------------------------------------------------------------


def _geometry(x, y, width, height):
    return dict(screenX=x, screenY=y, outerWidth=width, outerHeight=height,
                availLeft=0, availTop=0, availWidth=1920, availHeight=1032)


class TestWindowGeometry:
    def test_maximized_box_offset_past_edge_does_not_fit(self):
        # What the free binary reports for a maximized window
        assert not window_geometry_fits(_geometry(10, 47, 1920, 1032))

    def test_box_inside_available_screen_fits(self):
        assert window_geometry_fits(_geometry(424, 119, 1400, 900))

    def test_box_past_taskbar_does_not_fit(self):
        assert not window_geometry_fits(_geometry(114, 69, 1600, 1000))

    def test_viewport_origin_maximized(self):
        bounds = {"left": 0, "top": 0, "width": 1920, "height": 1080, "windowState": "maximized"}
        assert viewport_origin(bounds, 907) == (0, 173)

    def test_viewport_origin_retina(self):
        bounds = {"left": 64, "top": 102, "width": 1224, "height": 684, "windowState": "normal"}
        assert viewport_origin(bounds, 1118, device_scale_factor=2) == (136, 446)

    def test_viewport_origin_normal_window(self):
        bounds = {"left": 300, "top": 120, "width": 1200, "height": 800, "windowState": "normal"}
        assert viewport_origin(bounds, 623) == (304, 293)


class TestXdotoolCommands:
    def test_path_offsets_points_and_keeps_delays(self):
        steps = xdotool_commands({"type": "path", "points": [[10, 20, 0], [15.4, 22.6, 16]]}, (100, 200))
        assert steps == [(0.0, ["mousemove", "110", "220"]), (0.016, ["mousemove", "115", "223"])]

    def test_page_zoom_scales_css_coordinates(self):
        steps = xdotool_commands({"type": "move", "x": 100, "y": 40}, (217, 287), zoom=1.5)
        assert steps == [(0.0, ["mousemove", "367", "347"])]

    def test_click_moves_first_and_holds_the_button(self):
        steps = xdotool_commands({"type": "click", "x": 5, "y": 6}, (0, 173))
        assert [argv for _, argv in steps] == [["mousemove", "5", "179"], ["mousedown", "1"], ["mouseup", "1"]]
        assert steps[1][0] == 0.0
        assert 0.06 <= steps[2][0] <= 0.14

    def test_type_holds_each_key(self):
        steps = xdotool_commands({"type": "type", "text": "-A\n"}, (0, 0))
        assert [argv for _, argv in steps] == [
            ["keydown", "--", "0x2d"], ["keyup", "--", "0x2d"],
            ["keydown", "--", "0x41"], ["keyup", "--", "0x41"],
            ["keydown", "--", "Return"], ["keyup", "--", "Return"],
        ]
        delays = [delay for delay, _ in steps]
        assert delays[0] == 0.0 and delays[2] == delays[4] == 0.09
        assert all(0.05 <= delay <= 0.12 for delay in delays[1::2])

    def test_type_off_the_keymap_uses_xdotool_type(self):
        assert xdotool_commands({"type": "type", "text": "ñ", "delay_ms": 0}, (0, 0)) == [(0.0, ["type", "--", "ñ"])]

    def test_key_holds_the_combination(self):
        steps = xdotool_commands({"type": "key", "keys": "ctrl+0"}, (0, 0))
        assert [argv for _, argv in steps] == [["keydown", "--", "ctrl+0"], ["keyup", "--", "ctrl+0"]]
        assert 0.05 <= steps[1][0] <= 0.12

    def test_pointer_location_is_in_viewport_css_pixels(self):
        browser = SimpleNamespace(display=_mod.Display(100, []))

        async def fake_xdotool(argv, cp):
            assert argv == ["getmouselocation", "--shell"] and cp is browser
            return "X=1300\nY=844\nSCREEN=0\nWINDOW=123\n"
        with patch.object(_mod, "_run_xdotool", fake_xdotool):
            assert asyncio.run(_mod.pointer_location((0, 244), 2.0, browser)) == [650.0, 300.0]

    def test_unknown_action_fails(self):
        with pytest.raises(ValueError):
            xdotool_commands({"type": "teleport"}, (0, 0))


class TestPoolLifecycleMethods:
    """get_or_launch, the idle reaper and shutdown call these; a refactor once dropped them."""

    @pytest.mark.parametrize("name", ["_cleanup_process", "terminate_seed", "shutdown", "_wait_for_cdp",
                                      "_settle_window", "_make_room", "_abandon_launch"])
    def test_method_exists(self, name):
        assert callable(getattr(ChromePool, name))


# ---------------------------------------------------------------------------
# GET /history
# ---------------------------------------------------------------------------


class TestHistory:
    """The address bar's suggestions come from the default identity's profile, under
    the pool's key for it: the fingerprint seed when one is set."""

    @pytest.mark.parametrize("default_seed", [None, "39758"])
    def test_reads_the_default_identitys_history(self, tmp_path, default_seed):
        import sqlite3

        key = default_seed or "__default__"
        (tmp_path / key / "Default").mkdir(parents=True)
        database = sqlite3.connect(tmp_path / key / "Default" / "History")
        database.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, "
                         "typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER)")
        database.execute("INSERT INTO urls VALUES (1, 'https://github.com/notifications', 'Notifications', 1, 1, 1, 0)")
        database.commit()
        database.close()
        pool = ChromePool(binary="chrome", global_args=[], headless=True, data_dir=str(tmp_path),
                          default_seed=default_seed)
        request = SimpleNamespace(app={"pool": pool}, query={"q": "git"}, headers={"Host": "127.0.0.1:9222"})

        response = asyncio.run(_mod.handle_history(request))

        assert json.loads(response.text) == {"suggestions": [
            {"url": "https://github.com/notifications", "address": "github.com/notifications",
             "title": "Notifications"}]}

    @pytest.mark.parametrize("host", ["attacker.example:9222", "stealthbox:9222", "", "10.0.0.5:9222"])
    def test_answers_loopback_callers_only(self, host):
        request = SimpleNamespace(app={}, query={"q": "git"}, headers={"Host": host})
        assert asyncio.run(_mod.handle_history(request)).status == 403

    @pytest.mark.parametrize("host", ["127.0.0.1:9222", "localhost:9222", "[::1]:9222"])
    def test_loopback_hosts_are_allowed(self, host):
        assert _mod._host_is_loopback(host)


# ---------------------------------------------------------------------------
# Session browsers: one per Stealthbox session, each on its own display
# ---------------------------------------------------------------------------


class TestSessionBrowsers:
    def _pool(self, tmp_path):
        return ChromePool(binary="/fake/chrome", global_args=[], headless=False, data_dir=str(tmp_path))

    @staticmethod
    def _process(port, display=None):
        return SimpleNamespace(process=SimpleNamespace(poll=lambda: None), cdp_port=port, display=display)

    def test_session_json_routes_its_websockets_through_the_session(self, monkeypatch, tmp_path):
        TestJsonHandlers()._patch_session(monkeypatch, {
            "webSocketDebuggerUrl": "ws://127.0.0.1:5101/devtools/browser/browser-guid",
        })
        pool = self._pool(tmp_path)
        pool._processes["s_one-ab12"] = self._process(5101)
        request = SimpleNamespace(headers={"Host": "127.0.0.1:9222"}, scheme="http",
                                  app={"port": 9222, "pool": pool},
                                  match_info={"key": "s_one-ab12", "endpoint": "version"})

        payload = json.loads(asyncio.run(_mod.handle_session_json(request)).text)

        assert payload["webSocketDebuggerUrl"] == "ws://127.0.0.1:9222/sessions/s_one-ab12/devtools/browser/browser-guid"

    def test_a_session_that_is_not_open_is_not_found(self, tmp_path):
        request = SimpleNamespace(app={"pool": self._pool(tmp_path)}, match_info={"key": "s_gone"})
        with pytest.raises(aiohttp.web.HTTPNotFound):
            _mod._session_process(request)

    @pytest.mark.parametrize("key", ["12345", "__default__", "s_../x", "s_"])
    def test_only_session_keys_close_through_the_session_route(self, key, tmp_path):
        request = SimpleNamespace(app={"pool": self._pool(tmp_path)}, match_info={"key": key})
        assert asyncio.run(_mod.handle_session_close(request)).status == 400

    def test_sessions_opening_at_once_take_different_displays(self, tmp_path):
        pool = self._pool(tmp_path)
        assert [pool._take_display() for _ in range(3)] == [100, 101, 102]
        pool._displays.discard(101)
        assert pool._take_display() == 101
        assert pool._take_display() == 103

    def test_a_display_has_its_own_vnc_port(self):
        assert _mod.Display(99, []).vnc_port == 5900
        assert _mod.Display(101, []).vnc_port == 5902

    def test_the_profile_copy_leaves_out_caches_open_tabs_and_locks(self, tmp_path):
        source = tmp_path / "default"
        for name in ["Default/Cookies", "Default/Local Storage/leveldb/000003.log", "Default/Cache/Cache_Data/index",
                     "Default/Code Cache/js/index", "Default/Sessions/Session_1", "SingletonLock",
                     "Default/Service Worker/CacheStorage/x", "Default/Service Worker/Database/y", ".cdp-server-seed"]:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text("x")
        copy = tmp_path / "s_copy"

        _mod.shutil.copytree(source, copy, symlinks=True, ignore=_mod.PROFILE_COPY_SKIPS)

        kept = {str(path.relative_to(copy)) for path in copy.rglob("*") if path.is_file()}
        assert kept == {"Default/Cookies", "Default/Local Storage/leveldb/000003.log",
                        "Default/Service Worker/Database/y", ".cdp-server-seed"}

    def test_input_finds_the_browser_that_has_the_tab(self, monkeypatch, tmp_path):
        tabs = {5100: [{"id": "AAAA1111"}], 5101: [{"id": "BBBB2222"}]}

        class Response:
            def __init__(self, port):
                self.port = port

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return None

            async def json(self):
                return tabs[self.port]

        class Session:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return None

            def get(self, url):
                return Response(int(url.split(":")[2].split("/")[0]))

        monkeypatch.setattr(_mod.aiohttp, "ClientSession", Session)
        pool = self._pool(tmp_path)
        main, session = self._process(5100), self._process(5101, _mod.Display(100, []))
        pool._processes.update({"__default__": main, "s_one": session})

        assert asyncio.run(pool.process_for_target("BBBB2222")) is session
        with pytest.raises(aiohttp.web.HTTPNotFound):
            asyncio.run(pool.process_for_target("CCCC3333"))
