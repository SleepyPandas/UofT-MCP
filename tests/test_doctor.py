"""`uoft-mcp doctor` reports the install and prints a ready-to-paste client configuration."""

import json
import sys
from pathlib import Path

import pytest

from uoft_mcp import cli, doctor
from uoft_mcp.utilities.browser_select import BROWSER_ENV


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(BROWSER_ENV, raising=False)


@pytest.fixture
def no_browser(monkeypatch):
    monkeypatch.setattr(doctor, "detect_system_browser", lambda choice: None)
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


@pytest.mark.parametrize(
    ("system", "label"), [("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")]
)
def test_doctor_reports_installed_system_browser_without_starting_playwright(
    monkeypatch, system, label
):
    monkeypatch.setattr(doctor, "detect_system_browser", lambda choice: system)

    def fail():
        raise AssertionError("must not start Playwright when a system browser exists")

    monkeypatch.setattr(doctor, "chromium_installed", fail)
    report = doctor.sign_in_browser_report()
    assert report.startswith(f"[ok] Sign-in will use {label}")
    assert "auth setup" not in report


def test_doctor_reports_bundled_chromium_when_no_system_browser(monkeypatch):
    monkeypatch.setattr(doctor, "detect_system_browser", lambda choice: None)
    monkeypatch.setattr(doctor, "chromium_installed", lambda: True)
    assert doctor.sign_in_browser_report().startswith("[ok] Sign-in will use the Chromium")


def test_doctor_recommends_setup_only_when_nothing_is_usable(no_browser):
    report = doctor.sign_in_browser_report()
    assert report.startswith("[warn]")
    assert "Microsoft Edge or Google Chrome" in report
    assert "uoft-mcp auth setup" in report


def test_doctor_warning_does_not_fail_the_check(capsys, no_browser, tmp_path):
    executable = tmp_path / "uoft-mcp"
    executable.write_text("")
    assert doctor.run_doctor(str(executable)) == 0
    assert "[warn]" in capsys.readouterr().err


def test_doctor_honours_the_override_when_it_names_bundled_chromium(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "chromium")
    monkeypatch.setattr(doctor, "chromium_installed", lambda: False)
    report = doctor.sign_in_browser_report()
    assert report.startswith("[warn]")
    assert "Run: uoft-mcp auth setup" in report


def test_doctor_override_to_missing_system_browser_does_not_fall_back(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "msedge")
    monkeypatch.setattr(doctor, "detect_system_browser", lambda choice: None)

    def fail():
        raise AssertionError("an explicit system browser never falls back to Chromium")

    monkeypatch.setattr(doctor, "chromium_installed", fail)
    report = doctor.sign_in_browser_report()
    assert report.startswith("[warn] Microsoft Edge is not available")


def test_doctor_warns_about_an_invalid_override(monkeypatch):
    monkeypatch.setenv(BROWSER_ENV, "netscape")
    report = doctor.sign_in_browser_report()
    assert report.startswith("[warn]")
    assert BROWSER_ENV in report
    assert "netscape" not in report
