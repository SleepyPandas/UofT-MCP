"""Exercise real compact MCP calls, offline batching, and private snapshot boundaries."""

import asyncio
import json

import httpx
import pytest
from mcp import Client
from test_auth import manager

from uoft_mcp.acorn.client import AcornError
from uoft_mcp.server import create_server


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert len(result.content) == 1
    assert result.structured_content is None
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_discovery_surface_and_schema_savings():
    sizes = {}
    for profile, count in [("compact", 7), ("legacy", 25)]:
        async with Client(create_server(tool_profile=profile)) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == count
            sizes[profile] = len(json.dumps([t.model_dump(mode="json") for t in tools]).encode())
            if profile == "compact":
                assert {t.name for t in tools} == {
                    "uoft_discover",
                    "uoft_read",
                    "uoft_result",
                    "save_timetable",
                    "uoft_login",
                    "uoft_auth_status",
                    "uoft_forget_session",
                }
                all_ops = await call(client, "uoft_discover", {"detail": "names", "limit": 25})
                assert len(all_ops["operations"]) == 21
                assert "save_timetable" not in all_ops["operations"]
                exact = await call(
                    client,
                    "uoft_discover",
                    {
                        "query": "get_course_details",
                        "detail": "schemas",
                        "limit": 1,
                    },
                )
                assert exact["operations"][0]["arguments"]["required"] == ["course_code"]
                assert "ctx" not in exact["operations"][0]["arguments"]["properties"]
                acorn = await call(client, "uoft_discover", {"service": "acorn"})
                assert len(acorn["operations"]) == 3
                assert all(op["requires_auth"] for op in acorn["operations"])
    assert sizes["compact"] < sizes["legacy"] * 0.5


@pytest.mark.anyio
async def test_batch_dedup_partial_failure_and_snapshot_paging():
    requests = []

    async def upstream(request):
        requests.append(request)
        if request.url.path.endswith("reference-data"):
            return httpx.Response(503, text="PRIVATE upstream diagnostics")
        return httpx.Response(200, json=list(range(40)))

    async with Client(create_server(httpx.MockTransport(upstream))) as client:
        results = (
            await call(
                client,
                "uoft_read",
                {
                    "requests": [
                        {"operation": "get_current_sessions"},
                        {"operation": "get_reference_data"},
                        {"operation": "get_current_sessions", "selection": {"offset": 20}},
                    ]
                },
            )
        )["results"]
        assert len(requests) == 2
        assert results[0]["data"] == list(range(20))
        assert "503" in results[1]["error"] and "PRIVATE" not in results[1]["error"]
        assert results[2]["data"] == list(range(20, 40))
        assert results[0]["handle"] == results[2]["handle"]
        page = await call(
            client,
            "uoft_result",
            {
                "handle": results[0]["handle"],
                "selection": {"offset": 20},
            },
        )
        assert page["data"] == list(range(20, 40))
        assert len(requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        {"operation": "save_timetable"},
        {"operation": "https://example.com"},
        {"operation": "get_course_details", "arguments": {"course_code": "../bad"}},
        {"operation": "get_current_sessions", "arguments": {"unexpected": 1}},
        {"operation": "get_current_sessions", "selection": {"pointer": "invalid"}},
        {
            "operation": "search_course_titles",
            "arguments": {
                "term": "CSC",
                "divisions": "ARTSC",
                "sessions": "20269",
                "lower_threshold": 200,
                "upper_threshold": 50,
            },
        },
        {
            "operation": "generate_timetable",
            "arguments": {
                "plans": [
                    {
                        "courses": [{"course_id": "a", "activity_types": ["Lecture"]}],
                        "blocked_times": [{"day": "Monday", "start": "12:00", "end": "11:00"}],
                    }
                ]
            },
        },
    ],
)
async def test_entire_batch_validated_before_io(bad):
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    async with Client(create_server(httpx.MockTransport(upstream))) as client:
        result = await client.call_tool(
            "uoft_read",
            {
                "requests": [
                    {"operation": "get_current_sessions"},
                    bad,
                ]
            },
        )
        assert result.is_error
        assert not requests


@pytest.mark.anyio
async def test_public_concurrency_is_four():
    active = peak = 0
    gate = asyncio.Event()

    async def upstream(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={})

    async with Client(create_server(httpx.MockTransport(upstream))) as client:
        result = await call(
            client,
            "uoft_read",
            {
                "requests": [
                    {"operation": "get_course_details", "arguments": {"course_code": f"CSC{i}H1"}}
                    for i in range(8)
                ]
            },
        )
        assert all("error" not in item for item in result["results"])
        assert peak == 4


@pytest.mark.anyio
async def test_private_serialization_and_failure_invalidates_handles(tmp_path):
    auth, backend = manager(tmp_path)
    active = peak = 0
    fail = False

    async def read(endpoint):
        nonlocal active, peak
        if fail:
            raise AcornError("Login required.", "login_required")
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"courses": ["SYNTHETIC"]}

    backend.read_acorn = read
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        items = (
            await call(
                client,
                "uoft_read",
                {
                    "requests": [
                        {"operation": "acorn_get_dashboard_courses"},
                        {"operation": "acorn_get_student_registration_info"},
                    ]
                },
            )
        )["results"]
        assert peak == 1
        handle = items[0]["handle"]
        assert (await call(client, "uoft_result", {"handle": handle}))["data"]
        fail = True
        failed = await call(
            client,
            "uoft_read",
            {
                "requests": [
                    {"operation": "acorn_get_dashboard_courses"},
                ]
            },
        )
        assert "error" in failed["results"][0]
        assert (await client.call_tool("uoft_result", {"handle": handle})).is_error


@pytest.mark.anyio
async def test_forget_during_read_cannot_retain_or_return_private_result(tmp_path):
    auth, backend = manager(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(endpoint):
        started.set()
        await release.wait()
        return {"secret": "DO_NOT_RETURN"}

    backend.read_acorn = read
    async with Client(create_server(auth_factory=lambda: auth)) as client:
        pending = asyncio.create_task(
            call(
                client,
                "uoft_read",
                {
                    "requests": [
                        {"operation": "acorn_get_dashboard_courses"},
                    ]
                },
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        forgetting = asyncio.create_task(auth.forget())
        await asyncio.sleep(0.01)
        release.set()
        result = await pending
        await forgetting
        assert "error" in result["results"][0]
        assert "DO_NOT_RETURN" not in json.dumps(result)


@pytest.mark.anyio
async def test_login_clears_private_snapshot_but_public_snapshot_survives(tmp_path):
    auth, backend = manager(tmp_path)

    async def read(endpoint):
        return {"registrationStatusList": []}

    backend.read_acorn = read
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=["public"]))
    async with Client(create_server(transport, auth_factory=lambda: auth)) as client:
        items = (
            await call(
                client,
                "uoft_read",
                {
                    "requests": [
                        {"operation": "get_current_sessions"},
                        {"operation": "acorn_get_student_registration_info"},
                    ]
                },
            )
        )["results"]
        await call(client, "uoft_login", {"service": "acorn"})
        assert (await client.call_tool("uoft_result", {"handle": items[1]["handle"]})).is_error
        public = await call(client, "uoft_result", {"handle": items[0]["handle"]})
        assert public["data"] == ["public"]


@pytest.mark.anyio
async def test_selection_error_retains_handle_for_correction():
    count = 0

    def upstream(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"payload": [{"code": "CSC108H1"}]})

    async with Client(create_server(httpx.MockTransport(upstream))) as client:
        item = (
            await call(
                client,
                "uoft_read",
                {
                    "requests": [
                        {
                            "operation": "get_course_details",
                            "arguments": {"course_code": "CSC108H1"},
                            "selection": {"pointer": "/nonexistent"},
                        }
                    ]
                },
            )
        )["results"][0]
        assert "error" in item and item["retained"]
        corrected = await call(
            client,
            "uoft_result",
            {
                "handle": item["handle"],
                "selection": {"pointer": "/payload/0/code"},
            },
        )
        assert corrected["data"] == "CSC108H1" and count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("count", [0, 9])
async def test_batch_size_bounds(count):
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with Client(create_server(httpx.MockTransport(upstream))) as client:
        result = await client.call_tool(
            "uoft_read",
            {
                "requests": [{"operation": "get_current_sessions"}] * count,
            },
        )
        assert result.is_error and not requests
