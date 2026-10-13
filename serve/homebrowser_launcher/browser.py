"""Core browser launch functions for homebrowser_launcher.

Provides launch() and launch_async() — thin wrappers around Playwright
that use our patched stealth Chromium binary instead of stock Chromium.

Usage:
    from homebrowser_launcher import launch

    browser = launch()
    page = browser.new_page()
    page.goto("https://protected-site.com")
    browser.close()
"""

from __future__ import annotations
import logging
import os
from typing import TypedDict
from urllib.parse import quote, unquote, urlparse, urlunparse
from .config import BINARY_DEFAULT_LOCALE, HARDWARE_PROFILE_FLAGS, HARDWARE_PROFILES, get_default_stealth_args, hardware_profile_args

logger = logging.getLogger("homebrowser_launcher")

class _ProxySettingsRequired(TypedDict):
    server: str

class ProxySettings(_ProxySettingsRequired, total=False):
    """Playwright-compatible proxy configuration."""

    bypass: str
    username: str
    password: str

def _ensure_proxy_scheme(proxy_url: str) -> str:
    """Prepend http:// to schemeless proxy URLs so parsers can extract hostname."""
    return proxy_url if "://" in proxy_url else f"http://{proxy_url}"

def _assemble_proxy_url(
    scheme: str,
    host: str,
    port: int | None,
    enc_user: str,
    enc_pass: str | None,
    path: str = "",
    params: str = "",
    query: str = "",
    fragment: str = "",
) -> str:
    """Build a proxy URL from already-percent-encoded credentials and host parts.

    ``enc_pass is None`` means no password (no colon in userinfo). Empty string
    means present-but-empty (colon preserved). This mirrors the distinction
    urlparse makes between ``user@host`` and ``user:@host``.
    """
    if ":" in host:  # IPv6 literal — re-add brackets
        host = f"[{host}]"
    if enc_pass is not None:
        userinfo = f"{enc_user}:{enc_pass}@"
    elif enc_user:
        userinfo = f"{enc_user}@"
    else:
        userinfo = ""
    netloc = f"{userinfo}{host}"
    if port is not None:
        netloc += f":{port}"
    return urlunparse((scheme, netloc, path, params, query, fragment))

def _reconstruct_socks_url(proxy: ProxySettings) -> str:
    """Reconstruct a SOCKS5 URL with inline credentials from a Playwright proxy dict."""
    server = proxy.get("server", "")
    username = proxy.get("username", "")
    password = proxy.get("password", "")
    if not username:
        return server
    parsed = urlparse(server)
    enc_user = quote(username, safe="")
    # Dict convention: empty/missing password → no colon.
    enc_pass = quote(password, safe="") if password else None
    return _assemble_proxy_url(
        parsed.scheme, parsed.hostname or "", parsed.port,
        enc_user, enc_pass, parsed.path,
    )

def _normalize_socks_string_url(url: str) -> str:
    """Re-encode credentials in a SOCKS5 URL string so Chromium's parser doesn't
    truncate them at special chars like '='. Idempotent: pre-encoded input stays
    the same (decoded then re-encoded).

    Emits an INFO log when re-encoding actually changes the URL, so users who
    previously hit silent SOCKS5 fallback (#157) can see what the wrapper did.
    Silent on already-encoded inputs (no false-positive noise).

    On unparseable input (invalid port, broken IPv6 literal, etc.) logs a
    warning and returns the original string — preserves pre-fix pass-through
    behavior so Chromium's own error handling kicks in.
    """
    try:
        parsed = urlparse(url)
        # Accessing .port raises ValueError on invalid port strings.
        _ = parsed.port
    except ValueError as e:
        logger.warning("Malformed SOCKS5 proxy URL, passing through unchanged: %s", e)
        return url
    # Skip only if no credentials at all (username AND password both absent).
    # urlparse returns None for absent components, "" for present-but-empty.
    if parsed.username is None and parsed.password is None:
        return url
    raw_user = parsed.username or ""
    enc_user = quote(unquote(raw_user), safe="") if raw_user else ""
    # Preserve the colon separator when password component is present, even if
    # empty, so `user:@host` stays `user:@host`.
    if parsed.password is not None:
        raw_pass = parsed.password
        enc_pass = quote(unquote(raw_pass), safe="") if raw_pass else ""
    else:
        raw_pass = None
        enc_pass = None
    normalized = _assemble_proxy_url(
        parsed.scheme, parsed.hostname or "", parsed.port,
        enc_user, enc_pass,
        parsed.path, parsed.params, parsed.query, parsed.fragment,
    )
    # Compare credentials, not the full URL: urlparse cosmetically lowercases
    # scheme and hostname, so a full-string compare would falsely fire on
    # `socks5://USER:pass@HOST.com:1080` even when no encoding work happened.
    if enc_user != raw_user or enc_pass != raw_pass:
        logger.info(
            "Auto URL-encoded SOCKS5 proxy credentials (special characters "
            "detected). Pre-encode the URL to suppress this notice."
        )
    return normalized

def _extract_proxy_url(proxy: str | ProxySettings | None) -> str | None:
    """Extract and normalize proxy URL string from proxy param.

    For proxy dicts with separate username/password fields, reconstructs
    the full URL with inline credentials so proxy auth works.
    """
    if proxy is None:
        return None
    if isinstance(proxy, dict):
        server = proxy.get("server", "")
        if not server:
            return None
        if proxy.get("username"):
            return (
                _reconstruct_socks_url(proxy) if _is_socks_proxy(proxy)
                else _reconstruct_http_url(proxy)
            )
        return _ensure_proxy_scheme(server)
    return _ensure_proxy_scheme(proxy)

def _get_flag_value(args: list[str] | None, *keys: str) -> str | None:
    """Return the value of the first ``--key=value`` flag found in *args*, else None."""
    for a in args or []:
        for k in keys:
            if a.startswith(k + "="):
                return a.split("=", 1)[1]
    return None

def maybe_resolve_geoip(
    geoip: bool,
    proxy: str | ProxySettings | None,
    timezone: str | None,
    locale: str | None,
    args: list[str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Auto-fill timezone/locale from the egress IP when geoip is enabled.

    Returns ``(timezone, locale, exit_ip)``.  *exit_ip* is a free bonus
    from the geoip lookup (no extra HTTP call) — used for WebRTC spoofing.

    With a proxy the egress IP is the proxy's exit IP; with no proxy it is
    the machine's own public IP, so geoip works proxy-free too.

    A timezone/locale set as a raw flag in *args* (``--fingerprint-timezone``,
    ``--lang``, ``--fingerprint-locale``) counts as explicit: it is promoted to
    the param so geoip leaves it alone, mirroring the ``timezone=``/``locale=``
    override path.
    """
    if not geoip:
        return timezone, locale, None

    # Promote raw flags to explicit params so geoip doesn't clobber them.
    if timezone is None:
        timezone = _get_flag_value(args, "--fingerprint-timezone")
    if locale is None:
        locale = _get_flag_value(args, "--lang", "--fingerprint-locale")

    from .geoip import resolve_proxy_exit_ip, resolve_proxy_geo_with_ip

    # None when no proxy → echo services resolve the machine's own public IP
    proxy_url = _extract_proxy_url(proxy) if proxy else None

    # When both tz/locale are explicit, resolve the exit IP for WebRTC — but only
    # with a proxy. With no proxy the WebRTC IP would just be the real connection
    # IP the site already sees (a no-op), so skip the third-party echo call.
    if timezone is not None and locale is not None:
        exit_ip = resolve_proxy_exit_ip(proxy_url) if proxy_url else None
        return timezone, locale, exit_ip

    geo_tz, geo_locale, exit_ip = resolve_proxy_geo_with_ip(proxy_url)
    if timezone is None:
        timezone = geo_tz
    if locale is None:
        locale = geo_locale
    missing = [name for name, value in (("timezone", timezone), ("locale", locale)) if value is None]
    if missing:
        raise RuntimeError(
            "GeoIP resolution failed: could not determine " + " and ".join(missing)
        )
    return timezone, locale, exit_ip

def _resolve_webrtc_args(
    args: list[str] | None,
    proxy: str | ProxySettings | None,
) -> list[str] | None:
    """Replace --fingerprint-webrtc-ip=auto with the resolved proxy exit IP.

    Returns args unchanged if no ``auto`` value is present.
    """
    if not args:
        return args
    idx = None
    for i, a in enumerate(args):
        if a == "--fingerprint-webrtc-ip=auto":
            idx = i
            break
    if idx is None:
        return args
    proxy_url = _extract_proxy_url(proxy)
    if not proxy_url:
        logger.warning("--fingerprint-webrtc-ip=auto requires a proxy; removing flag")
        args = list(args)
        del args[idx]
        return args
    try:
        from .geoip import resolve_proxy_exit_ip
        exit_ip = resolve_proxy_exit_ip(proxy_url)
    except Exception:
        logger.warning("Failed to resolve proxy exit IP for WebRTC spoofing; removing --fingerprint-webrtc-ip=auto")
        args = list(args)
        del args[idx]
        return args
    if exit_ip:
        args = list(args)
        args[idx] = f"--fingerprint-webrtc-ip={exit_ip}"
    else:
        logger.warning("Could not resolve proxy exit IP for WebRTC spoofing; removing --fingerprint-webrtc-ip=auto")
        args = list(args)
        del args[idx]
    return args

def build_args(
    stealth_args: bool,
    extra_args: list[str] | None,
    timezone: str | None = None,
    locale: str | None = None,
    headless: bool = True,
    extension_paths: list[str] | None = None,
    start_maximized: bool = False,
) -> list[str]:
    """Combine stealth args with user-provided args and locale flags.

    Deduplicates by flag key (everything before '=').
    Priority: stealth defaults < user args < dedicated params (timezone/locale).
    """
    seen: dict[str, str] = {}

    if stealth_args:
        for arg in get_default_stealth_args():
            seen[arg.split("=", 1)[0]] = arg

    # GPU blocklist bypass:
    # - Headed mode (all platforms): Chromium blocks WebGL on software GPUs
    #   in Docker/Xvfb. Flag lets SwiftShader serve WebGL. See issue #56.
    # - Windows (all modes): Chromium's GPU blocklist blocks WebGPU for the
    #   Microsoft Basic Render Driver. Dawn's adapter_blocklist bypass alone
    #   isn't enough — need this flag too. Linux doesn't need it.
    import platform as _platform
    if not headless or _platform.system() == "Windows":
        seen["--ignore-gpu-blocklist"] = "--ignore-gpu-blocklist"

    if extra_args:
        for arg in extra_args:
            key = arg.split("=", 1)[0]
            if key in seen:
                logger.debug("Arg override: %s -> %s", seen[key], arg)
            seen[key] = arg

    # Coherent GPU/CPU/memory for a spoofed persona, chosen by the final seed.
    # Linux hosts only: elsewhere the persona is the native platform and the real
    # GPU renders. Skipped entirely when the caller set any hardware flag, so a
    # partial override never gets mixed with a profile it does not belong to.
    persona = seen.get("--fingerprint-platform", "").split("=", 1)[-1]
    if (
        stealth_args
        and _platform.system() == "Linux"
        and persona in HARDWARE_PROFILES
        and "--fingerprint" in seen
        and not any(flag in seen for flag in HARDWARE_PROFILE_FLAGS)
    ):
        seed = seen["--fingerprint"].split("=", 1)[1]
        for arg in hardware_profile_args(persona, seed):
            seen[arg.split("=", 1)[0]] = arg

    # Playwright's default launch args switch off a browser feature that stock Chrome
    # ships enabled. Re-enable it alongside the Windows font-metrics profile so the
    # feature set matches a stock browser rather than a test harness. Merged into any
    # existing --enable-features value rather than added as a second flag.
    if "--fingerprint-windows-font-metrics" in seen:
        key = "--enable-features"
        current = seen[key].split("=", 1)[-1] if key in seen else ""
        features = [f for f in current.split(",") if f]
        if "MediaRouter" not in features:
            features.append("MediaRouter")
            seen[key] = f"{key}={','.join(features)}"

    # Timezone/locale flags are independent of stealth_args — always inject when set
    if timezone:
        key = "--fingerprint-timezone"
        flag = f"{key}={timezone}"
        if key in seen:
            logger.debug("Arg override: %s -> %s", seen[key], flag)
        seen[key] = flag
    if locale:
        # --fingerprint-locale pins navigator.languages to the one locale while
        # Accept-Language keeps the base language too (["en-US"] next to
        # "en-US,en;q=0.9"), a mismatch a page and its server can compare. The
        # binary's built-in locale is en-US with matching languages and header, so
        # en-US sets --lang only.
        keys = ("--lang",) if locale == BINARY_DEFAULT_LOCALE else ("--lang", "--fingerprint-locale")
        if locale == BINARY_DEFAULT_LOCALE:
            seen.pop("--fingerprint-locale", None)
        for key in keys:
            flag = f"{key}={locale}"
            if key in seen:
                logger.debug("Arg override: %s -> %s", seen[key], flag)
            seen[key] = flag

    if extension_paths:
        abs_paths = [os.path.abspath(p) for p in extension_paths]
        ext_val = ",".join(abs_paths)

        seen["--load-extension"] = f"--load-extension={ext_val}"
        seen["--disable-extensions-except"] = (
            f"--disable-extensions-except={ext_val}"
        )

    # Open maximized (real Windows Chrome overwhelmingly runs maximized) so the
    # window fills the spoofed screen. Skipped if the caller already chose a
    # window geometry. Gated to binaries where this stays coherent (see
    # binary_supports_maximized_window) — below the gate it would create
    # outerWidth < innerWidth.
    if start_maximized and not any(
        k in seen for k in ("--start-maximized", "--window-size", "--window-position")
    ):
        seen["--start-maximized"] = "--start-maximized"

    return list(seen.values())

def _reconstruct_http_url(proxy: ProxySettings) -> str:
    """Reconstruct an HTTP(S) proxy URL with inline credentials from a Playwright proxy dict."""
    server = proxy.get("server", "")
    username = proxy.get("username", "")
    password = proxy.get("password", "")
    if not username:
        return server
    parsed = urlparse(_ensure_proxy_scheme(server))
    enc_user = quote(username, safe="")
    enc_pass = quote(password, safe="") if password else None
    return _assemble_proxy_url(
        parsed.scheme, parsed.hostname or "", parsed.port,
        enc_user, enc_pass, parsed.path,
    )

def _is_socks_proxy(proxy: str | ProxySettings | None) -> bool:
    """Check if the proxy uses SOCKS5 protocol."""
    if proxy is None:
        return False
    url = proxy.get("server", "") if isinstance(proxy, dict) else proxy
    return url.lower().startswith(("socks5://", "socks5h://"))
