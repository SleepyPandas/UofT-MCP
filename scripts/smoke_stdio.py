"""Smoke-test an installed `uoft-mcp` executable with a real MCP stdio handshake.

Usage: python scripts/smoke_stdio.py PATH_TO_UOFT_MCP

Standard library only, so it runs with any Python and does not depend on the install under test.
Makes no network calls and needs no sign-in.
"""

import json
import subprocess
import sys

EXPECTED_TOOLS = {"uoft_discover", "uoft_read", "uoft_result", "save_timetable", "uoft_login"}


def main(executable: str) -> int:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "install-smoke", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    process = subprocess.Popen(
        [executable],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    tools: set[str] = set()
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" not in message:
                continue
            response = json.loads(process.stdout.readline())
            assert response["id"] == message["id"] and "result" in response, response
            if message["id"] == 2:
                tools = {tool["name"] for tool in response["result"]["tools"]}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    missing = EXPECTED_TOOLS - tools
    if missing:
        print(f"FAIL: missing tools {sorted(missing)}; got {sorted(tools)}", file=sys.stderr)
        return 1
    print(f"OK: {executable} answered initialize and listed {len(tools)} tools")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
