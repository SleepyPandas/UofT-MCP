"""Plain-language status classification: HTTP responses, exceptions, and tool surfaces."""

import json
import socket
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from mcp import Client
from test_auth import manager

from uoft_mcp.acorn.client import AcornEndpoint, AcornError
from uoft_mcp.degree_explorer.client import DegreeExplorerEndpoint, DegreeExplorerError
from uoft_mcp.server import create_server
from uoft_mcp.status import (
    SERVICE_LABELS,
    ServiceReadError,
    StatusKind,
    classify_exception,
    classify_http_status,
    describe,
    kind_for_state,
    legacy_state,
    looks_like_maintenance,
)
from uoft_mcp.timetable_builder.client import (
    TimetableAPIError,
    create_http_client,
    request_json,
)
from uoft_mcp.utilities.auth_browser import (
    SERVICES,
    PlaywrightSession,
    classify_and_parse_response,
)

JSON = {"content-type": "application/json"}
HTML = {"content-type": "text/html"}
SERVICE_NAMES = [*SERVICE_LABELS]


class PlaywrightTimeoutError(Exception):
    """Stands in for playwright.async_api.TimeoutError, which is not a builtin subclass."""


PlaywrightTimeoutError.__name__ = "TimeoutError"


# --- the central catalogue -------------------------------------------------------------


@pytest.mark.parametrize("kind", list(StatusKind))
@pytest.mark.parametrize("service", SERVICE_NAMES)
def test_every_kind_has_a_plain_message_and_state(kind, service):
    status = describe(kind, service)
    assert status.kind is kind
    assert status.summary
    assert status.state == legacy_state(kind)
    assert status.message.startswith(status.summary)
    assert "None" not in status.message
    assert "{" not in status.message
    if kind is not StatusKind.CONNECTED:
        assert status.next_action
    payload = status.as_dict()
    assert payload["kind"] == kind.value
    assert json.dumps(payload)


def test_kinds_map_onto_the_existing_machine_readable_states():
    assert {kind: legacy_state(kind) for kind in StatusKind} == {
        StatusKind.CONNECTED: "connected",
        StatusKind.NOT_CHECKED: "not_checked",
        StatusKind.SIGNED_OUT: "login_required",
        StatusKind.DUO_PENDING: "login_required",
        StatusKind.LOGIN_IN_PROGRESS: "login_in_progress",
        StatusKind.ACCESS_DENIED: "access_denied",
        StatusKind.SERVICE_DOWN: "unavailable",
        StatusKind.RATE_LIMITED: "unavailable",
        StatusKind.NETWORK: "unavailable",
        StatusKind.UNKNOWN: "unavailable",
        StatusKind.PAGE_CHANGED: "unexpected_response",
        StatusKind.REQUEST_REJECTED: "unexpected_response",
    }


@pytest.mark.parametrize(
    "state,kind",
    [
        ("connected", StatusKind.CONNECTED),
        ("not_checked", StatusKind.NOT_CHECKED),
        ("login_required", StatusKind.SIGNED_OUT),
        ("login_in_progress", StatusKind.LOGIN_IN_PROGRESS),
        ("access_denied", StatusKind.ACCESS_DENIED),
        ("unavailable", StatusKind.SERVICE_DOWN),
        ("unexpected_response", StatusKind.PAGE_CHANGED),
        ("something_new", StatusKind.UNKNOWN),
    ],
)
def test_legacy_state_strings_get_a_kind(state, kind):
    assert kind_for_state(state) is kind
    assert ServiceReadError("x", state).kind is kind


def test_example_messages_read_as_plain_words():
    assert describe(StatusKind.SIGNED_OUT, "acorn").message == (
        "Signed out of ACORN. Run uoft_login to sign in again."
    )
    assert describe(StatusKind.DUO_PENDING, "degree_explorer").message == (
        "Duo sign-in pending or failed for Degree Explorer. "
        "Run uoft_login and approve the Duo prompt."
    )
    assert describe(StatusKind.SERVICE_DOWN, "acorn", "HTTP 503").message == (
        "ACORN is down or not responding (HTTP 503). Try again later."
    )
    assert "Degree Explorer is down" in describe(StatusKind.SERVICE_DOWN, "degree_explorer").message
    assert "no internet connection" in describe(StatusKind.NETWORK, "acorn").message
    assert "UofT changed the ACORN page" in describe(StatusKind.PAGE_CHANGED, "acorn").message
    assert describe(StatusKind.DUO_PENDING, "").summary == "Duo sign-in pending or failed."


def test_access_denied_advice_depends_on_whether_login_applies():
    private = describe(StatusKind.ACCESS_DENIED, "acorn", "HTTP 403")
    public = describe(StatusKind.ACCESS_DENIED, "timetable", "HTTP 403")
    assert "uoft_login" in private.next_action
    assert "uoft_login" not in public.next_action


# --- HTTP statuses ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, StatusKind.SIGNED_OUT),
        (403, StatusKind.ACCESS_DENIED),
        (408, StatusKind.SERVICE_DOWN),
        (429, StatusKind.RATE_LIMITED),
        (500, StatusKind.SERVICE_DOWN),
        (502, StatusKind.SERVICE_DOWN),
        (503, StatusKind.SERVICE_DOWN),
        (504, StatusKind.SERVICE_DOWN),
        (404, StatusKind.PAGE_CHANGED),
    ],
)
def test_http_status_classification_for_private_services(status, kind):
    result = classify_http_status("acorn", status)
    assert result is not None
    assert result.kind is kind


def test_missing_public_route_is_a_rejected_request_not_a_changed_page():
    result = classify_http_status("timetable", 404, "getCoursesByCodeAndSectionCode/X")
    assert result is not None
    assert result.kind is StatusKind.REQUEST_REJECTED
    assert "HTTP 404 at getCoursesByCodeAndSectionCode/X" in result.message


@pytest.mark.parametrize("status", [200, 204, 301, 302])
def test_success_and_redirect_statuses_need_the_body(status):
    assert classify_http_status("acorn", status) is None


@pytest.mark.parametrize(
    "status,headers,body,kind",
    [
        (401, {}, "SECRET", StatusKind.SIGNED_OUT),
        (
            302,
            {"location": "https://idpz.utorauth.utoronto.ca/x?SECRET"},
            "",
            StatusKind.SIGNED_OUT,
        ),
        (302, {"location": "https://weblogin.utoronto.ca/x"}, "", StatusKind.SIGNED_OUT),
        (302, {"location": "https://sso.duosecurity.com/x"}, "", StatusKind.SIGNED_OUT),
        (302, {"location": "https://evil.test/?SECRET"}, "", StatusKind.PAGE_CHANGED),
        (200, HTML, "<input name='j_password'>", StatusKind.SIGNED_OUT),
        (200, HTML, "<form>SAMLRequest</form>", StatusKind.SIGNED_OUT),
        (403, {}, "SECRET", StatusKind.ACCESS_DENIED),
        (429, {}, "SECRET", StatusKind.RATE_LIMITED),
        (500, {}, "SECRET", StatusKind.SERVICE_DOWN),
        (503, HTML, "<h1>Down for maintenance SECRET</h1>", StatusKind.SERVICE_DOWN),
        (200, HTML, "<h1>Scheduled maintenance SECRET</h1>", StatusKind.SERVICE_DOWN),
        (404, {}, "SECRET", StatusKind.PAGE_CHANGED),
        (200, HTML, "<h1>New page SECRET</h1>", StatusKind.PAGE_CHANGED),
        (200, JSON, "{SECRET", StatusKind.PAGE_CHANGED),
        (200, JSON, '"SECRET"', StatusKind.PAGE_CHANGED),
        (200, JSON, "[]", StatusKind.CONNECTED),
    ],
)
def test_response_classification_covers_auth_outage_and_parse_paths(status, headers, body, kind):
    result, payload = classify_and_parse_response(SERVICES["acorn"], status, headers, body)
    assert result.kind is kind
    assert result.state == legacy_state(kind)
    assert "SECRET" not in result.message
    assert (payload is not None) is (kind is StatusKind.CONNECTED)


def test_outage_message_includes_http_code_and_service():
    result, _ = classify_and_parse_response(SERVICES["acorn"], 503, {}, "")
    assert result.message == "ACORN is down or not responding (HTTP 503). Try again later."
    result, _ = classify_and_parse_response(SERVICES["degree_explorer"], 502, {}, "")
    assert result.message.startswith("Degree Explorer is down or not responding (HTTP 502)")


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Down for MAINTENANCE", True),
        ("Service Unavailable", True),
        ("<h1>Welcome</h1>", False),
    ],
)
def test_maintenance_detection(body, expected):
    assert looks_like_maintenance(body) is expected


# --- exceptions ------------------------------------------------------------------------


def _http_error(status):
    request = httpx.Request("GET", "https://api.easi.utoronto.ca/ttb/x")
    return httpx.HTTPStatusError("bad", request=request, response=httpx.Response(status))


@pytest.mark.parametrize(
    "exc,kind",
    [
        (TimeoutError("SECRET"), StatusKind.SERVICE_DOWN),
        (httpx.ReadTimeout("SECRET"), StatusKind.SERVICE_DOWN),
        (PlaywrightTimeoutError("Timeout 30000ms exceeded"), StatusKind.SERVICE_DOWN),
        (RuntimeError("Request timed out SECRET"), StatusKind.SERVICE_DOWN),
        (httpx.ConnectError("SECRET"), StatusKind.NETWORK),
        (ConnectionRefusedError("SECRET"), StatusKind.NETWORK),
        (socket.gaierror(11001, "getaddrinfo failed"), StatusKind.NETWORK),
        (RuntimeError("getaddrinfo ENOTFOUND acorn.utoronto.ca"), StatusKind.NETWORK),
        (RuntimeError("net::ERR_INTERNET_DISCONNECTED"), StatusKind.NETWORK),
        (RuntimeError("connect ECONNREFUSED 1.2.3.4:443"), StatusKind.NETWORK),
        (_http_error(503), StatusKind.SERVICE_DOWN),
        (_http_error(429), StatusKind.RATE_LIMITED),
        (_http_error(403), StatusKind.ACCESS_DENIED),
        (_http_error(404), StatusKind.PAGE_CHANGED),
        (ValueError("SECRET"), StatusKind.PAGE_CHANGED),
        (RuntimeError("SECRET"), StatusKind.UNKNOWN),
    ],
)
def test_exception_classification(exc, kind):
    result = classify_exception(exc, "acorn")
    assert result.kind is kind
    assert "SECRET" not in result.message
    assert "ACORN" in result.message


def test_timeout_message_includes_limit_and_location():
    result = classify_exception(
        httpx.ReadTimeout("x"), "timetable", where="current-session", timeout_seconds=30.0
    )
    assert "timed out after 30 seconds at current-session" in result.message


# --- Timetable Builder (public API) ----------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "respond,kind,fragment",
    [
        (lambda r: httpx.Response(503, text="SECRET"), StatusKind.SERVICE_DOWN, "HTTP 503"),
        (lambda r: httpx.Response(429), StatusKind.RATE_LIMITED, "HTTP 429"),
        (lambda r: httpx.Response(403), StatusKind.ACCESS_DENIED, "HTTP 403"),
        (lambda r: httpx.Response(404), StatusKind.REQUEST_REJECTED, "HTTP 404"),
        (
            lambda r: httpx.Response(200, text="<html>SECRET"),
            StatusKind.PAGE_CHANGED,
            "invalid JSON",
        ),
    ],
)
async def test_timetable_http_failures(respond, kind, fragment):
    async with create_http_client(httpx.MockTransport(respond)) as client:
        with pytest.raises(TimetableAPIError) as error:
            await request_json(client, "GET", "current-session")
    assert error.value.kind is kind
    assert error.value.state == legacy_state(kind)
    assert fragment in str(error.value)
    assert "current-session" in str(error.value)
    assert "SECRET" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,kind",
    [
        (httpx.ReadTimeout("t"), StatusKind.SERVICE_DOWN),
        (httpx.ConnectTimeout("t"), StatusKind.SERVICE_DOWN),
        (httpx.ConnectError("dns"), StatusKind.NETWORK),
        (httpx.ReadError("reset"), StatusKind.NETWORK),
    ],
)
async def test_timetable_transport_failures(failure, kind):
    def respond(request):
        raise failure

    async with create_http_client(httpx.MockTransport(respond)) as client:
        with pytest.raises(TimetableAPIError) as error:
            await request_json(client, "GET", "current-session")
    assert error.value.kind is kind


@pytest.mark.anyio
async def test_timetable_tool_error_is_plain_language():
    def respond(request):
        return httpx.Response(503)

    async with Client(create_server(httpx.MockTransport(respond), tool_profile="legacy")) as client:
        result = await client.call_tool("get_current_sessions", {})
    assert result.is_error
    assert "Timetable Builder is down or not responding (HTTP 503 at current-session)" in (
        result.content[0].text
    )


# --- Playwright probe and reads --------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,kind",
    [
        (TimeoutError("SECRET"), StatusKind.SERVICE_DOWN),
        (PlaywrightTimeoutError("SECRET"), StatusKind.SERVICE_DOWN),
        (RuntimeError("getaddrinfo ENOTFOUND SECRET"), StatusKind.NETWORK),
        (RuntimeError("SECRET"), StatusKind.UNKNOWN),
    ],
)
async def test_probe_transport_failures_are_classified(failure, kind):
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(side_effect=failure))
    result = await session.probe(SERVICES["acorn"])
    assert result.kind is kind
    assert result.state == "unavailable"
    assert "SECRET" not in result.message


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reader,endpoint,error_type,service",
    [
        ("read_acorn", AcornEndpoint.DASHBOARD_COURSES, AcornError, "ACORN"),
        (
            "read_degree_explorer",
            DegreeExplorerEndpoint.PLANNER,
            DegreeExplorerError,
            "Degree Explorer",
        ),
    ],
)
@pytest.mark.parametrize(
    "status,kind,fragment",
    [
        (503, StatusKind.SERVICE_DOWN, "is down or not responding (HTTP 503)"),
        (401, StatusKind.SIGNED_OUT, "Signed out"),
        (429, StatusKind.RATE_LIMITED, "slow down"),
    ],
)
async def test_reads_report_service_specific_plain_errors(
    reader, endpoint, error_type, service, status, kind, fragment
):
    response = Mock(status=status, headers={}, text=AsyncMock(return_value=""), dispose=AsyncMock())
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(return_value=response))
    with pytest.raises(error_type) as error:
        await getattr(session, reader)(endpoint)
    assert error.value.kind is kind
    assert service in str(error.value)
    assert fragment in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,kind",
    [
        (RuntimeError("getaddrinfo ENOTFOUND"), StatusKind.NETWORK),
        (TimeoutError(), StatusKind.SERVICE_DOWN),
    ],
)
async def test_reads_distinguish_network_from_outage(failure, kind):
    session = PlaywrightSession()
    session.api = Mock(get=AsyncMock(side_effect=failure))
    with pytest.raises(AcornError) as error:
        await session.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
    assert error.value.kind is kind


# --- AuthManager status surface --------------------------------------------------------


@pytest.mark.anyio
async def test_status_adds_kind_and_next_action_without_removing_fields(tmp_path):
    auth, _ = manager(tmp_path)
    try:
        initial = auth.status()
        for name in SERVICES:
            service = initial["services"][name]
            assert service["state"] == "not_checked"
            assert service["kind"] == "not_checked"
            assert "uoft_auth_status" in service["next_action"]
            assert {"state", "message", "last_verified"} <= service.keys()
        assert {"login", "persistence"} <= initial.keys()

        refreshed = await auth.refresh()
        for name in SERVICES:
            service = refreshed["services"][name]
            assert service["kind"] == "signed_out"
            assert service["state"] == "login_required"
            assert service["next_action"] == "Run uoft_login to sign in again."
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_read_failure_records_specific_kind_in_status(tmp_path):
    auth, browser = manager(tmp_path)
    browser.read_acorn = AsyncMock(
        side_effect=AcornError.from_status(describe(StatusKind.RATE_LIMITED, "acorn", "HTTP 429"))
    )
    try:
        with pytest.raises(AcornError):
            await auth.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
        service = auth.status()["services"]["acorn"]
        assert service["state"] == "unavailable"
        assert service["kind"] == "rate_limited"
        assert "slow down" in service["message"]
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_unexpected_backend_failure_is_classified_not_generic(tmp_path):
    auth, browser = manager(tmp_path)
    browser.read_degree_explorer = AsyncMock(side_effect=RuntimeError("getaddrinfo ENOTFOUND"))
    try:
        with pytest.raises(DegreeExplorerError) as error:
            await auth.read_degree_explorer(DegreeExplorerEndpoint.PLANNER)
        assert error.value.kind is StatusKind.NETWORK
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_login_timeout_reports_duo_pending(tmp_path):
    auth, browser = manager(tmp_path, login_timeout=0.05)
    browser.ready.clear()
    try:
        await auth.login("acorn")
        result = await auth.wait_for_login()
        assert result["login"]["state"] == "timed_out"
        assert "Duo sign-in pending or failed" in result["login"]["message"]
        service = result["services"]["acorn"]
        assert service["kind"] == "duo_pending"
        assert service["state"] == "login_required"
        assert "approve the Duo prompt" in service["next_action"]
    finally:
        await auth.close()


@pytest.mark.anyio
async def test_read_during_login_uses_login_in_progress_kind(tmp_path):
    auth, browser = manager(tmp_path)
    browser.ready.clear()
    await auth.login("acorn")
    await browser.opened.wait()
    try:
        with pytest.raises(AcornError) as error:
            await auth.read_acorn(AcornEndpoint.DASHBOARD_COURSES)
        assert error.value.kind is StatusKind.LOGIN_IN_PROGRESS
        assert error.value.state == "login_in_progress"
    finally:
        await auth.close()
