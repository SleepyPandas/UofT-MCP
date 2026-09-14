"""Allowlisted Degree Explorer reads using the existing authenticated API context."""

from enum import StrEnum
from typing import Any

from uoft_mcp.utilities.auth_browser import Service, classify_and_parse_response

BASE_URL = "https://degreeexplorer.utoronto.ca/degreeExplorer/rest"


class DegreeExplorerEndpoint(StrEnum):
    ACADEMIC_HISTORY = "/dxStudent/getAcademicHistory"
    STUDENT_DATA = "/dxStudent/getStudentData"
    STUDENT_RECORD = "/dxStudent/getStudentRecord"
    STUDENT_USER_DATA = "/dxMenu/getStudentUserData"
    STUDENT_MENU = "/dxMenu/getStudentMenu"
    MESSAGES = "/messages/getMessages"
    SESSION_TIMEOUTS = "/dxMenu/getSessionTimeouts"
    PLANNER = "/dxPlanner/getPlanner"
    CELL_DETAILS = "/dxPlanner/getCellDetails"


class DegreeExplorerError(Exception):
    """Sanitized failure with an authentication status, never an upstream body."""

    def __init__(self, message: str, state: str = "unexpected_response"):
        super().__init__(message)
        self.state = state


async def request_degree_explorer(api: Any, endpoint: DegreeExplorerEndpoint) -> Any:
    """Perform exactly one GET; reject other routes, redirects, and non-JSON responses.

    No query parameters, retries, browser navigation, or student-record persistence.
    Dispose Playwright's retained response body even on failure or cancellation.
    """
    if not isinstance(endpoint, DegreeExplorerEndpoint):
        raise DegreeExplorerError("Unsupported Degree Explorer endpoint.")
    response = None
    try:
        response = await api.get(
            BASE_URL + endpoint,
            headers={"Accept": "application/json"},
            timeout=30_000,
            max_redirects=0,
        )
        body = await response.text()
        # Unlike the authentication probe, reads may return any valid JSON root.
        service = Service("degree_explorer", BASE_URL, BASE_URL + endpoint, object)
        result, payload = classify_and_parse_response(
            service, response.status, response.headers, body
        )
        if result.state != "connected":
            raise DegreeExplorerError(
                f"Degree Explorer {endpoint} (HTTP {response.status}): {result.message}",
                result.state,
            )
        return payload
    except DegreeExplorerError:
        raise
    except Exception:
        raise DegreeExplorerError(
            f"Could not read Degree Explorer {endpoint}; request failed or timed out "
            "(30-second timeout). Try again later.",
            "unavailable",
        ) from None
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass
