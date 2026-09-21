"""Acorn discovery, routing, and compact results through the MCP SDK."""

import json
from functools import partial
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import Client
from test_auth import manager

from uoft_mcp.acorn.client import BASE_URL, AcornError
from uoft_mcp.acorn.selection import select_fields
from uoft_mcp.server import create_server as _create_server
from uoft_mcp.utilities.auth_browser import PlaywrightSession

create_server = partial(_create_server, tool_profile="legacy")

ROUTES = {
    "acorn_get_eligible_registrations": (
        "/enrolment/eligible-registrations",
        [{"sessionDescription": "Synthetic session", "registrationParams": {"x": [None, True]}}],
        "sessionDescription",
    ),
    "acorn_get_dashboard_courses": (
        "/dashboard/courseRegistration/enrolledCourses",
        {"enrolledCourses": [{"code": "SYN101"}], "sessionDescription": "Synthetic session"},
        "enrolledCourses",
    ),
    "acorn_get_student_registration_info": (
        "/profile/studentRegistrationInfo",
        {"personId": "SYNTHETIC_PRIVATE", "studentHasFutureExams": False},
        "studentHasFutureExams",
    ),
}


@pytest.mark.anyio
async def test_discovery_routes_and_projection(tmp_path):
    auth, backend = manager(tmp_path)
    session = PlaywrightSession()
    backend.read_acorn = session.read_acorn
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert len(tools) == 25
        for name, (path, payload, field) in ROUTES.items():
            tool = tools[name]
            assert set(tool.input_schema["properties"]) == {"fields"}
            assert not tool.input_schema.get("required")
            assert tool.output_schema is None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is True
            assert len(tool.description) < 300
            response = Mock(
                status=200,
                headers={"content-type": "application/json"},
                text=AsyncMock(return_value=json.dumps(payload)),
                dispose=AsyncMock(),
            )
            session.api = Mock(get=AsyncMock(return_value=response))
            for args in ({}, {"fields": [field]}):
                result = await client.call_tool(name, args)
                assert not result.is_error
                assert len(result.content) == 1
                expected = select_fields(payload, args.get("fields"))
                assert result.content[0].text == json.dumps(
                    expected, ensure_ascii=False, separators=(",", ":")
                )
            assert session.api.get.await_count == 2
            session.api.get.assert_awaited_with(
                BASE_URL + path,
                headers={"Accept": "application/json"},
                timeout=30_000,
                max_redirects=0,
            )
            assert response.dispose.await_count == 2
        assert backend.visited == []
        assert "SYNTHETIC_PRIVATE" not in json.dumps(auth.status())
    assert backend.closed


@pytest.mark.anyio
async def test_invalid_fields_and_login_error(tmp_path):
    auth, backend = manager(tmp_path)
    backend.read_acorn = AsyncMock(return_value={"enrolledCourses": []})
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        for fields in ([], [""], [" "]):
            result = await client.call_tool("acorn_get_dashboard_courses", {"fields": fields})
            assert result.is_error
            assert "nonempty" in result.content[0].text
        backend.read_acorn.assert_not_called()
        result = await client.call_tool("acorn_get_dashboard_courses", {"fields": ["SECRET"]})
        assert result.is_error
        assert "Unknown field" in result.content[0].text
        assert "SECRET" not in result.content[0].text
        assert auth.status()["services"]["acorn"]["state"] == "connected"
        backend.read_acorn.side_effect = AcornError("Sign in with uoft_login.", "login_required")
        result = await client.call_tool("acorn_get_dashboard_courses", {})
        assert result.is_error
        assert "uoft_login" in result.content[0].text
        assert auth.status()["services"]["acorn"]["state"] == "login_required"
        assert backend.visited == []


@pytest.mark.parametrize(
    "data,fields,expected",
    [
        ([], None, []),
        ([], ["sessionDescription"], []),
        ({}, None, {}),
        (
            {"nested": [None, True, "\u00e9"], "other": 1},
            ["nested"],
            {"nested": [None, True, "\u00e9"]},
        ),
        ([{"a": 1}, {"b": 2}], ["a"], [{"a": 1}, {}]),
        ({"a": None}, ["a", "a"], {"a": None}),
    ],
)
def test_projection_preserves_values_without_mutation(data, fields, expected):
    before = json.dumps(data)
    assert select_fields(data, fields) == expected
    assert json.dumps(data) == before


@pytest.mark.parametrize(
    "data,fields",
    [
        ({}, ["unknown"]),
        ([], ["unknown"]),
        ({"a": 1}, []),
        ({"a": 1}, [2]),
        ([None], ["a"]),
        ({"a": 1}, "a"),
    ],
)
def test_projection_rejects_invalid_selections(data, fields):
    with pytest.raises(AcornError):
        select_fields(data, fields)
