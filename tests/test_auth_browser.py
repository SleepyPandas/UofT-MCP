"""Verify fixed Playwright request boundaries without contacting UofT."""

from unittest.mock import AsyncMock, Mock

import pytest

from uoft_mcp.utilities.auth_browser import SERVICES, PlaywrightSession


@pytest.mark.anyio
async def test_probe_does_not_follow_redirects_or_retain_responses():
    session = PlaywrightSession()
    response = Mock(
        status=302,
        headers={"location": "https://idpz.utorauth.utoronto.ca/?SAMLRequest=SECRET"},
        text=AsyncMock(return_value=""),
        dispose=AsyncMock(),
    )
    session.api = Mock(get=AsyncMock(return_value=response))
    result = await session.probe(SERVICES["acorn"])
    assert result.state == "login_required"
    session.api.get.assert_awaited_once_with(
        SERVICES["acorn"].probe_url,
        headers={"Accept": "application/json"},
        timeout=15_000,
        max_redirects=0,
    )
    response.dispose.assert_awaited_once()
    assert "SECRET" not in result.message


@pytest.mark.anyio
async def test_network_error_is_sanitized():
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(side_effect=RuntimeError("SECRET redirect and headers")))
    result = await session.probe(SERVICES["acorn"])
    assert result.state == "unavailable"
    assert "SECRET" not in result.message


@pytest.mark.anyio
async def test_browser_close_restores_api_cookies_and_matching_user_agent():
    session = PlaywrightSession()
    state = {
        "cookies": [{"name": "session", "value": "SECRET"}],
        "origins": [],
        "user_agent": "Browser agent",
    }
    session.user_agent = "Browser agent"
    session.context = Mock(close=AsyncMock())
    session.browser = Mock(close=AsyncMock())
    request = Mock(new_context=AsyncMock())
    session.playwright = Mock(request=request)
    await session.finish_browser(state)
    request.new_context.assert_awaited_once_with(
        storage_state={"cookies": state["cookies"], "origins": []}, user_agent="Browser agent"
    )
    assert session.browser is None
    assert session.context is None
