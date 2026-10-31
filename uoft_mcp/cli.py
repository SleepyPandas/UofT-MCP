"""Local stdio by default; explicit terminal commands for authentication setup."""

import argparse
import asyncio
import json
import logging
import subprocess
import sys

from uoft_mcp.doctor import run_doctor
from uoft_mcp.utilities.auth import AuthManager
from uoft_mcp.utilities.browser_select import (
    BrowserUnavailable,
    browser_label,
    detect_system_browser,
    requested_browser,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="UofT MCP server and local authentication controls"
    )
    result.add_argument(
        "--tool-profile",
        choices=["compact", "legacy"],
        default="compact",
        help="MCP interface: compact (10 tools) or legacy (28 tools)",
    )
    commands = result.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="Check this install and print MCP client config")
    doctor.add_argument(
        "--command",
        dest="command_path",
        help="Server executable to put in the config (default: this install)",
    )
    auth = commands.add_parser("auth", help="Connect Degree Explorer and ACORN")
    actions = auth.add_subparsers(dest="action", required=True)
    setup = actions.add_parser(
        "setup",
        help="Download Chromium for sign-in, unless Edge or Chrome is already installed",
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="Download Chromium even if Edge or Chrome is installed",
    )
    login = actions.add_parser("login", help="Complete official UofT login in a browser")
    login.add_argument("--service", choices=["degree_explorer", "acorn", "both"], default="both")
    login.add_argument("--no-remember", action="store_true", help="Use a fresh memory-only session")
    actions.add_parser("status", help="Verify saved connections without opening a login window")
    actions.add_parser(
        "forget",
        aliases=["logout"],
        help="Delete local saved UofT sessions and their encryption key",
    )
    return result


async def run_auth(args: argparse.Namespace, manager_factory=AuthManager) -> int:
    manager = manager_factory()
    try:
        if args.action == "login":
            print(
                "Complete the official UofT login in the browser if prompted. Waiting...",
                file=sys.stderr,
            )
            await manager.login(args.service, remember=not args.no_remember)
            result = await manager.wait_for_login()
            success = result["login"]["state"] == "complete"
        elif args.action == "status":
            result = await manager.refresh()
            success = result["login"]["state"] != "failed" and all(
                service["state"] == "connected" for service in result["services"].values()
            )
        else:
            result = await manager.forget()
            success = result["login"]["state"] != "failed"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if success else 1
    finally:
        await manager.close()


def run_setup(*, force: bool) -> int:
    """Download Chromium only when sign-in has no installed Edge or Chrome to use."""
    if not force:
        try:
            system = detect_system_browser(requested_browser())
        except BrowserUnavailable as exc:
            print(exc, file=sys.stderr)
            return 1
        if system is not None:
            print(
                f"UofT sign-in will use {browser_label(system)}, which is already installed. "
                "No download needed. Use --force to download Chromium anyway.",
                file=sys.stderr,
            )
            return 0
    # Browser installation is explicit and never runs during server startup or a tool call.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=False,
        )
    except OSError:
        print("Could not start browser setup. Reinstall uoft-mcp and try again.", file=sys.stderr)
        return 1
    return result.returncode


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if args.command is None:
        from uoft_mcp.server import create_server

        create_server(tool_profile=args.tool_profile).run(transport="stdio")
        return
    if args.command == "doctor":
        raise SystemExit(run_doctor(args.command_path))
    if args.action == "setup":
        raise SystemExit(run_setup(force=args.force))
    try:
        raise SystemExit(asyncio.run(run_auth(args)))
    except KeyboardInterrupt:
        print("Login cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
