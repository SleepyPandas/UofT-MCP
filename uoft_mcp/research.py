"""Question-shaped course research tools: find_courses and course_info.

Both are read-only wrappers over the public Timetable Builder that return short,
readable JSON instead of the raw course objects, in every tool profile.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, Never
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from uoft_mcp.results import encode
from uoft_mcp.status import StatusKind, describe
from uoft_mcp.timetable_builder.client import TimetableAPIError, request_json
from uoft_mcp.timetable_builder.format import (
    UPSTREAM_PAGE_SIZE,
    UnexpectedShape,
    course_card,
    course_summary,
    current_session_codes,
    explicit_session_codes,
    extract_courses,
    page_window,
    session_label,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
CourseCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9]+$")]
SectionCode = Literal["F", "S", "Y"]
Campus = Literal["St. George", "Mississauga", "Scarborough"]
MAX_COURSE_CODES = 8


def _campus_filter(campus: Campus) -> str:
    """Map the friendly campus names onto the values getPageableCourses expects."""
    match campus:
        case "St. George":
            return "St. George"
        case "Mississauga":
            return "University of Toronto at Mississauga"
        case "Scarborough":
            return "Scarborough"
        case _:
            unused: Never = campus
            raise ToolError(f"Unsupported campus: {unused}")


def _page_changed(detail: str) -> ToolError:
    return ToolError(describe(StatusKind.PAGE_CHANGED, "timetable", detail).message)


async def _call(
    ctx: Context,
    method: str,
    path: str,
    *,
    params: dict[str, str | int | None] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """One bounded call to a fixed Timetable Builder path."""
    app = ctx.request_context.lifespan_context
    async with app.public_reads:
        return await request_json(app.timetable, method, path, params=params, body=body)


async def _fetch_course_cards(
    ctx: Context, code: str, section_code: SectionCode | None
) -> list[dict[str, Any]]:
    data = await _call(
        ctx,
        "GET",
        f"getCoursesByCodeAndSectionCode/{quote(code, safe='')}",
        params={"sectionCode": section_code},
    )
    try:
        courses, _ = extract_courses(data)
    except UnexpectedShape as exc:
        raise _page_changed(str(exc)) from exc
    return [course_card(course) for course in courses]


async def _course_outcome(
    ctx: Context, code: str, section_code: SectionCode | None
) -> list[dict[str, Any]] | str:
    """Cards for one code, or a plain-language reason there are none."""
    try:
        cards = await _fetch_course_cards(ctx, code, section_code)
    except TimetableAPIError as exc:
        if exc.kind == StatusKind.REQUEST_REJECTED:
            return f"No course found for {code}. Use the full code, for example CSC148H1."
        return str(exc)
    except ToolError as exc:
        return str(exc)
    if cards:
        return cards
    suffix = f" in section {section_code}" if section_code else ""
    return f"No offering of {code}{suffix} found."


async def _session_codes(ctx: Context, term: str | None) -> list[str]:
    if term is not None:
        explicit = explicit_session_codes(term)
        if explicit is not None:
            return explicit
    data = await _call(ctx, "GET", "current-session")
    try:
        return current_session_codes(data, term)
    except UnexpectedShape as exc:
        raise _page_changed(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def _search_body(
    keyword: str,
    sessions: list[str],
    campus: Campus | None,
    division: str | None,
    page: int,
) -> dict[str, Any]:
    return {
        # The keyword field only applies to title/code/description when the flag is set.
        "courseCodeAndTitleProps": {
            "courseCode": "",
            "courseTitle": keyword,
            "courseSectionCode": "",
            "searchCourseDescription": True,
        },
        "divisions": [division.upper()] if division else [],
        # The live API requires this empty filter even though the reference omits it.
        "departmentProps": [],
        "sessions": sessions,
        "campuses": [_campus_filter(campus)] if campus else [],
        "deliveryModes": [],
        "page": page,
        "pageSize": UPSTREAM_PAGE_SIZE,
        "direction": "asc",
    }


async def _search_page(
    ctx: Context, body: dict[str, Any]
) -> tuple[list[dict[str, Any]], int | None]:
    try:
        data = await _call(ctx, "POST", "getPageableCourses", body=body)
    except TimetableAPIError as exc:
        # The API answers HTTP 404 when nothing matches.
        if exc.kind == StatusKind.REQUEST_REJECTED:
            return [], 0
        raise
    try:
        rows, total = extract_courses(data)
    except UnexpectedShape as exc:
        raise _page_changed(str(exc)) from exc
    return list(rows), total


def register_research(server: MCPServer) -> None:
    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def find_courses(
        ctx: Context,
        query: Annotated[str, Field(min_length=1, pattern=r"\S")],
        term: Annotated[str | None, Field(min_length=1, pattern=r"\S")] = None,
        campus: Campus | None = None,
        division: Annotated[str | None, Field(pattern=r"^[A-Za-z]+$")] = None,
        page: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=UPSTREAM_PAGE_SIZE)] = 10,
    ) -> str:
        """Search courses by keyword and return short summaries, one per course offering.

        query matches course codes, titles, and descriptions (for example "machine
        learning", "MAT135", "CSC3"); each result's matched_in says which. term is a name
        like "Fall 2026" or "Winter 2027", or F, S, Y; omit it to search every current
        session. campus is St. George, Mississauga, or Scarborough; division is a code
        such as ARTSC, APSC, or FIS. Results are alphabetical by code. total counts
        offerings (a course taught in two terms counts twice); page starts at 1. Use
        course_info with a code for prerequisites, sections, and meeting times.
        """
        keyword = " ".join(query.split())
        try:
            sessions = await _session_codes(ctx, term)
            first_page, start = page_window(page, page_size)
            rows, total = await _search_page(
                ctx, _search_body(keyword, sessions, campus, division, first_page)
            )
            window = rows[start : start + page_size]
            offset = (page - 1) * page_size
            known_total = total if total is not None else offset + len(rows)
            if len(window) < page_size and offset + len(window) < known_total:
                more, _ = await _search_page(
                    ctx, _search_body(keyword, sessions, campus, division, first_page + 1)
                )
                window += more[: page_size - len(window)]
        except TimetableAPIError as exc:
            raise ToolError(str(exc)) from exc
        result: dict[str, Any] = {
            "query": keyword,
            "terms": [session_label(code) for code in sessions],
            "courses": [course_summary(course, keyword) for course in window],
            "total": known_total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + len(window) < known_total,
        }
        return encode(result)

    @server.tool(structured_output=False, annotations=READ_ONLY)
    async def course_info(
        ctx: Context,
        course_codes: Annotated[list[CourseCode], Field(min_length=1, max_length=MAX_COURSE_CODES)],
        section_code: SectionCode | None = None,
    ) -> str:
        """Get readable course cards: description, prerequisites, sections, and meetings.

        course_codes are full codes such as CSC148H1 (1 to 8). section_code optionally
        keeps only F (Fall), S (Winter), or Y (full year) offerings. Returns one card
        per offering with credits, breadth, plain-text requirements, and sections grouped
        by type with instructors, meeting times, enrolment, and waitlist. Sections
        flagged full or cancelled say so; lists are capped per type. The id is the Timetable
        Builder course id used by generate_timetable. Unknown codes appear under errors.
        """
        codes = list(dict.fromkeys(code.upper() for code in course_codes))
        outcomes = await asyncio.gather(
            *(_course_outcome(ctx, code, section_code) for code in codes)
        )
        courses: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for code, outcome in zip(codes, outcomes, strict=True):
            if isinstance(outcome, str):
                errors.append({"code": code, "error": outcome})
            else:
                courses.extend(outcome)
        if not courses:
            raise ToolError(" ".join(error["error"] for error in errors))
        result: dict[str, Any] = {"courses": courses}
        if errors:
            result["errors"] = errors
        return encode(result)
