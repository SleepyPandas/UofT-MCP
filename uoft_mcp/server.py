"""Seven read-only MCP tools mapping directly to Timetable Builder endpoints."""

import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from uoft_mcp.client import TimetableAPIError, create_http_client, request_json

NonEmptyString = Annotated[str, Field(min_length=1, pattern=r"\S")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
SectionCode = Literal["F", "S", "Y"]
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


async def _request(
    ctx: Context[httpx.AsyncClient],
    method: str,
    path: str,
    *,
    params: dict[str, str | int | None] | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    """Expose the complete upstream JSON as one MCP text block, including arrays."""
    try:
        data = await request_json(
            ctx.request_context.lifespan_context, method, path, params=params, body=body
        )
    except TimetableAPIError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(data, ensure_ascii=False)


def create_server(transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    """Build a server; tests can substitute an httpx MockTransport for the public API."""

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[httpx.AsyncClient]:
        """Share one connection pool and close it when the MCP server stops."""
        async with create_http_client(transport) as client:
            yield client

    server = MCPServer(
        "UofT Timetable Builder",
        instructions=(
            "Look up current sessions and reference data before choosing filters. "
            "Tools return the public UofT timetable API's complete JSON as text. "
            "This server does not enroll students or generate schedules."
        ),
        lifespan=lifespan,
    )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_current_sessions(ctx: Context[httpx.AsyncClient]) -> str:
        """Get active academic sessions. Use non-header entries' values as session IDs."""
        return await _request(ctx, "GET", "current-session")

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_reference_data(ctx: Context[httpx.AsyncClient]) -> str:
        """Get division, campus, delivery-mode, and sorting reference values."""
        return await _request(ctx, "GET", "reference-data")

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def get_divisions(ctx: Context[httpx.AsyncClient]) -> str:
        """List faculty/division codes; use returned values rather than campus abbreviations."""
        return await _request(ctx, "GET", "getMatchingDivisions")

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def search_departments(
        ctx: Context[httpx.AsyncClient], term: NonEmptyString, divisions: NonEmptyString
    ) -> str:
        """Search departments by keyword and division code (for example computer, ARTSC)."""
        return await _request(
            ctx, "GET", "getMatchingDepartments", params={"term": term, "divisions": divisions}
        )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def search_course_titles(
        ctx: Context[httpx.AsyncClient],
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
        ctx: Context[httpx.AsyncClient],
        course_code: Annotated[str, Field(pattern=r"^[A-Za-z0-9]+$")],
        section_code: SectionCode | None = None,
    ) -> str:
        """Get a full course code's sections, meetings, rooms, and instructors.

        For example, course_code is CSC108H1. Optionally filter by F, S, or Y.
        This endpoint has no session parameter in the supplied API reference.
        """
        return await _request(
            ctx,
            "GET",
            f"getCoursesByCodeAndSectionCode/{quote(course_code, safe='')}",
            params={"sectionCode": section_code},
        )

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def search_courses(
        ctx: Context[httpx.AsyncClient],
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

    return server


def main() -> None:
    """Run local stdio; stdout is reserved exclusively for MCP protocol messages."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    create_server().run(transport="stdio")
