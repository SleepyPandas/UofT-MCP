"""Allowlisted ACORN reads using the existing authenticated API context."""

from enum import StrEnum
from typing import Any

from uoft_mcp.utilities.auth_browser import Service, classify_and_parse_response

BASE_URL = "https://acorn.utoronto.ca/sws/rest"


class AcornEndpoint(StrEnum):
    ELIGIBLE_REGISTRATIONS = "/enrolment/eligible-registrations"
    DASHBOARD_COURSES = "/dashboard/courseRegistration/enrolledCourses"
    STUDENT_REGISTRATION_INFO = "/profile/studentRegistrationInfo"

    @property
    def json_type(self) -> type:
        return list if self is AcornEndpoint.ELIGIBLE_REGISTRATIONS else dict


class AcornError(Exception):
    """Sanitized failure with an authentication status, never an upstream body."""

    def __init__(self, message: str, state: str = "unexpected_response"):
        super().__init__(message)
        self.state = state


async def request_acorn(api: Any, endpoint: AcornEndpoint) -> Any:
    """Perform exactly one GET; reject other routes, redirects, and non-JSON responses.

    No query parameters, retries, browser navigation, or student-record persistence.
    Dispose Playwright's retained response body even on failure or cancellation.
    """
    if not isinstance(endpoint, AcornEndpoint):
        raise AcornError("Unsupported ACORN endpoint.")
    response = None
    try:
        response = await api.get(
            BASE_URL + endpoint,
            headers={"Accept": "application/json"},
            timeout=30_000,
            max_redirects=0,
        )
        body = await response.text()
        # Each documented route has a fixed root type; never treat login HTML as data.
        service = Service("acorn", BASE_URL, BASE_URL + endpoint, endpoint.json_type)
        result, payload = classify_and_parse_response(
            service, response.status, response.headers, body
        )
        if result.state != "connected":
            raise AcornError(
                f"ACORN {endpoint} (HTTP {response.status}): {result.message}",
                result.state,
            )
        return payload
    except AcornError:
        raise
    except Exception:
        raise AcornError(
            f"Could not read ACORN {endpoint}; request failed or timed out "
            "(30-second timeout). Try again later.",
            "unavailable",
        ) from None
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass
