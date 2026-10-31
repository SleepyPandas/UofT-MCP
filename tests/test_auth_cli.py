"""Terminal commands share the same manager without exposing authentication state."""

import json
import sys
from unittest.mock import Mock

import pytest
from test_auth import manager

from uoft_mcp import cli
from uoft_mcp.utilities.browser_select import BROWSER_ENV


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


INSTALL = [sys.executable, "-m", "playwright", "install", "chromium"]


@pytest.fixture(autouse=True)
def clean_browser_env(monkeypatch):
    monkeypatch.delenv(BROWSER_ENV, raising=False)


@pytest.fixture
def no_system_browser(monkeypatch):
    monkeypatch.setattr(cli, "detect_system_browser", lambda choice: None)


def test_setup_uses_current_python_and_routes_output_to_stderr(monkeypatch, no_system_browser):
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 0
    run.assert_called_once_with(INSTALL, stdout=sys.stderr, stderr=sys.stderr, check=False)


def test_setup_propagates_install_failure(monkeypatch, no_system_browser):
    monkeypatch.setattr(cli.subprocess, "run", Mock(return_value=Mock(returncode=1)))
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 1


def test_setup_reports_when_the_installer_cannot_start(monkeypatch, no_system_browser, capsys):
    monkeypatch.setattr(cli.subprocess, "run", Mock(side_effect=OSError("SECRET")))
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 1
    assert "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("system", "label"), [("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")]
)
def test_setup_skips_the_download_when_a_system_browser_exists(monkeypatch, capsys, system, label):
    monkeypatch.setattr(cli, "detect_system_browser", lambda choice: system)
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 0
    run.assert_not_called()
    err = capsys.readouterr().err
    assert label in err
    assert "--force" in err


def test_setup_force_downloads_even_with_a_system_browser(monkeypatch):
    monkeypatch.setattr(cli, "detect_system_browser", lambda choice: "msedge")
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup", "--force"])
    assert exc.value.code == 0
    run.assert_called_once_with(INSTALL, stdout=sys.stderr, stderr=sys.stderr, check=False)


def test_setup_checks_the_requested_browser(monkeypatch):
    seen = []
    monkeypatch.setenv(BROWSER_ENV, "chromium")
    monkeypatch.setattr(cli, "detect_system_browser", lambda choice: seen.append(choice))
    monkeypatch.setattr(cli.subprocess, "run", Mock(return_value=Mock(returncode=0)))
    with pytest.raises(SystemExit):
        cli.main(["auth", "setup"])
    assert seen == ["chromium"]


def test_setup_rejects_an_invalid_override_without_downloading(monkeypatch, capsys):
    monkeypatch.setenv(BROWSER_ENV, "netscape")
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "setup"])
    assert exc.value.code == 1
    run.assert_not_called()
    assert BROWSER_ENV in capsys.readouterr().err


def test_no_subcommand_starts_stdio(monkeypatch):
    import uoft_mcp.server

    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr(uoft_mcp.server, "create_server", factory)
    cli.main([])
    factory.assert_called_once_with(tool_profile="compact")
    server.run.assert_called_once_with(transport="stdio")
