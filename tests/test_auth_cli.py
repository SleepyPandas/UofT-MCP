"""Terminal commands share the same manager without exposing authentication state."""

import json
import sys
from unittest.mock import Mock

import pytest
from test_auth import manager

from uoft_mcp import cli


@pytest.mark.anyio
async def test_terminal_login_and_forget(tmp_path, capsys):
    auth, browser = manager(tmp_path)
    code = await cli.run_auth(cli.parser().parse_args(["auth", "login"]), lambda: auth)
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["login"]["state"] == "complete"
    assert "SECRET" not in captured.out + captured.err
    assert browser.closed
    assert not auth.store._owned
    code = await cli.run_auth(cli.parser().parse_args(["auth", "forget"]), lambda: auth)
    assert code == 0
    assert not auth.store.path.exists()


@pytest.mark.anyio
async def test_terminal_status_does_not_open_browser(tmp_path, capsys):
    auth, browser = manager(tmp_path)
    code = await cli.run_auth(cli.parser().parse_args(["auth", "status"]), lambda: auth)
    assert code == 1
    assert not browser.visited
    assert json.loads(capsys.readouterr().out)["services"]["acorn"]["state"] == "login_required"


def test_setup_uses_current_python_and_routes_output_to_stderr(monkeypatch):
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 0
    run.assert_called_once_with(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdout=sys.stderr,
        stderr=sys.stderr,
        check=False,
    )


def test_setup_propagates_install_failure(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", Mock(return_value=Mock(returncode=1)))
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 1


def test_no_subcommand_starts_stdio(monkeypatch):
    import uoft_mcp.server

    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr(uoft_mcp.server, "create_server", factory)
    cli.main([])
    factory.assert_called_once_with(tool_profile="compact")
    server.run.assert_called_once_with(transport="stdio")
