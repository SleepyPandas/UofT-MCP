"""MCP integration keeps authentication lazy and login operations nonblocking."""

import json

import httpx
import pytest
from mcp import Client
from test_auth import manager

from uoft_mcp.server import create_server


@pytest.mark.anyio
async def test_login_status_public_tool_and_forget_over_mcp(tmp_path):
    auth, browser = manager(tmp_path)
    browser.ready.clear()
    async with Client(
        create_server(
            httpx.MockTransport(lambda r: httpx.Response(200, json=["public"])),
            auth_factory=lambda: auth,
        )
    ) as client:
        initial = await client.call_tool("uoft_auth_status", {})
        assert not auth._opened
        assert json.loads(initial.content[0].text)["login"]["state"] == "idle"
        login = await client.call_tool("uoft_login", {"service": "both"})
        assert json.loads(login.content[0].text)["login"]["state"] == "in_progress"
        await browser.opened.wait()
        public = await client.call_tool("get_current_sessions", {})
        assert json.loads(public.content[0].text) == ["public"]
        progress = await client.call_tool("uoft_auth_status", {"refresh": True})
        assert json.loads(progress.content[0].text)["login"]["state"] == "in_progress"
        browser.ready.set()
        await auth.wait_for_login()
        connected = await client.call_tool("uoft_auth_status", {"refresh": True})
        result = json.loads(connected.content[0].text)
        assert all(s["state"] == "connected" for s in result["services"].values())
        assert "SECRET" not in connected.content[0].text
        forgotten = await client.call_tool("uoft_forget_session", {})
        assert json.loads(forgotten.content[0].text)["login"]["state"] == "idle"
        assert not auth.store.path.exists()
    assert browser.closed


@pytest.mark.anyio
async def test_shutdown_cancels_pending_browser_login(tmp_path):
    auth, browser = manager(tmp_path)
    browser.ready.clear()
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        await client.call_tool("uoft_login", {})
        await browser.opened.wait()
    assert browser.closed
    assert not auth.store._owned


@pytest.mark.anyio
async def test_invalid_service_rejected_without_authentication(tmp_path):
    auth, _ = manager(tmp_path)
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        result = await client.call_tool("uoft_login", {"service": "https://evil.test"})
        assert result.is_error
        assert not auth._opened


@pytest.mark.anyio
async def test_public_tools_do_not_open_authentication(tmp_path):
    auth, _ = manager(tmp_path)

    def forbidden():
        raise AssertionError("Authentication must stay lazy")

    auth.store.acquire = forbidden
    async with Client(
        create_server(
            httpx.MockTransport(lambda r: httpx.Response(200, json={})), auth_factory=lambda: auth
        )
    ) as client:
        assert not (await client.call_tool("get_reference_data", {})).is_error
        assert not (await client.call_tool("uoft_auth_status", {})).is_error
