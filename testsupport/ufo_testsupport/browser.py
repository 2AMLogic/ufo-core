"""Pinned Chrome for Testing support for live suites."""

import os
import sys
from pathlib import Path

from ufo.harness.sandbox.session import PLAYWRIGHT_CHROMIUM_REVISION, PLAYWRIGHT_VERSION

PLAYWRIGHT_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"
MISSING_BROWSER_REASON = (
    f"install Chrome for Testing with npx playwright@{PLAYWRIGHT_VERSION} install chromium"
)
_CACHE = {
    "darwin": "Library/Caches/ms-playwright",
    "linux": ".cache/ms-playwright",
}
_EXECUTABLE = {
    "darwin": "chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "linux": "chrome-linux*/chrome",
}
_SANDBOXED_EXECUTABLE = {
    "darwin": ("chromium_headless_shell", "chrome-headless-shell-mac*/chrome-headless-shell"),
    "linux": ("chromium", _EXECUTABLE["linux"]),
}
_HEADLESS_FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--use-mock-keychain",
    "--password-store=basic",
    "--no-first-run",
    "--no-default-browser-check",
)
_LINUX_FLAGS = ("--no-sandbox", "--disable-dev-shm-usage")


def chrome_for_testing() -> str | None:
    """Return the pinned Chrome for Testing executable when installed."""
    return _pinned("chromium", _EXECUTABLE[sys.platform])


def sandboxed_chrome() -> str | None:
    """Return the pinned Chrome a live suite launches inside the local carrier's kernel sandbox. On
    macOS that is the headless shell: the full browser registers with LaunchServices as it
    starts, which the Seatbelt profile keeps out of reach, and aborts."""
    return _pinned(*_SANDBOXED_EXECUTABLE[sys.platform])


def _pinned(browser: str, executable: str) -> str | None:
    cache = Path(os.environ.get(PLAYWRIGHT_PATH_ENV, Path.home() / _CACHE[sys.platform]))
    matches = sorted((cache / f"{browser}-{PLAYWRIGHT_CHROMIUM_REVISION}").glob(executable))
    return str(matches[0]) if matches else None


def headless_flags(platform: str) -> tuple[str, ...]:
    """Return headless flags safe for the host platform."""
    return _HEADLESS_FLAGS + (_LINUX_FLAGS if platform.startswith("linux") else ())
