"""Stealth configuration and platform detection for stealthbox_launcher."""

from __future__ import annotations
import hashlib
import os
import platform
import random
import re
from pathlib import Path

# The locale the binary presents with no --fingerprint-locale: navigator.languages
# ["en-US", "en"] and Accept-Language "en-US,en;q=0.9", consistent with each other.
BINARY_DEFAULT_LOCALE = "en-US"

# ---------------------------------------------------------------------------
# Default stealth arguments passed to the patched Chromium binary.
# These activate source-level fingerprint patches compiled into the binary.
# ---------------------------------------------------------------------------
def get_default_stealth_args() -> list[str]:
    """Build stealth args with a random fingerprint seed per launch.

    On macOS, skips platform/GPU spoofing — runs as a native Mac browser.
    Spoofing Windows on Mac creates detectable mismatches (fonts, GPU, etc.).
    """
    seed = random.randint(10000, 99999)
    system = platform.system()

    base = [
        "--no-sandbox",
        f"--fingerprint={seed}",
    ]

    if system == "Darwin":
        # Tell the fingerprint patches we're on macOS so GPU/UA match natively
        return base + ["--fingerprint-platform=macos"]

    # Linux/Windows: Windows fingerprint profile.
    # Screen and window size come from the real display, not this flag (verified:
    # identical across seeds), so the wrapper must not emulate a viewport on top in
    # headed mode — that would break outerWidth >= innerWidth coherence.
    return base + ["--fingerprint-platform=windows"]

# ---------------------------------------------------------------------------
# Hardware profiles — keep GPU, CPU threads, and memory coherent per persona.
# Free binaries pair every seed with 8 threads and 8 GB whatever GPU the seed
# lands on, which yields pairings no real machine has (an RTX 5090 next to 8
# threads, an M2 Pro with 8 threads where real ones have 10 or 12). Each profile
# below is a real pairing; the seed picks one deterministically, so the same
# seed is still the same machine. navigator.deviceMemory is capped at 8 by
# Chrome, so every profile reports 8.
#
# Windows: desktop NVIDIA only, so the binary's D3D11 WebGL parameter set keeps
# matching the renderer string. macOS: the chips of 13" MacBook Airs, whose
# default 1440x900 Retina screen is what the macOS persona reports.
# ---------------------------------------------------------------------------
HARDWARE_PROFILES: dict[str, tuple[str, tuple[tuple[str, int], ...]]] = {
    "windows": ("NVIDIA", (
        ("ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 (0x00001F82) Direct3D11 vs_5_0 ps_5_0, D3D11)", 8),
        ("ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER (0x000021C4) Direct3D11 vs_5_0 ps_5_0, D3D11)", 12),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 (0x00001F08) Direct3D11 vs_5_0 ps_5_0, D3D11)", 12),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)", 12),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)", 16),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti (0x00002489) Direct3D11 vs_5_0 ps_5_0, D3D11)", 16),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)", 16),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)", 12),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)", 16),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Ti (0x00002803) Direct3D11 vs_5_0 ps_5_0, D3D11)", 16),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)", 20),
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 SUPER (0x00002783) Direct3D11 vs_5_0 ps_5_0, D3D11)", 20),
    )),
    "macos": ("Apple", (
        ("ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)", 8),
        ("ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)", 8),
        ("ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)", 8),
        ("ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)", 10),
    )),
}

HARDWARE_PROFILE_FLAGS = (
    "--fingerprint-gpu-vendor",
    "--fingerprint-gpu-renderer",
    "--fingerprint-hardware-concurrency",
    "--fingerprint-device-memory",
)

def hardware_profile_args(platform_name: str, seed: str) -> list[str]:
    """Return coherent GPU/CPU/memory flags for a persona and seed."""
    vendor, profiles = HARDWARE_PROFILES[platform_name]
    digest = hashlib.sha256(seed.encode()).digest()
    renderer, threads = profiles[int.from_bytes(digest[:4], "big") % len(profiles)]
    return [
        f"--fingerprint-gpu-vendor=Google Inc. ({vendor})",
        f"--fingerprint-gpu-renderer={renderer}",
        f"--fingerprint-hardware-concurrency={threads}",
        "--fingerprint-device-memory=8",
    ]

_VERSION_PIN_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3,4}$")

def normalize_requested_version(version: str | None = None) -> str | None:
    """Return an explicit Chromium version pin from arg/env, or None.

    The explicit argument wins over STEALTHBOX_VERSION. Only numeric dotted
    versions are accepted because the value is interpolated into cache paths and
    download URLs.
    """
    raw = version if version is not None else os.environ.get("STEALTHBOX_VERSION")
    if raw is None:
        return None
    normalized = raw.strip()
    if not normalized:
        return None
    if not _VERSION_PIN_RE.fullmatch(normalized):
        raise ValueError(
            "Invalid browser version pin. Use a full numeric Chromium version, "
            "e.g. '148.0.7778.215.2'."
        )
    return normalized

# ---------------------------------------------------------------------------
# Binary cache paths
# ---------------------------------------------------------------------------
def get_cache_dir() -> Path:
    """Return the cache directory for downloaded binaries.

    Override with STEALTHBOX_CACHE_DIR env var.
    Default: ~/.stealthbox/
    """
    custom = os.environ.get("STEALTHBOX_CACHE_DIR")
    if custom:
        return Path(custom)
    return Path.home() / ".stealthbox"

def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse '145.0.7718.0' into (145, 0, 7718, 0) for comparison."""
    return tuple(int(x) for x in v.split("."))

def _version_newer(a: str, b: str) -> bool:
    """Return True if version a is strictly newer than version b."""
    return _version_tuple(a) > _version_tuple(b)

# ---------------------------------------------------------------------------
# Headless viewport handling by binary version
# ---------------------------------------------------------------------------
# First Chromium build that reports coherent headless dimensions without an
# emulated viewport. On these binaries the wrapper launches headless with
# no_viewport; older binaries need a fixed DEFAULT_VIEWPORT to stay coherent.
# None => not shipped yet; feature off, behavior byte-identical to today.
# TODO: set to the chromium version string that first ships it.
HEADLESS_NO_VIEWPORT_MIN_VERSION: str | None = "148.0.7778.215.4"

def binary_supports_headless_no_viewport(browser_version: str | None = None) -> bool:
    """Whether headless can launch with ``no_viewport`` on the binary.

    Only binaries at or above ``HEADLESS_NO_VIEWPORT_MIN_VERSION`` qualify. The
    binary is always a local one (``STEALTHBOX_BINARY_PATH``), so the version is
    the declared one (``browser_version`` or ``STEALTHBOX_VERSION``); with none
    declared it stays on the safe path.
    """
    if HEADLESS_NO_VIEWPORT_MIN_VERSION is None:
        return False
    try:
        version = normalize_requested_version(browser_version)
    except ValueError:
        return False
    if not version:
        return False
    try:
        return not _version_newer(HEADLESS_NO_VIEWPORT_MIN_VERSION, version)
    except (ValueError, AttributeError):
        return False

def binary_supports_maximized_window(browser_version: str | None = None) -> bool:
    """Whether the wrapper may auto-add ``--start-maximized``.

    Gated on the same threshold as the no_viewport shim: only binaries whose
    headless surface-fix + headed screen-clamp make a maximized window coherent
    (``outer == screen``). Below it, maximizing headless while the CDP viewport
    stays at 1280x720 yields ``outerWidth < innerWidth`` — an impossible-window
    bot tell — so the flag must NOT be added.
    """
    return binary_supports_headless_no_viewport(browser_version)
