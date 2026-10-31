"""Choose the browser for UofT sign-in: installed Edge or Chrome first, bundled Chromium last.

Every launch uses ``chromium.launch()``, never ``launch_persistent_context()``, so
Playwright gives the browser a fresh temporary user data directory. A system browser is
used only as an executable; its profile, cookies, extensions, and history are not opened.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Never, get_args

BrowserName = Literal["msedge", "chrome", "chromium"]
BrowserChoice = Literal["auto", "msedge", "chrome", "chromium"]

BROWSER_ENV = "UOFT_MCP_BROWSER"
BROWSER_CHOICES: tuple[str, ...] = get_args(BrowserChoice)

# Playwright resolves channels from these fixed locations; mirroring them keeps detection
# cheap (no browser launch, no Playwright driver) and consistent with what launch will find.
_SYSTEM_BROWSER_PATHS: Mapping[str, Mapping[str, str]] = {
    "msedge": {
        "win32": "Microsoft\\Edge\\Application\\msedge.exe",
        "darwin": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "linux": "/opt/microsoft/msedge/msedge",
    },
    "chrome": {
        "win32": "Google\\Chrome\\Application\\chrome.exe",
        "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "linux": "/opt/google/chrome/chrome",
    },
}
_WINDOWS_ROOT_VARIABLES = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")


class BrowserUnavailable(Exception):
    """Fixed plain-language message; never carries launch errors, paths, or URLs."""


def requested_browser(env: Mapping[str, str] | None = None) -> BrowserChoice:
    """Read ``UOFT_MCP_BROWSER``; an unset or blank value means ``auto``."""
    value = (os.environ if env is None else env).get(BROWSER_ENV, "").strip().lower()
    if not value:
        return "auto"
    match value:
        case "auto" | "msedge" | "chrome" | "chromium":
            return value
        case _:
            raise BrowserUnavailable(
                f"{BROWSER_ENV} must be one of: {', '.join(BROWSER_CHOICES)}. "
                f"Fix or unset it, then try again."
            )


def browser_order(choice: BrowserChoice, platform: str | None = None) -> tuple[BrowserName, ...]:
    """Browsers to try, best first. An explicit choice is never silently replaced."""
    match choice:
        case "auto":
            if (platform or sys.platform) == "win32":
                return ("msedge", "chrome", "chromium")
            return ("chrome", "msedge", "chromium")
        case "msedge" | "chrome" | "chromium":
            return (choice,)
        case _:
            unused: Never = choice
            raise ValueError(f"Unhandled browser choice: {unused}")


def browser_label(name: BrowserName) -> str:
    match name:
        case "msedge":
            return "Microsoft Edge"
        case "chrome":
            return "Google Chrome"
        case "chromium":
            return "Chromium"
        case _:
            unused: Never = name
            raise ValueError(f"Unhandled browser: {unused}")


def system_browser_path(
    name: Literal["msedge", "chrome"],
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    exists: Callable[[Path], bool] = Path.is_file,
) -> Path | None:
    """Where an installed Edge or Chrome lives, or None. Only looks at the file system."""
    platform = platform or sys.platform
    suffix = _SYSTEM_BROWSER_PATHS[name].get(platform)
    if suffix is None:
        return None
    if platform != "win32":
        path = Path(suffix)
        return path if exists(path) else None
    env = os.environ if env is None else env
    drive = env.get("HOMEDRIVE")
    roots = [env.get(variable) for variable in _WINDOWS_ROOT_VARIABLES]
    if drive:
        roots += [drive + "\\Program Files", drive + "\\Program Files (x86)"]
    for root in roots:
        if root and exists(Path(root) / suffix):
            return Path(root) / suffix
    return None


def detect_system_browser(
    choice: BrowserChoice,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    exists: Callable[[Path], bool] = Path.is_file,
) -> Literal["msedge", "chrome"] | None:
    """The installed Edge or Chrome that sign-in would pick for this choice, if any."""
    for name in browser_order(choice, platform):
        match name:
            case "msedge" | "chrome":
                if system_browser_path(name, platform, env, exists) is not None:
                    return name
            case "chromium":
                pass
            case _:
                unused: Never = name
                raise ValueError(f"Unhandled browser: {unused}")
    return None


def no_browser_message(choice: BrowserChoice) -> str:
    match choice:
        case "auto":
            return (
                "No browser is available for UofT sign-in. Install Microsoft Edge or "
                "Google Chrome, or run: uoft-mcp auth setup. Sign-in also needs a desktop session."
            )
        case "msedge" | "chrome":
            return (
                f"{browser_label(choice)} is not available, and {BROWSER_ENV} is set to it. "
                f"Install it, or unset {BROWSER_ENV} to use another browser."
            )
        case "chromium":
            return (
                f"Chromium is not available, and {BROWSER_ENV} is set to it. "
                "Run: uoft-mcp auth setup"
            )
        case _:
            unused: Never = choice
            raise ValueError(f"Unhandled browser choice: {unused}")


async def _launch(playwright: Any, name: BrowserName, headless: bool) -> Any:
    match name:
        case "msedge" | "chrome":
            return await playwright.chromium.launch(channel=name, headless=headless)
        case "chromium":
            return await playwright.chromium.launch(headless=headless)
        case _:
            unused: Never = name
            raise ValueError(f"Unhandled browser: {unused}")


async def launch_browser(playwright: Any, *, headless: bool) -> Any:
    """Launch the first working browser for sign-in or quiet renewal.

    Raises ``BrowserUnavailable`` with a fixed message when nothing can be started.
    Individual launch errors are discarded because they can contain local paths.
    """
    choice = requested_browser()
    for name in browser_order(choice):
        try:
            return await _launch(playwright, name, headless)
        except Exception:
            continue
    raise BrowserUnavailable(no_browser_message(choice))
