"""Public timetable tools, read-only Degree Explorer, and UofT login over stdio."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Never
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from uoft_mcp.degree_explorer.client import DegreeExplorerEndpoint, DegreeExplorerError
from uoft_mcp.timetable_builder.client import TimetableAPIError, create_http_client, request_json
from uoft_mcp.utilities.auth import AuthManager
from uoft_mcp.utilities.auth_browser import ServiceChoice

NonEmptyString = Annotated[str, Field(min_length=1, pattern=r"\S")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
SectionCode = Literal["F", "S", "Y"]
TimePreference = Literal["early", "balanced", "late"]
Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ClockTime = Annotated[str, Field(pattern=r"^\d{1,2}:\d{2}$")]
ObjectId = Annotated[str, Field(pattern=r"^[A-Za-z0-9]+$")]
ShareId = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9]+$")]
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
DEGREE_EXPLORER_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
SAVE_SHARE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
TTB_ORIGIN = "https://ttb.utoronto.ca"
TIMETABLE_STATE_KEYS = frozenset({"sessions", "timetables", "plans"})


class BlockedTime(BaseModel):
    """A weekday interval the solver should leave empty."""

    day: Weekday
    start: ClockTime
    end: ClockTime


class GenerationCourse(BaseModel):
    """One course the solver should schedule, identified by Timetable Builder id."""

    course_id: ObjectId
    activity_types: Annotated[list[NonEmptyString], Field(min_length=1)]


class GenerationPlan(BaseModel):
    """One term's courses, time preference, and optional blocked intervals."""

    courses: Annotated[list[GenerationCourse], Field(min_length=1)]
    preference: TimePreference = "balanced"
    blocked_times: list[BlockedTime] | None = None


async def _degree_explorer(ctx: Context[AppContext], endpoint: DegreeExplorerEndpoint) -> str:
    """Return complete upstream JSON as MCP text; surface sanitized tool errors."""
    try:
        data = await ctx.request_context.lifespan_context.auth.read_degree_explorer(endpoint)
    except DegreeExplorerError as exc:
        raise ToolError(str(exc)) from None
    return json.dumps(data, ensure_ascii=False)


async def _request(
    ctx: Context[AppContext],
    method: str,
    path: str,
    *,
    params: dict[str, str | int | None] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    content: str | bytes | None = None,
    content_type: str | None = None,
) -> str:
    """Expose the complete upstream JSON as one MCP text block, including arrays."""
    data = await _request_data(
        ctx,
        method,
        path,
        params=params,
        body=body,
        content=content,
        content_type=content_type,
    )
    return json.dumps(data, ensure_ascii=False)


async def _request_data(
    ctx: Context[AppContext],
    method: str,
    path: str,
    *,
    params: dict[str, str | int | None] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    content: str | bytes | None = None,
    content_type: str | None = None,
) -> Any:
    """Call one fixed Timetable Builder path and return the parsed JSON."""
    try:
        return await request_json(
            ctx.request_context.lifespan_context.timetable,
            method,
            path,
            params=params,
            body=body,
            content=content,
            content_type=content_type,
        )
    except TimetableAPIError as exc:
        raise ToolError(str(exc)) from exc


def _fitness_option(preference: TimePreference) -> str:
    """Map the public preference names to the solver's fitnessFunctionOption values."""
    match preference:
        case "early":
            return "MORNING_WEIGHTED"
        case "balanced":
            return "BALANCED"
        case "late":
            return "AFTERNOON_WEIGHTED"
        case _:
            unused: Never = preference
            raise ToolError(f"Unsupported preference: {unused}")


def _weekday_number(day: Weekday) -> int:
    """Map Timetable Builder weekday names onto the solver's 1-5 day numbers."""
    match day:
        case "Monday":
            return 1
        case "Tuesday":
            return 2
        case "Wednesday":
            return 3
        case "Thursday":
            return 4
        case "Friday":
            return 5
        case _:
            unused: Never = day
            raise ToolError(f"Unsupported weekday: {unused}")


def _millis_of_day(clock: str) -> int:
    """Convert a 24-hour HH:MM clock time into the API's milliseconds-since-midnight value."""
    hours_text, minutes_text = clock.split(":")
    hours = int(hours_text)
    minutes = int(minutes_text)
    if hours > 23 or minutes > 59:
        raise ToolError(f"Invalid time {clock}; use HH:MM on a 24-hour clock.")
    return (hours * 3600 + minutes * 60) * 1000


def _blocked_off(blocked_times: list[BlockedTime] | None) -> list[dict[str, Any]]:
    """Convert optional blocked clock ranges into the generateYear blockedOff objects."""
    intervals: list[dict[str, Any]] = []
    for blocked in blocked_times or []:
        start = _millis_of_day(blocked.start)
        end = _millis_of_day(blocked.end)
        if start >= end:
            raise ToolError(
                f"{blocked.day} blocked time {blocked.start} must be earlier than {blocked.end}."
            )
        day = _weekday_number(blocked.day)
        intervals.append(
            {
                "start": {"day": day, "millisofday": start},
                "end": {"day": day, "millisofday": end},
            }
        )
    return intervals


def _generation_body(plans: list[GenerationPlan]) -> list[dict[str, Any]]:
    """Build the verified generateYear request from validated MCP arguments."""
    return [
        {
            "courses": [
                {
                    "id": course.course_id,
                    "sections": [
                        {"name": "*", "type": activity_type}
                        for activity_type in course.activity_types
                    ],
                }
                for course in plan.courses
            ],
            "fitnessFunctionOption": _fitness_option(plan.preference),
            "blockedOff": _blocked_off(plan.blocked_times),
        }
        for plan in plans
    ]


@dataclass
class AppContext:
    timetable: httpx.AsyncClient
    auth: AuthManager


def create_server(
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    auth_factory: Callable[[], AuthManager] = AuthManager,
) -> MCPServer:
    """Build a server; tests can substitute an httpx MockTransport for the public API."""

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        """Share one connection pool and close it when the MCP server stops."""
        async with create_http_client(transport) as client:
            auth = auth_factory()
            try:
                yield AppContext(client, auth)
            finally:
                await auth.close()

    server = MCPServer(
        "UofT MCP",
        instructions=(
            "Look up current sessions and reference data before choosing filters. "
            "Course details include the course id needed by generate_timetable. "
            "Data tools return complete upstream JSON as text. "
            "This server does not enroll students. "
            "Use uoft_login to connect Degree Explorer or ACORN in an official browser window. "
            "Never ask for credentials or Duo codes in chat. Login returns immediately; "
            "check uoft_auth_status for progress, waiting a few seconds between checks. "
            "Only retry login on user request. uoft_forget_session removes local saved access. "
            "degree_explorer_* tools only read the connected student's Degree Explorer data. "
            "They reuse saved access without opening a browser and accept no arguments. "
            "Choose academic_history for courses/marks, student_data or student_record for "
            "Current Status, and planner for existing timelines. Fetch only the data needed. "
            "Cell selectors are undocumented; get_cell_details cannot select a specific cell. "
            "No Degree Explorer writes, enrolment changes, or ACORN data tools are available."
        ),
        lifespan=lifespan,
    )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_current_sessions(ctx: Context[AppContext]) -> str:
        """Get active academic sessions. Use non-header entries' values as session IDs."""
        return await _request(ctx, "GET", "current-session")

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_reference_data(ctx: Context[AppContext]) -> str:
        """Get division, campus, delivery-mode, and sorting reference values."""
        return await _request(ctx, "GET", "reference-data")

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_divisions(ctx: Context[AppContext]) -> str:
        """List faculty/division codes; use returned values rather than campus abbreviations."""
        return await _request(ctx, "GET", "getMatchingDivisions")

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def search_departments(
        ctx: Context[AppContext], term: NonEmptyString, divisions: NonEmptyString
    ) -> str:
        """Search departments by keyword and division code (for example computer, ARTSC)."""
        return await _request(
            ctx, "GET", "getMatchingDepartments", params={"term": term, "divisions": divisions}
        )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def search_course_titles(
        ctx: Context[AppContext],
        term: NonEmptyString,
        divisions: NonEmptyString,
        sessions: NonEmptyString,
        lower_threshold: NonNegativeInt = 50,
        upper_threshold: NonNegativeInt = 200,
    ) -> str:
        """Autocomplete a course code/title using a division code and a current session ID.

        Thresholds are upstream autocomplete tuning parameters, not pagination.
        """
        if lower_threshold > upper_threshold:
            raise ToolError("lower_threshold must be less than or equal to upper_threshold.")
        return await _request(
            ctx,
            "GET",
            "getOptimizedMatchingCourseTitles",
            params={
                "term": term,
                "divisions": divisions,
                "sessions": sessions,
                "lowerThreshold": lower_threshold,
                "upperThreshold": upper_threshold,
            },
        )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_course_details(
        ctx: Context[AppContext],
        course_code: Annotated[str, Field(pattern=r"^[A-Za-z0-9]+$")],
        section_code: SectionCode | None = None,
    ) -> str:
        """Get a full course code's sections, meetings, rooms, and instructors.

        For example, course_code is CSC108H1. Optionally filter by F, S, or Y.
        This endpoint has no session parameter in the supplied API reference.
        Use the returned course id with generate_timetable.
        """
        return await _request(
            ctx,
            "GET",
            f"getCoursesByCodeAndSectionCode/{quote(course_code, safe='')}",
            params={"sectionCode": section_code},
        )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def search_courses(
        ctx: Context[AppContext],
        course_code: str = "",
        course_title: str = "",
        course_section_code: SectionCode | None = None,
        search_course_description: bool = False,
        divisions: list[NonEmptyString] | None = None,
        sessions: list[NonEmptyString] | None = None,
        campuses: list[NonEmptyString] | None = None,
        delivery_modes: list[NonEmptyString] | None = None,
        page: PositiveInt = 1,
        page_size: PositiveInt = 20,
        direction: Literal["asc", "desc"] = "asc",
    ) -> str:
        """Search one page of courses using optional code/title and reference-code filters.

        Page numbering starts at one; page_size defaults to 20. Get session IDs
        from get_current_sessions and other filter codes from get_reference_data.
        Use an exact course_code from autocomplete; use course_title for keywords.
        Sorting defaults to asc. No automatic pagination is performed.
        """
        body = {
            "courseCodeAndTitleProps": {
                "courseCode": course_code,
                "courseTitle": course_title,
                "courseSectionCode": course_section_code or "",
                "searchCourseDescription": search_course_description,
            },
            "divisions": divisions or [],
            # The live API requires this empty filter even though the reference omits it.
            "departmentProps": [],
            "sessions": sessions or [],
            "campuses": campuses or [],
            "deliveryModes": delivery_modes or [],
            "page": page,
            "pageSize": page_size,
            "direction": direction,
        }
        return await _request(ctx, "POST", "getPageableCourses", body=body)

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def generate_timetable(
        ctx: Context[AppContext],
        plans: Annotated[list[GenerationPlan], Field(min_length=1)],
    ) -> str:
        """Ask UofT's solver for conflict-free lecture, tutorial, and practical sections.

        Each plan is one term. course_id values come from get_course_details.
        activity_types are the section types to fill, such as Lecture, Tutorial,
        or Practical. preference may be early, balanced, or late. Optional
        blocked_times use weekday names and 24-hour HH:MM clock times. This does
        not enroll students or write an ACORN timetable.
        """
        return await _request(ctx, "POST", "generateYear", body=_generation_body(plans))

    @server.tool(structured_output=False, annotations=SAVE_SHARE)
    async def save_timetable(ctx: Context[AppContext], timetable: dict[str, Any]) -> str:
        """Store a serialized Timetable Builder state and return a public share URL.

        timetable must be the TTB state object with sessions, timetables, and
        plans. The wrapper posts the official ttb.utoronto.ca URL for that state
        and returns the upstream share id plus a https://ttb.utoronto.ca/#!/?t=
        link. This creates an anonymous share record only; it does not enroll
        students.
        """
        missing = TIMETABLE_STATE_KEYS.difference(timetable)
        if missing:
            names = ", ".join(sorted(missing))
            raise ToolError(f"timetable is missing required keys: {names}.")
        encoded = quote(
            f"{TTB_ORIGIN}/#!/?{json.dumps(timetable, ensure_ascii=False, separators=(',', ':'))}",
            safe="!~*'()",
        )
        data = await _request_data(
            ctx, "POST", "tiny/shorten", content=encoded, content_type="text/plain"
        )
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise ToolError("UofT timetable API did not return a share id at tiny/shorten.")
        return json.dumps(
            {**data, "share_url": f"{TTB_ORIGIN}/#!/?t={data['id']}"},
            ensure_ascii=False,
        )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def retrieve_timetable(ctx: Context[AppContext], share_id: ShareId) -> str:
        """Load a previously saved Timetable Builder state by its public share id."""
        return await _request(ctx, "GET", "tiny/retrieve", params={"id": share_id})

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_academic_history(ctx: Context[AppContext]) -> str:
        """Read the connected student's course history, sessions, marks, and requirement data.

        Use for completed coursework and academic-history questions. No arguments or filters;
        returns complete upstream JSON as text, including arrays and status fields.
        Requires Degree Explorer login via uoft_login; reuses saved access without a browser.
        Performs one GET with no retries and reports authentication/API failures as tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.ACADEMIC_HISTORY)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_student_data(ctx: Context[AppContext]) -> str:
        """Read the connected student's payload used by Degree Explorer's Current Status page.

        Use for current academic status; use degree_explorer_get_academic_history for marks.
        No arguments. Returns complete upstream JSON as text; field meanings follow the API.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.STUDENT_DATA)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_student_record(ctx: Context[AppContext]) -> str:
        """Read the connected student's record payload used by Degree Explorer Current Status.

        Use when the underlying record is needed beyond degree_explorer_get_student_data.
        This is not a certified transcript. No arguments; complete upstream JSON as text.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.STUDENT_RECORD)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_student_user_data(ctx: Context[AppContext]) -> str:
        """Read the connected student's Degree Explorer menu/session user metadata.

        Use for app-shell user context, not course history; use uoft_auth_status to check login.
        No arguments. Returns complete upstream JSON as text and may include student identity.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.STUDENT_USER_DATA)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_student_menu(ctx: Context[AppContext]) -> str:
        """Read the connected student's available Degree Explorer navigation entries.

        Use to inspect app navigation, not degree completion. No arguments.
        Returns complete upstream JSON as text; the observed menu root is an array.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.STUDENT_MENU)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_messages(ctx: Context[AppContext]) -> str:
        """Read Degree Explorer's UI string catalog, not a student inbox or correspondence.

        Use to interpret interface labels or message keys returned by other Degree Explorer
        tools. No arguments or search filter. Returns complete upstream JSON as text.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.MESSAGES)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_session_timeouts(ctx: Context[AppContext]) -> str:
        """Read Degree Explorer's web-client timeout settings; this does not extend a session.

        Use for timeout configuration, not remaining login lifetime or proof of authentication.
        Use uoft_auth_status for connection status. No arguments; complete JSON returned as text.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.SESSION_TIMEOUTS)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_planner(ctx: Context[AppContext]) -> str:
        """Read the connected student's existing planner timelines and primary-plan flags.

        Use to inspect saved plans. Does not create, edit, select, or evaluate a plan.
        No arguments or plan filter. Returns complete upstream JSON as text.
        Requires Degree Explorer login via uoft_login. Reuses saved access without a browser;
        one GET, no retries, and authentication/API failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.PLANNER)

    @server.tool(structured_output=False, annotations=DEGREE_EXPLORER_READ)
    async def degree_explorer_get_cell_details(ctx: Context[AppContext]) -> str:
        """Read the planner cell-popup endpoint with no arguments, as documented in the registry.

        Cell selectors are undocumented: this cannot target a particular timeline or cell.
        Use degree_explorer_get_planner first. May be empty or fail if upstream needs UI context.
        Returns complete upstream JSON as text. Requires Degree Explorer login via uoft_login.
        Reuses saved access without a browser; one GET, no retries; failures become tool errors.
        """
        return await _degree_explorer(ctx, DegreeExplorerEndpoint.CELL_DETAILS)

    @server.tool(
        structured_output=False,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False
        ),
    )
    async def uoft_login(
        ctx: Context[AppContext], service: ServiceChoice = "both", remember: bool = True
    ) -> str:
        """Start official UofT browser login, returning immediately while you complete Duo.

        service is degree_explorer, acorn, or both. Saved sessions are reused when valid.
        Newly verified browser sessions wait five seconds, recheck access, and save the
        latest session before continuing or closing. Status stays in_progress during the wait.
        remember=False starts a fresh memory-only session without deleting saved sessions.
        Never supply passwords or MFA codes through tools. Check uoft_auth_status for progress.
        """
        result = await ctx.request_context.lifespan_context.auth.login(service, remember)
        return json.dumps(result, ensure_ascii=False)

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def uoft_auth_status(ctx: Context[AppContext], refresh: bool = False) -> str:
        """Get connection and login progress without exposing credentials or student records.

        refresh=True checks both services using saved cookies; it never opens a login window.
        A saved cookie is not proof of a valid session. last_verified is historical, not expiry.
        """
        auth = ctx.request_context.lifespan_context.auth
        result = await auth.refresh() if refresh else auth.status()
        return json.dumps(result, ensure_ascii=False)

    @server.tool(
        structured_output=False,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True
        ),
    )
    async def uoft_forget_session(ctx: Context[AppContext]) -> str:
        """Cancel login and delete this MCP's local sessions and encryption key for both apps.

        This closes managed authentication resources. It is not university-wide logout.
        """
        result = await ctx.request_context.lifespan_context.auth.forget()
        return json.dumps(result, ensure_ascii=False)

    return server


def main() -> None:
    """Run stdio by default, or explicit auth commands through the terminal interface."""
    from uoft_mcp.cli import main as cli_main

    cli_main()
