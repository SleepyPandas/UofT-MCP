"""`uoft-mcp doctor`: check the local install and print MCP client configuration."""

import json
import platform
import sys
from importlib import metadata
from pathlib import Path

from playwright.sync_api import sync_playwright

MIN_PYTHON = (3, 13)
SERVER_NAME = "uoft"


def server_command(command: str | None = None) -> tuple[str, list[str]]:
    """Return the absolute command and arguments an MCP client should launch."""
    if command:
        return str(Path(command).expanduser().resolve()), []
    script = Path(sys.executable).parent / (
        "uoft-mcp.exe" if sys.platform == "win32" else "uoft-mcp"
    )
    if script.is_file():
        return str(script), []
    return sys.executable, ["-m", "uoft_mcp"]


def client_config(command: str | None = None) -> dict:
    executable, args = server_command(command)
    entry: dict = {"command": executable}
    if args:
        entry["args"] = args
    return {"mcpServers": {SERVER_NAME: entry}}


def chromium_installed() -> bool:
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:  # Any Playwright failure means the browser is unusable.
        return False


def run_doctor(command: str | None = None) -> int:
    """Print a short report to stderr and the config JSON to stdout; return an exit code."""
    problems = 0
    version = metadata.version("uoft-mcp")
    print(f"uoft-mcp {version} on {platform.system()} {platform.machine()}", file=sys.stderr)

    python_ok = sys.version_info >= MIN_PYTHON
    problems += not python_ok
    print(
        f"[{'ok' if python_ok else 'FAIL'}] Python {platform.python_version()}"
        f" (needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)",
        file=sys.stderr,
    )

    executable, _ = server_command(command)
    command_ok = Path(executable).is_file()
    problems += not command_ok
    print(f"[{'ok' if command_ok else 'FAIL'}] Server command: {executable}", file=sys.stderr)

    if chromium_installed():
        print("[ok] Chromium for UofT sign-in is installed", file=sys.stderr)
    else:
        print(
            "[warn] Chromium is not installed (only needed to sign in to ACORN and Degree "
            "Explorer). Run: uoft-mcp auth setup",
            file=sys.stderr,
        )

    print(
        "\nPaste this into your MCP client configuration (Cursor, Claude Desktop, ...):\n",
        file=sys.stderr,
    )
    print(json.dumps(client_config(command), indent=2))
    return 1 if problems else 0
