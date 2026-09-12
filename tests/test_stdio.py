"""Verify the installed module speaks MCP over actual process pipes, without network calls."""

import asyncio
import json
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters


@pytest.mark.anyio
async def test_sdk_connects_to_stdio_from_another_directory(tmp_path):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "uoft_mcp"], cwd=str(tmp_path)
    )
    with anyio.fail_after(20):
        async with Client(parameters, mode="legacy") as client:
            assert len((await client.list_tools()).tools) == 22
            result = await client.call_tool("uoft_auth_status", {})
            status = json.loads(result.content[0].text)
            assert status["services"]["acorn"]["state"] == "not_checked"
            assert status["persistence"]["message"] == "Saved sessions have not been opened."


@pytest.mark.anyio
async def test_stdout_contains_only_protocol_json():
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "stdio-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uoft_mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        for message in messages:
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            if "id" in message:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
                response = json.loads(line)
                assert response["jsonrpc"] == "2.0"
                assert response["id"] == message["id"]
                assert "result" in response
        process.stdin.close()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        assert not stdout
        assert process.returncode == 0, stderr.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
