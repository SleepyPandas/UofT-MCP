"""Offline, synthetic MCP efficiency comparison: python -m uoft_mcp.benchmark."""

import asyncio
import json

import httpx
from mcp import Client

from uoft_mcp.results import encode
from uoft_mcp.server import create_server


class SyntheticAuth:
    """No browser, credentials, keyring, persistence, or real student records."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def read_degree_explorer(self, endpoint):
        self.calls += 1
        return self.payload

    async def read_acorn(self, endpoint):
        self.calls += 1
        return self.payload

    async def close(self):
        pass


async def measure(profile: str, workflow: str) -> dict:
    rows = [
        {"code": f"CSC{i:03}H1", "term": "F", "details": "synthetic detail " * 80}
        for i in range(100)
    ]
    payload = {"payload": {"courses": rows}, "status": []}
    auth = SyntheticAuth(payload)
    public_calls = 0

    def upstream(request):
        nonlocal public_calls
        public_calls += 1
        return httpx.Response(200, json=payload)

    operations = (
        [
            ("get_course_details", {"course_code": "CSC108H1"}),
            ("get_course_details", {"course_code": "CSC148H1"}),
            ("get_course_details", {"course_code": "CSC165H1"}),
        ]
        if workflow == "courses"
        else [
            ("degree_explorer_get_academic_history", {}),
            ("degree_explorer_get_student_data", {}),
            ("degree_explorer_get_planner", {}),
        ]
    )
    selection = {"pointer": "/payload/courses", "fields": ["code", "term"], "limit": 5}
    result = {
        "profile": profile,
        "workflow": workflow,
        "tool_count": 0,
        "tool_definition_bytes": 0,
        "discovery_response_bytes": 0,
        "data_response_bytes": 0,
        "mcp_tool_calls": 0,
        "upstream_requests": 0,
        "paging_upstream_requests": 0,
    }
    async with Client(
        create_server(
            httpx.MockTransport(upstream), auth_factory=lambda: auth, tool_profile=profile
        )
    ) as client:
        tools = (await client.list_tools()).tools
        result["tool_count"] = len(tools)
        result["tool_definition_bytes"] = len(
            encode(
                [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
            ).encode()
        )

        async def invoke(name, arguments, *, discovery=False):
            response = await client.call_tool(name, arguments)
            if response.is_error:
                raise RuntimeError("Synthetic benchmark tool call failed.")
            result["mcp_tool_calls"] += 1
            text = response.content[0].text
            field = "discovery_response_bytes" if discovery else "data_response_bytes"
            result[field] += len(text.encode())
            return json.loads(text)

        if profile == "legacy":
            for name, arguments in operations:
                await invoke(name, arguments)
        else:
            # Cold workflow includes schema discovery, rather than assuming prior knowledge.
            await invoke(
                "uoft_discover",
                {
                    "query": "get_course_details" if workflow == "courses" else "",
                    "service": "timetable" if workflow == "courses" else "degree_explorer",
                    "detail": "schemas",
                    "limit": 25,
                },
                discovery=True,
            )
            data = await invoke(
                "uoft_read",
                {
                    "requests": [
                        {"operation": name, "arguments": arguments, "selection": selection}
                        for name, arguments in operations
                    ]
                },
            )
            calls_before = public_calls + auth.calls
            await invoke(
                "uoft_result",
                {
                    "handle": data["results"][0]["handle"],
                    "selection": {**selection, "offset": 5},
                },
            )
            result["paging_upstream_requests"] = public_calls + auth.calls - calls_before
        result["upstream_requests"] = public_calls + auth.calls
        result["total_response_bytes"] = (
            result["discovery_response_bytes"] + result["data_response_bytes"]
        )
    return result


async def run() -> list[dict]:
    return [
        await measure(profile, workflow)
        for workflow in ("courses", "academic_records")
        for profile in ("legacy", "compact")
    ]


def main() -> None:
    print(json.dumps(asyncio.run(run()), indent=2))


if __name__ == "__main__":
    main()
