"""Exercise the public MCP interface against a deterministic HTTP transport."""

import json

import httpx
import pytest
from mcp import Client

from uoft_mcp.server import create_server

TOOL_NAMES = {
    "get_current_sessions",
    "get_reference_data",
    "get_divisions",
    "search_departments",
    "search_course_titles",
    "get_course_details",
    "search_courses",
}


@pytest.mark.anyio
async def test_discovery_and_required_arguments():
    async with Client(
        create_server(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    ) as c:
        tools = {tool.name: tool for tool in (await c.list_tools()).tools}
        assert set(tools) == TOOL_NAMES
        for tool in tools.values():
            assert tool.description
            assert "ctx" not in tool.input_schema["properties"]
            assert tool.annotations.read_only_hint is True
        assert set(tools["search_course_titles"].input_schema["required"]) == {
            "term",
            "divisions",
            "sessions",
        }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,arguments,path,query",
    [
        ("get_current_sessions", {}, "current-session", {}),
        ("get_reference_data", {}, "reference-data", {}),
        ("get_divisions", {}, "getMatchingDivisions", {}),
        (
            "search_departments",
            {"term": "computer & math", "divisions": "ARTSC"},
            "getMatchingDepartments",
            {"term": "computer & math", "divisions": "ARTSC"},
        ),
        (
            "search_course_titles",
            {"term": "CSC", "divisions": "ARTSC", "sessions": "20269"},
            "getOptimizedMatchingCourseTitles",
            {
                "term": "CSC",
                "divisions": "ARTSC",
                "sessions": "20269",
                "lowerThreshold": "50",
                "upperThreshold": "200",
            },
        ),
        (
            "search_course_titles",
            {
                "term": "data",
                "divisions": "ARTSC",
                "sessions": "20271",
                "lower_threshold": 0,
                "upper_threshold": 10,
            },
            "getOptimizedMatchingCourseTitles",
            {
                "term": "data",
                "divisions": "ARTSC",
                "sessions": "20271",
                "lowerThreshold": "0",
                "upperThreshold": "10",
            },
        ),
        (
            "get_course_details",
            {"course_code": "CSC108H1"},
            "getCoursesByCodeAndSectionCode/CSC108H1",
            {},
        ),
        (
            "get_course_details",
            {"course_code": "CSC108H1", "section_code": "F"},
            "getCoursesByCodeAndSectionCode/CSC108H1",
            {"sectionCode": "F"},
        ),
    ],
)
async def test_get_requests(name, arguments, path, query):
    payload = {"payload": [{"unexpectedField": [None, True, {"nested": 42}]}], "status": []}
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.easi.utoronto.ca"
        assert request.url.path == f"/ttb/{path}"
        assert dict(request.url.params) == query
        assert not request.content
        assert request.headers["accept"] == "application/json"
        assert request.headers["origin"] == "https://ttb.utoronto.ca"
        assert request.headers["referer"] == "https://ttb.utoronto.ca/"
        assert request.headers["user-agent"].startswith("Mozilla/5.0")
        assert set(request.extensions["timeout"].values()) == {30.0}
        return httpx.Response(200, json=payload)

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        result = await client.call_tool(name, arguments)
        assert not result.is_error
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == payload
    assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [[], [1, {"code": "CSC108H1"}], None, "message", 7])
async def test_non_object_json_is_preserved(payload):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=json.dumps(payload)))
    async with Client(create_server(transport)) as client:
        result = await client.call_tool("get_current_sessions", {})
        assert not result.is_error
        assert json.loads(result.content[0].text) == payload


@pytest.mark.anyio
@pytest.mark.parametrize("custom", [False, True])
async def test_paginated_search(custom):
    arguments = (
        {
            "course_code": "CSC108H1",
            "course_title": "Programming",
            "course_section_code": "F",
            "search_course_description": True,
            "divisions": ["ARTSC"],
            "sessions": ["20269"],
            "campuses": ["St. George"],
            "delivery_modes": ["INPER"],
            "page": 2,
            "page_size": 5,
            "direction": "desc",
        }
        if custom
        else {}
    )
    expected = {
        "courseCodeAndTitleProps": {
            "courseCode": "CSC108H1" if custom else "",
            "courseTitle": "Programming" if custom else "",
            "courseSectionCode": "F" if custom else "",
            "searchCourseDescription": custom,
        },
        "departmentProps": [],
        "divisions": ["ARTSC"] if custom else [],
        "sessions": ["20269"] if custom else [],
        "campuses": ["St. George"] if custom else [],
        "deliveryModes": ["INPER"] if custom else [],
        "page": 2 if custom else 1,
        "pageSize": 5 if custom else 20,
        "direction": "desc" if custom else "asc",
    }
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/ttb/getPageableCourses"
        assert not request.url.query
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == expected
        return httpx.Response(200, json={"payload": {"pageableCourse": {"courses": []}}})

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        result = await client.call_tool("search_courses", arguments)
        assert not result.is_error
    assert len(seen) == 1  # The wrapper must not walk subsequent pages automatically.


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,message",
    [
        (404, "HTTP 404"),
        (500, "HTTP 500"),
        ("timeout", "timed out after 30 seconds"),
        ("connection", "Could not reach"),
        ("json", "invalid JSON"),
    ],
)
async def test_upstream_failures_are_tool_errors(failure, message):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("test connection", request=request)
        if failure == "json":
            return httpx.Response(200, text="<html>unavailable</html>")
        return httpx.Response(failure, text="upstream diagnostic body")

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        result = await client.call_tool("get_current_sessions", {})
        assert result.is_error
        assert message in result.content[0].text
        assert "current-session" in result.content[0].text
        assert "upstream diagnostic body" not in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("search_departments", {"term": "computer"}),
        ("search_departments", {"term": " ", "divisions": "ARTSC"}),
        ("search_course_titles", {"term": "CSC", "divisions": "ARTSC"}),
        (
            "search_course_titles",
            {
                "term": "CSC",
                "divisions": "ARTSC",
                "sessions": "20269",
                "lower_threshold": 20,
                "upper_threshold": 10,
            },
        ),
        ("get_course_details", {"course_code": "../current-session"}),
        ("get_course_details", {"course_code": "CSC108H1", "section_code": "X"}),
        ("search_courses", {"page": 0}),
        ("search_courses", {"page_size": 0}),
        ("search_courses", {"sessions": [""]}),
    ],
)
async def test_invalid_arguments_do_not_reach_upstream(name, arguments):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        result = await client.call_tool(name, arguments)
        assert result.is_error
    assert requests == []


@pytest.mark.anyio
async def test_shared_http_client_is_closed_after_lifespan(monkeypatch):
    import uoft_mcp.server as module

    clients = []
    original = module.create_http_client

    def track_client(transport):
        client = original(transport)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "create_http_client", track_client)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    async with Client(create_server(transport)) as client:
        await client.call_tool("get_current_sessions", {})
        await client.call_tool("get_divisions", {})
        assert len(clients) == 1
        assert not clients[0].is_closed
    assert clients[0].is_closed
