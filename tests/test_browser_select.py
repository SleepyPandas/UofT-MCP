"""Sign-in browser choice: installed Edge/Chrome first, bundled Chromium last. No real browser."""

import sys
from pathlib import Path

import pytest

from uoft_mcp.utilities.auth_browser import SERVICES, BrowserError, PlaywrightSession
from uoft_mcp.utilities.browser_select import (
    BROWSER_ENV,
    BrowserUnavailable,
    browser_order,
    launch_browser,
    requested_browser,
    system_browser_path,
)

SECRET_PATH = r"C:\Users\someone\SECRET\msedge.exe"


class FakeChromium:
    """Records launch calls; browsers named in ``broken`` raise like a missing install."""

    def __init__(self, broken=()):
        self.broken = set(broken)
        self.calls = []

    async def launch(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs.get("channel", "chromium")
        if name in self.broken:
            raise RuntimeError(f"{name} is not installed at {SECRET_PATH}")
        return {"browser": name, **kwargs}


class FakePlaywright:
    def __init__(self, broken=()):
        self.chromium = FakeChromium(broken)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(BROWSER_ENV, raising=False)


def system_first():
    return "msedge" if sys.platform == "win32" else "chrome"


def system_second():
    return "chrome" if sys.platform == "win32" else "msedge"


def test_auto_prefers_edge_on_windows_and_chrome_elsewhere():
    assert browser_order("auto", "win32") == ("msedge", "chrome", "chromium")
    assert browser_order("auto", "darwin") == ("chrome", "msedge", "chromium")
    assert browser_order("auto", "linux") == ("chrome", "msedge", "chromium")


@pytest.mark.parametrize("name", ["msedge", "chrome", "chromium"])
def test_explicit_choice_is_never_replaced(name):
    assert browser_order(name, "win32") == (name,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "auto"),
        ("", "auto"),
        ("  ", "auto"),
        ("AUTO", "auto"),
        (" MSEdge ", "msedge"),
        ("chrome", "chrome"),
        ("Chromium", "chromium"),
    ],
)
def test_requested_browser_reads_the_environment(value, expected):
    env = {} if value is None else {BROWSER_ENV: value}
    assert requested_browser(env) == expected


def test_invalid_override_is_rejected_without_echoing_it():
    with pytest.raises(BrowserUnavailable) as exc:
        requested_browser({BROWSER_ENV: "firefox-SECRET"})
    assert "SECRET" not in str(exc.value)
    assert BROWSER_ENV in str(exc.value)
    assert "auto, msedge, chrome, chromium" in str(exc.value)


@pytest.mark.anyio
async def test_auto_uses_the_first_installed_system_browser():
    playwright = FakePlaywright()
    browser = await launch_browser(playwright, headless=False)
    assert browser == {"browser": system_first(), "channel": system_first(), "headless": False}
    assert len(playwright.chromium.calls) == 1


@pytest.mark.anyio
async def test_auto_falls_through_failed_channels_to_bundled_chromium():
    playwright = FakePlaywright(broken={"msedge", "chrome"})
    browser = await launch_browser(playwright, headless=True)
    assert browser == {"browser": "chromium", "headless": True}
    assert playwright.chromium.calls == [
        {"channel": system_first(), "headless": True},
        {"channel": system_second(), "headless": True},
        {"headless": True},
    ]


@pytest.mark.anyio
async def test_auto_tries_the_second_system_browser_before_chromium():
    playwright = FakePlaywright(broken={system_first()})
    browser = await launch_browser(playwright, headless=True)
    assert browser["browser"] == system_second()
    assert "chromium" not in {call.get("channel", "chromium") for call in playwright.chromium.calls}


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["msedge", "chrome"])
async def test_override_selects_only_that_channel(monkeypatch, name):
    monkeypatch.setenv(BROWSER_ENV, name)
    playwright = FakePlaywright()
    await launch_browser(playwright, headless=False)
    assert playwright.chromium.calls == [{"channel": name, "headless": False}]


@pytest.mark.anyio
async def test_override_chromium_skips_system_browsers(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "chromium")
    playwright = FakePlaywright()
    await launch_browser(playwright, headless=False)
    assert playwright.chromium.calls == [{"headless": False}]


@pytest.mark.anyio
async def test_failed_override_does_not_fall_back(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "msedge")
    playwright = FakePlaywright(broken={"msedge"})
    with pytest.raises(BrowserUnavailable) as exc:
        await launch_browser(playwright, headless=False)
    assert len(playwright.chromium.calls) == 1
    assert "Microsoft Edge" in str(exc.value)
    assert BROWSER_ENV in str(exc.value)
    assert "SECRET" not in str(exc.value)


@pytest.mark.anyio
async def test_invalid_override_never_launches(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "safari")
    playwright = FakePlaywright()
    with pytest.raises(BrowserUnavailable):
        await launch_browser(playwright, headless=False)
    assert playwright.chromium.calls == []


@pytest.mark.anyio
async def test_all_failures_give_one_plain_instruction():
    playwright = FakePlaywright(broken={"msedge", "chrome", "chromium"})
    with pytest.raises(BrowserUnavailable) as exc:
        await launch_browser(playwright, headless=False)
    message = str(exc.value)
    assert "Microsoft Edge or Google Chrome" in message
    assert "uoft-mcp auth setup" in message
    assert "SECRET" not in message
    assert len(playwright.chromium.calls) == 3


@pytest.mark.anyio
async def test_interactive_login_reports_the_browser_message():
    session = PlaywrightSession()
    session.playwright = FakePlaywright(broken={"msedge", "chrome", "chromium"})
    session.api = object()
    with pytest.raises(BrowserError) as exc:
        await session.begin_browser({"cookies": [], "origins": []})
    assert "uoft-mcp auth setup" in str(exc.value)
    assert "SECRET" not in str(exc.value)


@pytest.mark.anyio
async def test_interactive_login_uses_a_fresh_non_persistent_context():
    class Page:
        async def evaluate(self, script):
            return "agent"

    class Context:
        request = object()

        async def new_page(self):
            return Page()

    class Browser:
        def __init__(self):
            self.context_options = None

        async def new_context(self, **options):
            self.context_options = options
            return Context()

    class Api:
        async def dispose(self):
            pass

    class Chromium(FakeChromium):
        async def launch(self, **kwargs):
            await super().launch(**kwargs)
            self.browser = Browser()
            return self.browser

    playwright = FakePlaywright()
    playwright.chromium = Chromium()
    session = PlaywrightSession()
    session.playwright = playwright
    session.api = Api()
    state = {"cookies": [{"name": "kept", "value": "SECRET"}], "origins": []}
    await session.begin_browser(state)
    assert playwright.chromium.calls == [{"channel": system_first(), "headless": False}]
    assert not hasattr(playwright.chromium, "launch_persistent_context")
    assert playwright.chromium.browser.context_options == {
        "storage_state": {"cookies": state["cookies"], "origins": []},
        "accept_downloads": False,
    }


@pytest.mark.anyio
async def test_quiet_renewal_uses_the_same_selection(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "chrome")

    class Api:
        async def storage_state(self):
            return {"cookies": [], "origins": []}

    playwright = FakePlaywright()
    session = PlaywrightSession()
    session.playwright = playwright
    session.api = Api()
    result = await session.silent_refresh(SERVICES["acorn"])
    assert result.state == "unavailable"  # The fake browser cannot open pages.
    assert playwright.chromium.calls == [{"channel": "chrome", "headless": True}]


def fake_exists(*present):
    wanted = {str(Path(path)) for path in present}
    return lambda path: str(path) in wanted


def test_windows_detection_checks_each_install_root_in_order():
    env = {
        "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
        "PROGRAMFILES": r"C:\Program Files",
        "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
        "HOMEDRIVE": "D:",
    }
    edge = Path(r"C:\Program Files (x86)") / r"Microsoft\Edge\Application\msedge.exe"
    found = system_browser_path("msedge", "win32", env, fake_exists(edge))
    assert found == edge
    assert system_browser_path("chrome", "win32", env, fake_exists(edge)) is None
    hand_installed = Path("D:\\Program Files") / r"Google\Chrome\Application\chrome.exe"
    assert system_browser_path("chrome", "win32", env, fake_exists(hand_installed)) == (
        hand_installed
    )


def test_windows_detection_ignores_unset_roots():
    assert system_browser_path("msedge", "win32", {}, lambda path: True) is None


@pytest.mark.parametrize(
    ("name", "platform", "location"),
    [
        ("chrome", "darwin", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("msedge", "darwin", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("chrome", "linux", "/opt/google/chrome/chrome"),
        ("msedge", "linux", "/opt/microsoft/msedge/msedge"),
    ],
)
def test_posix_detection_uses_the_fixed_install_location(name, platform, location):
    assert system_browser_path(name, platform, {}, fake_exists(location)) == Path(location)
    assert system_browser_path(name, platform, {}, lambda path: False) is None


def test_unsupported_platform_has_no_system_browser():
    assert system_browser_path("chrome", "freebsd", {}, lambda path: True) is None
