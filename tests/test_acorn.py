"""Read boundaries and session reuse, using synthetic payloads and no live account."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from test_auth import Keys, manager

from uoft_mcp.acorn.client import BASE_URL, AcornEndpoint, AcornError
from uoft_mcp.utilities.auth_browser import PlaywrightSession


@pytest.mark.anyio
@pytest.mark.parametrize("endpoint", list(AcornEndpoint))
@pytest.mark.parametrize("empty", [False, True])
async def test_one_allowlisted_get_preserves_json_and_disposes(endpoint, empty):
    payload = {} if empty else {"nested": [None, True, "synthetic \u00e9"]}
    if endpoint is AcornEndpoint.ELIGIBLE_REGISTRATIONS:
        payload = [] if empty else [payload]
    session = PlaywrightSession()
    response = Mock(
        status=200,
        headers={"content-type": "application/json"},
        text=AsyncMock(return_value=json.dumps(payload)),
        dispose=AsyncMock(),
    )
    session.api = Mock(get=AsyncMock(return_value=response))
    assert await session.read_acorn(endpoint) == payload
    session.api.get.assert_awaited_once_with(
        BASE_URL + endpoint,
        headers={"Accept": "application/json"},
        timeout=30_000,
        max_redirects=0,
    )
    response.dispose.assert_awaited_once()


@pytest.mark.anyio
async def test_read_parses_large_payload_only_once():
    payload = {"courses": [{"id": index, "marks": [75, 80, 90]} for index in range(1000)]}
    body = json.dumps(payload)
    response = Mock(
        status=200,
        headers={"content-type": "application/json"},
        text=AsyncMock(return_value=body),
        dispose=AsyncMock(),
    )
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(return_value=response))
    with patch("uoft_mcp.utilities.auth_browser.json.loads", wraps=json.loads) as parse:
        assert await session.read_acorn(AcornEndpoint.STUDENT_REGISTRATION_INFO) == payload
        parse.assert_called_once_with(body)
    response.dispose.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,headers,body,state",
    [
        (401, {}, "SECRET", "login_required"),
        (302, {"location": "https://idpz.utorauth.utoronto.ca/?SECRET"}, "", "login_required"),
        (302, {"location": "https://evil.test/?SECRET"}, "", "unexpected_response"),
        (200, {"content-type": "text/html"}, "j_password SECRET", "login_required"),
        (403, {}, "SECRET", "access_denied"),
        (429, {}, "SECRET", "unavailable"),
        (503, {}, "SECRET", "unavailable"),
        (404, {}, "SECRET", "unexpected_response"),
        (200, {"content-type": "text/html"}, "SECRET", "unexpected_response"),
        (200, {"content-type": "application/json"}, "SECRET", "unexpected_response"),
    ],
)
async def test_failures_are_sanitized_and_disposed(status, headers, body, state):
    session = PlaywrightSession()
    response = Mock(
        status=status, headers=headers, text=AsyncMock(return_value=body), dispose=AsyncMock()
    )
    session.api = Mock(get=AsyncMock(return_value=response))
    with pytest.raises(AcornError) as error:
        await session.read_acorn(AcornEndpoint.STUDENT_REGISTRATION_INFO)
    assert error.value.state == state
    assert "SECRET" not in str(error.value)
    assert "/profile/studentRegistrationInfo" in str(error.value)
    response.dispose.assert_awaited_once()
    assert session.api.get.await_count == 1


@pytest.mark.anyio
async def test_transport_failure_and_arbitrary_route():
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(side_effect=RuntimeError("SECRET")))
    with pytest.raises(AcornError, match="failed or timed out") as error:
        await session.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
    assert "SECRET" not in str(error.value)
    session.api.get.reset_mock()
    for path in ["https://evil.test", "/dxPlanner/savePlanner", "/dxPlanner/getPlanner"]:
        with pytest.raises(AcornError, match="Unsupported"):
            await session.read_acorn(path)
    session.api.get.assert_not_called()


@pytest.mark.anyio
async def test_cancellation_disposes_response():
    session = PlaywrightSession()
    response = Mock(text=AsyncMock(side_effect=asyncio.CancelledError), dispose=AsyncMock())
    session.api = Mock(get=AsyncMock(return_value=response))
    with pytest.raises(asyncio.CancelledError):
        await session.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
    response.dispose.assert_awaited_once()


@pytest.mark.anyio
async def test_read_restores_session_checkpoints_cookies_without_records(tmp_path):
    keys = Keys()
    first, _ = manager(tmp_path, keys=keys)
    await first.login("acorn")
    await first.wait_for_login()
    await first.close()
    auth, browser = manager(tmp_path, keys=keys)

    async def read(endpoint):
        assert endpoint is AcornEndpoint.STUDENT_REGISTRATION_INFO
        assert browser.state["cookies"]
        browser.state["cookies"][0]["value"] = "ROTATED_SECRET"
        return {"synthetic_student_record": [42]}

    browser.read_acorn = AsyncMock(side_effect=read)
    try:
        assert await auth.read_acorn(AcornEndpoint.STUDENT_REGISTRATION_INFO) == {
            "synthetic_student_record": [42]
        }
        assert browser.visited == []
        assert not browser.browser_open
        saved = auth.store.load(True)
        assert saved["cookies"][0]["value"] == "ROTATED_SECRET"
        assert "synthetic_student_record" not in json.dumps(saved)
        status = auth.status()
        assert status["services"]["acorn"]["state"] == "connected"
        assert status["services"]["degree_explorer"]["state"] == "not_checked"
        assert "SECRET" not in json.dumps(status)
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_read_failure_updates_status_without_opening_browser(tmp_path):
    auth, browser = manager(tmp_path)
    browser.read_acorn = AsyncMock(
        side_effect=AcornError("Sign in with uoft_login.", "login_required")
    )
    try:
        with pytest.raises(AcornError, match="uoft_login"):
            await auth.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
        assert auth.status()["services"]["acorn"]["state"] == "login_required"
        assert browser.visited == []
        assert not browser.browser_open
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_read_during_login_returns_immediately(tmp_path):
    auth, browser = manager(tmp_path)
    browser.ready.clear()
    browser.read_acorn = AsyncMock()
    await auth.login("acorn")
    await browser.opened.wait()
    try:
        async with asyncio.timeout(1):
            with pytest.raises(AcornError, match="login is in progress"):
                await auth.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
        browser.read_acorn.assert_not_called()
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_forget_waits_for_active_read(tmp_path):
    auth, browser = manager(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def read(endpoint):
        started.set()
        await finish.wait()
        assert not browser.closed
        return []

    browser.read_acorn = read
    task = asyncio.create_task(auth.read_acorn(AcornEndpoint.DASHBOARD_COURSES))
    await started.wait()
    forget = asyncio.create_task(auth.forget())
    await asyncio.sleep(0)
    assert not forget.done()
    finish.set()
    assert await task == []
    await forget
    assert browser.closed
    assert not auth.store.path.exists()
    await auth.close()


@pytest.mark.anyio
@pytest.mark.parametrize("endpoint", list(AcornEndpoint))
@pytest.mark.parametrize("payload", [None, True, 42, "SECRET", [], {}])
async def test_unexpected_json_root_is_rejected(endpoint, payload):
    if type(payload) is endpoint.json_type:
        return
    response = Mock(
        status=200,
        headers={"content-type": "application/json"},
        text=AsyncMock(return_value=json.dumps(payload)),
        dispose=AsyncMock(),
    )
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(return_value=response))
    with pytest.raises(AcornError) as error:
        await session.read_acorn(endpoint)
    assert error.value.state == "unexpected_response"
    assert "SECRET" not in str(error.value)
    response.dispose.assert_awaited_once()


@pytest.mark.anyio
async def test_timeout_is_sanitized():
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(side_effect=TimeoutError("SECRET")))
    with pytest.raises(AcornError, match="30-second timeout") as error:
        await session.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
    assert error.value.state == "unavailable"
    assert "SECRET" not in str(error.value)
