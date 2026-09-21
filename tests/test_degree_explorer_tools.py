"""Exercise Degree Explorer discovery and calls through the official MCP SDK."""

import json
from functools import partial
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import Client
from test_auth import manager

from uoft_mcp.degree_explorer.client import BASE_URL
from uoft_mcp.server import create_server as _create_server
from uoft_mcp.utilities.auth_browser import PlaywrightSession

create_server = partial(_create_server, tool_profile="legacy")

ROUTES = {
    "academic_history": "/dxStudent/getAcademicHistory",
    "student_data": "/dxStudent/getStudentData",
    "student_record": "/dxStudent/getStudentRecord",
    "student_user_data": "/dxMenu/getStudentUserData",
    "student_menu": "/dxMenu/getStudentMenu",
    "messages": "/messages/getMessages",
    "session_timeouts": "/dxMenu/getSessionTimeouts",
    "planner": "/dxPlanner/getPlanner",
    "cell_details": "/dxPlanner/getCellDetails",
}


@pytest.mark.anyio
async def test_discovery_contract_and_all_routes(tmp_path):
    auth, backend = manager(tmp_path)
    session = PlaywrightSession()
    payload = [{"synthetic": {"nested": [None, True, "é"]}, "status": []}]
    response = Mock(
        status=200,
        headers={"content-type": "application/json"},
        text=AsyncMock(return_value=json.dumps(payload)),
        dispose=AsyncMock(),
    )
    session.api = Mock(get=AsyncMock(return_value=response))
    backend.read_degree_explorer = session.read_degree_explorer
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for suffix, path in ROUTES.items():
            name = "degree_explorer_get_" + suffix
            tool = tools[name]
            assert tool.input_schema["type"] == "object"
            assert tool.input_schema["properties"] == {}
            assert not tool.input_schema.get("required")
            assert tool.output_schema is None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is True
            assert "No arguments" in tool.description or "no arguments" in tool.description
            assert "uoft_login" in tool.description
            assert "JSON" in tool.description
            result = await client.call_tool(name, {})
            assert not result.is_error
            assert len(result.content) == 1
            assert result.content[0].type == "text"
            assert json.loads(result.content[0].text) == payload
            session.api.get.assert_awaited_once_with(
                BASE_URL + path,
                headers={"Accept": "application/json"},
                timeout=30_000,
                max_redirects=0,
            )
            response.dispose.assert_awaited_once()
            session.api.get.reset_mock()
        assert backend.visited == []
    assert backend.closed


@pytest.mark.anyio
async def test_auth_failure_is_mcp_tool_error_and_updates_status(tmp_path):
    auth, backend = manager(tmp_path)
    session = PlaywrightSession()
    response = Mock(
        status=302,
        headers={"location": "https://idpz.utorauth.utoronto.ca/?SAMLRequest=SECRET"},
        text=AsyncMock(return_value="SECRET"),
        dispose=AsyncMock(),
    )
    session.api = Mock(get=AsyncMock(return_value=response))
    backend.read_degree_explorer = session.read_degree_explorer
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        result = await client.call_tool("degree_explorer_get_academic_history", {})
        assert result.is_error
        assert "uoft_login" in result.content[0].text
        assert "SECRET" not in result.content[0].text
        status = await client.call_tool("uoft_auth_status", {})
        assert json.loads(status.content[0].text)["services"]["degree_explorer"]["state"] == (
            "login_required"
        )
        assert "SECRET" not in status.content[0].text
        assert backend.visited == []
