"""`uoft-mcp doctor` reports the install and prints a ready-to-paste client configuration."""

import json
import sys
from pathlib import Path

import pytest

from uoft_mcp import cli, doctor


@pytest.fixture
def no_browser(monkeypatch):
    monkeypatch.setattr(doctor, "chromium_installed", lambda: False)


def test_config_uses_explicit_absolute_command(tmp_path):
    executable = tmp_path / "uoft-mcp"
    config = doctor.client_config(str(executable))
    entry = config["mcpServers"]["uoft"]
    assert entry == {"command": str(executable.resolve())}
    assert Path(entry["command"]).is_absolute()


def test_config_falls_back_to_python_module(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    entry = doctor.client_config()["mcpServers"]["uoft"]
    assert entry == {"command": str(tmp_path / "python"), "args": ["-m", "uoft_mcp"]}


def test_config_prefers_script_next_to_interpreter(monkeypatch, tmp_path):
    name = "uoft-mcp.exe" if sys.platform == "win32" else "uoft-mcp"
    (tmp_path / name).write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert doctor.client_config()["mcpServers"]["uoft"] == {"command": str(tmp_path / name)}


def test_doctor_prints_json_on_stdout_and_report_on_stderr(capsys, no_browser, tmp_path):
    executable = tmp_path / "uoft-mcp"
    executable.write_text("")
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "--command", str(executable)])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["mcpServers"]["uoft"]["command"] == str(executable.resolve())
    assert "auth setup" in captured.err
    assert "[ok] Server command" in captured.err


def test_doctor_fails_when_command_is_missing(capsys, no_browser, tmp_path):
    assert doctor.run_doctor(str(tmp_path / "missing")) == 1
    assert "[FAIL] Server command" in capsys.readouterr().err
