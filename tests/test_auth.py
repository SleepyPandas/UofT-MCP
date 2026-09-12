"""Exercise auth lifecycles offline without credentials, a browser, or real keyring."""

import asyncio
import json
from copy import deepcopy

import pytest

from uoft_mcp.auth import AuthManager
from uoft_mcp.auth_browser import SERVICES, BrowserClosed, ProbeResult, classify_response
from uoft_mcp.auth_store import EMPTY_STATE, SessionStore


class Keys:
    value = None

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def delete(self):
        self.value = None


class FakeBrowser:
    def __init__(self):
        self.state = deepcopy(EMPTY_STATE)
        self.ready = asyncio.Event()
        self.opened = asyncio.Event()
        self.ready.set()
        self.visited = []
        self.browser_open = False
        self.closed = False
        self.close_window = False
        self.fail_service = None
        self.expired = set()
        self.rotations = 0

    async def start(self, state):
        self.state = deepcopy(state)

    async def probe(self, service):
        if self.fail_service == service.name:
            return ProbeResult("unavailable", "Service unavailable.")
        cookies = self.state["cookies"]
        if service.name not in self.expired and any(c["name"] == service.name for c in cookies):
            self.rotations += 1
            for cookie in cookies:
                if cookie["name"] == service.name:
                    cookie["value"] = f"SECRET-{self.rotations}"
            return ProbeResult("connected", "Verified.")
        return ProbeResult("login_required", "Sign in.")

    async def snapshot(self):
        state = deepcopy(self.state)
        if not self.browser_open:
            state["origins"] = []  # An API context cannot track live browser storage.
        return state

    async def begin_browser(self, state):
        self.state = deepcopy(state)
        self.browser_open = True
        self.opened.set()

    async def navigate(self, service):
        self.visited.append(service.name)

    async def on_service(self, service):
        if self.close_window:
            raise BrowserClosed("Login window closed. Run uoft_login to try again.")
        if not self.ready.is_set():
            return False
        self.expired.discard(service.name)
        self.state["cookies"].append({"name": service.name, "value": "SECRET"})
        self.state["origins"] = [
            {"origin": service.app_url, "localStorage": [{"name": "auth", "value": "LOCAL_SECRET"}]}
        ]
        return True

    async def finish_browser(self, state):
        self.browser_open = False
        self.state = deepcopy(state)

    async def close(self):
        self.closed = True
        self.browser_open = False


def manager(tmp_path, browser=None, keys=None, **kwargs):
    browser = browser or FakeBrowser()
    return AuthManager(
        store=SessionStore(tmp_path, keys or Keys()),
        backend_factory=lambda: browser,
        poll_interval=0.001,
        **kwargs,
    ), browser


@pytest.mark.anyio
async def test_login_close_browser_and_restore_after_mcp_restart(tmp_path):
    keys = Keys()
    first, browser = manager(tmp_path, keys=keys)
    initial = await first.login()
    assert initial["login"]["state"] == "in_progress"
    result = await first.wait_for_login()
    assert result["login"]["state"] == "complete"
    assert browser.visited == ["degree_explorer", "acorn"]
    assert not browser.browser_open
    assert all(s["state"] == "connected" for s in (await first.refresh())["services"].values())
    saved_origins = deepcopy(first._state["origins"])
    assert saved_origins
    await first.close()

    second, restored = manager(tmp_path, keys=keys)
    await second.login()
    result = await second.wait_for_login()
    assert result["login"]["state"] == "complete"
    assert restored.visited == []
    assert second._state["origins"] == saved_origins
    assert "SECRET" not in json.dumps(result)
    await second.close()


@pytest.mark.anyio
async def test_nonblocking_login_dedup_and_public_status_while_waiting(tmp_path):
    auth, browser = manager(tmp_path)
    browser.ready.clear()
    await auth.login("degree_explorer")
    await browser.opened.wait()
    original = auth._task
    assert (await auth.login("acorn"))["login"]["services"] == ["degree_explorer"]
    assert auth._task is original
    assert (await auth.refresh())["login"]["state"] == "in_progress"
    browser.ready.set()
    assert (await auth.wait_for_login())["login"]["state"] == "complete"
    await auth.close()


@pytest.mark.anyio
async def test_partial_success_and_no_browser_for_outage(tmp_path):
    auth, browser = manager(tmp_path)
    browser.fail_service = "acorn"
    await auth.login()
    result = await auth.wait_for_login()
    assert result["login"]["state"] == "partial"
    assert result["services"]["degree_explorer"]["state"] == "connected"
    assert result["services"]["acorn"]["state"] == "unavailable"
    assert browser.visited == ["degree_explorer"]
    await auth.close()


@pytest.mark.anyio
async def test_expired_session_requires_explicit_login(tmp_path):
    auth, browser = manager(tmp_path)
    await auth.login()
    await auth.wait_for_login()
    browser.expired.add("degree_explorer")
    result = await auth.refresh()
    assert result["services"]["degree_explorer"]["state"] == "login_required"
    assert result["services"]["acorn"]["state"] == "connected"
    assert not browser.browser_open
    await auth.close()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["timeout", "window_close", "forget", "shutdown"])
async def test_interrupted_login_cleans_up(tmp_path, action):
    auth, browser = manager(tmp_path, login_timeout=0.03)
    browser.ready.clear()
    browser.close_window = action == "window_close"
    await auth.login()
    await browser.opened.wait()
    if action == "forget":
        assert (await auth.forget())["login"]["state"] == "idle"
        assert not auth.store.path.exists()
    elif action == "shutdown":
        await auth.close()
    else:
        result = await auth.wait_for_login()
        assert result["login"]["state"] == ("timed_out" if action == "timeout" else "cancelled")
    assert browser.closed
    assert not browser.browser_open
    await auth.close()
    other = SessionStore(tmp_path, Keys())
    other.acquire()
    other.close()


@pytest.mark.anyio
async def test_second_client_lock_conflict_does_not_clear_first_session(tmp_path):
    keys = Keys()
    first, browser = manager(tmp_path, keys=keys)
    await first.login()
    await first.wait_for_login()
    second, _ = manager(tmp_path, keys=keys)
    await second.login()
    assert "in use" in (await second.wait_for_login())["login"]["message"]
    assert "in use" in (await second.forget())["login"]["message"]
    assert first.store.path.exists()
    assert not browser.closed
    await second.close()
    await first.close()


@pytest.mark.anyio
async def test_memory_only_login_is_not_saved(tmp_path):
    auth, _ = manager(tmp_path)
    await auth.login(remember=False)
    result = await auth.wait_for_login()
    assert result["login"]["state"] == "complete"
    assert not result["persistence"]["enabled"]
    assert not auth.store.path.exists()
    await auth.close()


@pytest.mark.anyio
async def test_unexpected_backend_error_is_sanitized(tmp_path):
    auth, browser = manager(tmp_path)

    async def fail(state):
        raise RuntimeError("SECRET SAMLResponse and student payload")

    browser.begin_browser = fail
    await auth.login()
    result = await auth.wait_for_login()
    assert result["login"]["state"] == "failed"
    assert "SECRET" not in json.dumps(result)
    assert browser.closed
    await auth.close()


@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (200, {"content-type": "application/json"}, "{}", "connected"),
        (200, {"content-type": "application/json"}, "[]", "unexpected_response"),
        (200, {"content-type": "application/json"}, "{bad", "unexpected_response"),
        (200, {"content-type": "text/html"}, '<input name="j_password">', "login_required"),
        (200, {"content-type": "text/html"}, "maintenance", "unexpected_response"),
        (302, {"location": "https://idpz.utorauth.utoronto.ca/?SECRET"}, "", "login_required"),
        (302, {"location": "https://evil.test/?SECRET"}, "", "unexpected_response"),
        (401, {}, "SECRET", "login_required"),
        (403, {}, "SECRET", "access_denied"),
        (500, {}, "SECRET", "unavailable"),
        (429, {}, "SECRET", "unavailable"),
    ],
)
def test_response_classification_without_payload_disclosure(status, headers, body, expected):
    result = classify_response(SERVICES["degree_explorer"], status, headers, body)
    assert result.state == expected
    assert "SECRET" not in result.message


def test_empty_acorn_array_is_authenticated():
    assert (
        classify_response(SERVICES["acorn"], 200, {"content-type": "application/json"}, "[]").state
        == "connected"
    )
