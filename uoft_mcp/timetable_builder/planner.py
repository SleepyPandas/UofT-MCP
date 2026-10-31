"""One-call timetable planning: resolve course codes, solve with UofT's solver, share the result."""

import asyncio
import itertools
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Never
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from uoft_mcp.status import StatusKind, describe
from uoft_mcp.timetable_builder.client import (
    TTB_ORIGIN,
    TimetableAPIError,
    request_json,
    shorten_state,
)
from uoft_mcp.timetable_builder.format import is_full, is_open, seats
from uoft_mcp.timetable_builder.generation import (
    BlockedTime,
    GenerationCourse,
    GenerationPlan,
    NonEmptyString,
    SectionCode,
    TimePreference,
    blocked_off,
    fitness_option,
    generation_body,
)
from uoft_mcp.timetable_builder.sections import (
    compatible_combinations,
    pairing_problems,
    unchecked_restriction,
)
from uoft_mcp.timetable_builder.share_state import build_share_state

MAX_COURSES = 10
PLAN_TIMETABLE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False
)

# UTSC codes carry a letter in the fourth position (CSCA08H3); other campuses use digits.
_CODE_PATTERN = re.compile(r"([A-Z]{3}[A-Z\d]\d{2}[HY]\d)(?:[-_:\s]*([FSY]))?")
_FALL_WINTER_SESSION = re.compile(r"\d{4}[91]")
_ACTIVITY_ORDER = ("Lecture", "Tutorial", "Practical")
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
_ONLINE_LABELS = {"SYNC": "online", "ASYNC": "online, asynchronous"}
_HOUR_MS = 3_600_000
_MINUTE_MS = 60_000
_REVIEW_HINT = (
    "Try fewer or shorter blocked times, a different term for one of the courses, "
    "or dropping a course."
)


@dataclass(frozen=True)
class CourseRequest:
    code: str
    term: SectionCode | None


@dataclass(frozen=True)
class PlannedCourse:
    """The chosen offering of a requested course and the section types to fill."""

    offering: dict[str, Any]
    activity_types: list[str]

    @property
    def code(self) -> str:
        return self.offering["code"]


@dataclass(frozen=True)
class _Meeting:
    section: str
    day: int
    start: int
    end: int
    text: str


def _section_code(value: str) -> SectionCode:
    match value:
        case "F" | "S" | "Y":
            return value
        case _:
            raise ToolError(f"Term must be F, S or Y, not {value}.")


def _term_phase(term: SectionCode) -> Literal["fall", "winter"]:
    """Which solver plan a course joins; year-long courses are scheduled with the fall plan."""
    match term:
        case "F" | "Y":
            return "fall"
        case "S":
            return "winter"
        case _:
            unused: Never = term
            raise ToolError(f"Unsupported term: {unused}")


def parse_course_requests(
    course_codes: list[str], default_term: SectionCode | None
) -> list[CourseRequest]:
    """Normalize codes like ``csc148h1`` or ``CSC148H1-F``; a suffix overrides ``default_term``."""
    requests: list[CourseRequest] = []
    problems: list[str] = []
    for text in course_codes:
        found = _CODE_PATTERN.fullmatch(text.strip().upper())
        if found is None:
            problems.append(
                f"'{text}' isn't a full UofT course code. Use the form CSC148H1 "
                "(subject, number, H or Y, campus digit), optionally with a term such as "
                "CSC148H1-F."
            )
            continue
        code, suffix = found.groups()
        if any(request.code == code for request in requests):
            problems.append(f"{code} is listed more than once.")
            continue
        requests.append(CourseRequest(code, _section_code(suffix) if suffix else default_term))
    if problems:
        raise ToolError("\n".join(problems))
    return requests


def _session_name(session: str) -> str:
    match session[4:5]:
        case "9":
            return f"Fall {session[:4]}"
        case "1":
            return f"Winter {session[:4]}"
        case _:
            return session


def _offering_label(offering: dict[str, Any]) -> str:
    sessions = offering["sessions"]
    if len(sessions) == 2:
        return f"Fall-Winter {sessions[0][:4]}-{sessions[1][:4]}"
    return _session_name(sessions[0])


def _is_cancelled(item: dict[str, Any]) -> bool:
    return item.get("cancelInd") == "Y"


def _describe_options(offerings: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{offering['sectionCode']} ({_offering_label(offering)})"
        + (" - cancelled" if _is_cancelled(offering) else "")
        for offering in offerings
    )


def _fall_winter_offerings(courses: list[Any], code: str) -> list[dict[str, Any]]:
    return [
        course
        for course in courses
        if isinstance(course, dict)
        and str(course.get("code", "")).upper() == code
        and course.get("sectionCode") in {"F", "S", "Y"}
        and isinstance(course.get("sessions"), list)
        and course["sessions"]
        and all(_FALL_WINTER_SESSION.fullmatch(str(s)) for s in course["sessions"])
    ]


def choose_offering(request: CourseRequest, offerings: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the single live offering matching the requested term, or explain why not."""
    code = request.code
    wanted = request.term
    if wanted is None:
        matching = offerings
    else:
        matching = [o for o in offerings if o["sectionCode"] == wanted]
        if not matching and wanted in {"F", "S"}:
            # A year-long course occupies both terms, so it satisfies a fall or winter request.
            matching = [o for o in offerings if o["sectionCode"] == "Y"]
        if not matching:
            raise ToolError(
                f"{code} isn't offered for term {wanted}. Offered: {_describe_options(offerings)}."
            )
    live = [o for o in matching if not _is_cancelled(o)]
    if not live:
        raise ToolError(f"{code} is cancelled ({_describe_options(matching)}).")
    if len(live) > 1:
        raise ToolError(
            f"{code} is offered more than once: {_describe_options(live)}. Say which term you "
            f"want, for example {code}-{live[0]['sectionCode']} or term {live[0]['sectionCode']}."
        )
    return live[0]


def derive_activity_types(offering: dict[str, Any]) -> list[str]:
    """Section types to fill, from open sections that actually have a schedule."""
    types = {
        section["type"]
        for section in offering.get("sections") or []
        if isinstance(section.get("type"), str) and is_open(section)
    }
    if not types:
        raise ToolError(
            f"{offering['code']} has no scheduled sections yet (all are cancelled or TBA)."
        )
    return sorted(
        types,
        key=lambda t: (
            _ACTIVITY_ORDER.index(t) if t in _ACTIVITY_ORDER else len(_ACTIVITY_ORDER),
            t,
        ),
    )


async def _lookup(app: Any, request: CourseRequest) -> PlannedCourse:
    path = f"getCoursesByCodeAndSectionCode/{quote(request.code, safe='')}"
    async with app.public_reads:
        try:
            data = await request_json(app.timetable, "GET", path)
        except TimetableAPIError as exc:
            if exc.kind is StatusKind.REQUEST_REJECTED:
                raise ToolError(
                    f"I couldn't find a course with the code {request.code}. Check the code "
                    "(for example CSC148H1) or search for it first."
                ) from None
            raise ToolError(str(exc)) from exc
    try:
        courses = data["payload"]["pageableCourse"]["courses"]
    except (KeyError, TypeError):
        raise ToolError(
            describe(StatusKind.PAGE_CHANGED, "timetable", "unexpected course lookup shape").message
        ) from None
    if not isinstance(courses, list):
        raise ToolError(
            describe(StatusKind.PAGE_CHANGED, "timetable", "unexpected course lookup shape").message
        )
    offerings = _fall_winter_offerings(courses, request.code)
    if not offerings:
        raise ToolError(
            f"{request.code} has no Fall or Winter offering. It may only run in the summer "
            "session, which plan_timetable can't plan yet."
        )
    offering = choose_offering(request, offerings)
    return PlannedCourse(offering, derive_activity_types(offering))


async def resolve_courses(app: Any, requests: list[CourseRequest]) -> list[PlannedCourse]:
    """Look every course up concurrently and report all unusable codes in one message."""
    results = await asyncio.gather(*(_lookup(app, r) for r in requests), return_exceptions=True)
    problems = [str(r) for r in results if isinstance(r, ToolError)]
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, ToolError):
            raise result
    if problems:
        raise ToolError("\n".join(problems))
    return [r for r in results if isinstance(r, PlannedCourse)]


def _session_pair(session: str) -> tuple[str, str]:
    year = int(session[:4])
    if session[4] == "9":
        return session, f"{year + 1}1"
    return f"{year - 1}9", session


def academic_sessions(planned: list[PlannedCourse]) -> tuple[str, str]:
    """The (fall, winter) session codes shared by all courses, or an explanatory error."""
    pairs = {_session_pair(s) for p in planned for s in p.offering["sessions"]}
    if len(pairs) != 1:
        raise ToolError(
            "These courses belong to different academic years: "
            + "; ".join(f"{p.code} ({_offering_label(p.offering)})" for p in planned)
            + ". Pick offerings from one Fall-Winter year."
        )
    return next(iter(pairs))


def _clock(millis: int) -> str:
    return f"{millis // _HOUR_MS:02d}:{millis // _MINUTE_MS % 60:02d}"


def _meeting_time(day: int, start: int, end: int) -> str:
    return f"{_DAY_NAMES[day - 1]} {_clock(start)}-{_clock(end)}"


def _room(building: Any) -> str:
    if not isinstance(building, dict):
        return ""
    room = f"{building.get('buildingRoomNumber') or ''}{building.get('buildingRoomSuffix') or ''}"
    return " ".join(part for part in (building.get("buildingCode") or "", room) if part)


def _session_meetings(section: dict[str, Any], session: str) -> list[dict[str, Any]]:
    return sorted(
        (
            meeting
            for meeting in section.get("meetingTimes") or []
            if meeting.get("sessionCode", session) == session
        ),
        key=lambda m: (m["start"]["day"], m["start"]["millisofday"]),
    )


def _meeting_text(meeting: dict[str, Any]) -> str:
    start, end = meeting["start"], meeting["end"]
    text = _meeting_time(start["day"], start["millisofday"], end["millisofday"])
    room = _room(meeting.get("building"))
    return f"{text} {room}" if room else text


def _sorted_sections(course: dict[str, Any]) -> list[dict[str, Any]]:
    def key(section: dict[str, Any]) -> tuple[int, str]:
        kind = section.get("type")
        rank = _ACTIVITY_ORDER.index(kind) if kind in _ACTIVITY_ORDER else len(_ACTIVITY_ORDER)
        return rank, section.get("name", "")

    return sorted(course["sections"], key=key)


def _meetings(solved: list[dict[str, Any]], session: str) -> list[_Meeting]:
    found = []
    for course in solved:
        if session not in course["sessions"]:
            continue
        for section in course["sections"]:
            label = f"{course['code']} {section['name']}"
            for meeting in _session_meetings(section, session):
                if meeting.get("repetition", "WEEKLY") != "WEEKLY":
                    continue
                start, end = meeting["start"], meeting["end"]
                found.append(
                    _Meeting(
                        label,
                        start["day"],
                        start["millisofday"],
                        end["millisofday"],
                        _meeting_text(meeting),
                    )
                )
    return found


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _clash(a: _Meeting, b: _Meeting) -> bool:
    return a.section != b.section and a.day == b.day and _overlaps(a.start, a.end, b.start, b.end)


def _blocked_hits(
    meetings: list[_Meeting], intervals: list[dict[str, Any]]
) -> list[tuple[_Meeting, int, int, int]]:
    """Each meeting inside a blocked interval, with that interval's day, start and end."""
    hits = []
    for meeting in meetings:
        for interval in intervals:
            day = interval["start"]["day"]
            start, end = interval["start"]["millisofday"], interval["end"]["millisofday"]
            if meeting.day == day and _overlaps(meeting.start, meeting.end, start, end):
                hits.append((meeting, day, start, end))
    return hits


def check_schedule(
    sessions: tuple[str, str],
    planned: list[PlannedCourse],
    solved: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
) -> list[str]:
    """Raise when the solver output is unusable; return warnings about broken blocked times.

    The solver treats blocked times as preferences and returns its best effort, and it
    ignores which sections a section is restricted to, so missing sections, overlapping
    meetings and mismatched section pairs are detected here.
    """
    chosen = {
        course["code"]: {section.get("type") for section in course["sections"]} for course in solved
    }
    problems = [
        problem
        for course in solved
        for problem in pairing_problems(course["code"], course["sections"])
    ]
    for course in planned:
        missing = [t for t in course.activity_types if t not in chosen.get(course.code, set())]
        if missing:
            problems.append(
                f"no {'/'.join(t.lower() for t in missing)} section of {course.code} fits"
            )
    warnings = []
    for session in sessions:
        meetings = _meetings(solved, session)
        for a, b in itertools.combinations(meetings, 2):
            if _clash(a, b):
                problems.append(f"{a.section} ({a.text}) overlaps {b.section} ({b.text})")
        for meeting, day, start, end in _blocked_hits(meetings, intervals):
            warnings.append(
                f"{meeting.section} ({meeting.text}) falls inside your blocked time "
                f"{_meeting_time(day, start, end)}; the solver could not avoid it."
            )
    if problems:
        raise ToolError(
            "I couldn't find a schedule without conflicts: "
            + "; ".join(dict.fromkeys(problems))
            + f". {_REVIEW_HINT}"
        )
    return list(dict.fromkeys(warnings))


def _section_names(sections: list[dict[str, Any]]) -> str:
    return " + ".join(section["name"] for section in sorted(sections, key=lambda s: s["name"]))


def repair_pairings(
    sessions: tuple[str, str],
    planned: list[PlannedCourse],
    solved: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace sections the solver paired against the course's restrictions.

    The solver ignores section links, so for each offending course this picks the best
    allowed combination that clashes with nothing else in the plan: fewest broken blocked
    times first, then keeping the solver's sections (earliest type first), then fewest
    full sections.
    A course with no such combination is left alone for ``check_schedule`` to report.
    """
    by_code = {p.code: p for p in planned}
    repaired = list(solved)
    notes = []
    for index, course in enumerate(repaired):
        target = by_code.get(course["code"])
        if target is None or not pairing_problems(course["code"], course["sections"]):
            continue
        others = [c for i, c in enumerate(repaired) if i != index]
        fixed = {session: _meetings(others, session) for session in sessions}
        solver_names = {section["name"] for section in course["sections"]}
        best: tuple[tuple[int, tuple[bool, ...], int], list[dict[str, Any]]] | None = None
        for combination in compatible_combinations(
            target.offering["sections"], target.activity_types
        ):
            trial = [{**course, "sections": combination}]
            meetings = {session: _meetings(trial, session) for session in sessions}
            if any(
                _clash(a, b)
                for session in sessions
                for a, b in itertools.chain(
                    itertools.product(meetings[session], fixed[session]),
                    itertools.combinations(meetings[session], 2),
                )
            ):
                continue
            score = (
                sum(len(_blocked_hits(meetings[session], intervals)) for session in sessions),
                tuple(section["name"] not in solver_names for section in combination),
                sum(is_full(section) for section in combination),
            )
            if best is None or score < best[0]:
                best = (score, combination)
        if best is None:
            continue
        solver_sections = {section["name"]: section for section in course["sections"]}
        sections = [solver_sections.get(s["name"], s) for s in best[1]]
        notes.append(
            f"{course['code']}: the solver picked {_section_names(course['sections'])}, which the "
            f"course doesn't allow together; used {_section_names(sections)} instead."
        )
        repaired[index] = {**course, "sections": sections}
    return repaired, notes


def restriction_warnings(planned: list[PlannedCourse]) -> list[str]:
    """Course-note enrolment instructions that could not be checked against section data."""
    warnings = []
    for course in planned:
        text = unchecked_restriction(course.offering)
        if text:
            warnings.append(
                f"{course.code} has enrolment instructions this tool can't check; read them "
                f"before registering: {text}"
            )
    return warnings


def seat_warnings(solved: list[dict[str, Any]]) -> list[str]:
    """Name every chosen section that is already full; the solver ignores seat counts."""
    warnings = [
        f"{course['code']} {section['name']} has no open seats ({seats(section)})"
        for course in solved
        for section in course["sections"]
        if is_full(section)
    ]
    if warnings:
        warnings.append("Seat counts are a snapshot and change quickly; check ACORN to register.")
    return warnings


def _year_long_blocks(solved: list[dict[str, Any]], winter_session: str) -> list[BlockedTime]:
    """Winter meetings of year-long courses, so the winter solve cannot overlap them."""
    blocks = []
    for course in solved:
        if course.get("sectionCode") != "Y":
            continue
        for section in course["sections"]:
            for meeting in _session_meetings(section, winter_session):
                day = meeting["start"]["day"]
                if 1 <= day <= len(_WEEKDAYS):
                    blocks.append(
                        BlockedTime(
                            day=_WEEKDAYS[day - 1],
                            start=_clock(meeting["start"]["millisofday"]),
                            end=_clock(meeting["end"]["millisofday"]),
                        )
                    )
    return blocks


async def _generate(
    app: Any,
    courses: list[PlannedCourse],
    preference: TimePreference,
    blocked_times: list[BlockedTime],
) -> list[dict[str, Any]]:
    plan = GenerationPlan(
        courses=[
            GenerationCourse(course_id=c.offering["id"], activity_types=c.activity_types)
            for c in courses
        ],
        preference=preference,
        blocked_times=blocked_times or None,
    )
    try:
        data = await request_json(
            app.timetable, "POST", "generateYear", body=generation_body([plan])
        )
    except TimetableAPIError as exc:
        raise ToolError(str(exc)) from exc
    if not isinstance(data, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("courses"), list) for item in data
    ):
        raise ToolError(
            describe(StatusKind.PAGE_CHANGED, "timetable", "unexpected generateYear shape").message
        )
    return [course for item in data for course in item["courses"]]


async def solve(
    app: Any,
    planned: list[PlannedCourse],
    sessions: tuple[str, str],
    preference: TimePreference,
    blocked_times: list[BlockedTime],
) -> list[dict[str, Any]]:
    """Solve the fall plan (including year-long courses), then the winter plan.

    ``generateYear`` plans are positional and independent, and year-long courses must sit in
    only one of them or the solver picks different sections per term. They go in the fall
    plan, and their winter meetings are blocked off for the winter plan.
    """
    phases = [_term_phase(_section_code(p.offering["sectionCode"])) for p in planned]
    fall = [p for p, phase in zip(planned, phases, strict=True) if phase == "fall"]
    winter = [p for p, phase in zip(planned, phases, strict=True) if phase == "winter"]
    solved: list[dict[str, Any]] = []
    carried: list[BlockedTime] = []
    if fall:
        solved += await _generate(app, fall, preference, blocked_times)
        carried = _year_long_blocks(solved, sessions[1])
    if winter:
        solved += await _generate(app, winter, preference, [*blocked_times, *carried])
    return solved


def _instructors(section: dict[str, Any]) -> str:
    names = dict.fromkeys(
        " ".join(filter(None, (i.get("firstName"), i.get("lastName"))))
        for i in section.get("instructors") or []
    )
    return ", ".join(name for name in names if name)


def _section_line(section: dict[str, Any], session: str) -> str:
    meetings = _session_meetings(section, session)
    modes = {
        m.get("mode") for m in section.get("deliveryModes") or [] if m.get("session") == session
    }
    online = next((label for mode, label in _ONLINE_LABELS.items() if mode in modes), "")
    if meetings:
        when = "; ".join(_meeting_text(m) for m in meetings)
    else:
        when = "online, asynchronous" if online.endswith("asynchronous") else "time to be announced"
    extras = [
        e for e in (online if meetings else "", _instructors(section), seats(section) or "") if e
    ]
    suffix = f" ({'; '.join(extras)})" if extras else ""
    return f"  {section['type']} {section['name']}: {when}{suffix}"


def _weekly_overview(solved: list[dict[str, Any]], session: str) -> list[str]:
    by_day: dict[int, list[tuple[int, str]]] = {}
    for meeting in _meetings(solved, session):
        by_day.setdefault(meeting.day, []).append(
            (meeting.start, f"{_clock(meeting.start)}-{_clock(meeting.end)} {meeting.section}")
        )
    days = sorted({*range(1, 6), *by_day})
    return [
        f"  {_DAY_NAMES[day - 1]}: "
        + ("; ".join(text for _, text in sorted(by_day[day])) if day in by_day else "free")
        for day in days
    ]


def render_schedule(
    sessions: tuple[str, str],
    planned: list[PlannedCourse],
    solved: list[dict[str, Any]],
    preference: TimePreference,
) -> str:
    order = {p.code: index for index, p in enumerate(planned)}
    ordered = sorted(solved, key=lambda c: order.get(c["code"], len(order)))
    lines = []
    for session in sessions:
        in_term = [c for c in ordered if session in c["sessions"]]
        if not in_term:
            continue
        lines.append(f"{_session_name(session)} ({preference} schedule)")
        for course in in_term:
            year_long = " [full year]" if course.get("sectionCode") == "Y" else ""
            lines.append(f"{course['code']}{year_long} - {course.get('name', '')}")
            lines.extend(_section_line(s, session) for s in _sorted_sections(course))
        lines.append(f"Weekly overview, {_session_name(session)}:")
        lines.extend(_weekly_overview(in_term, session))
        lines.append("")
    return "\n".join(lines).rstrip()


async def _share(
    app: Any,
    sessions: tuple[str, str],
    planned: list[PlannedCourse],
    solved: list[dict[str, Any]],
    preference: TimePreference,
    intervals: list[dict[str, Any]],
) -> str:
    offerings = [p.offering for p in planned]
    planned_sessions = {s for offering in offerings for s in offering["sessions"]}
    state = build_share_state(
        list(sessions),
        offerings,
        solved,
        intervals,
        fitness_option(preference),
        planned_sessions,
    )
    try:
        data = await shorten_state(app.timetable, state)
    except TimetableAPIError as exc:
        return f"Share link unavailable: {exc}"
    return f"share_url: {TTB_ORIGIN}/#!/?t={data['id']}"


def register_planner(server: MCPServer) -> None:
    """Register ``plan_timetable``, a direct tool available in every profile."""

    @server.tool(structured_output=False, annotations=PLAN_TIMETABLE)
    async def plan_timetable(
        ctx: Context,
        course_codes: Annotated[list[NonEmptyString], Field(min_length=1, max_length=MAX_COURSES)],
        term: SectionCode | None = None,
        preference: TimePreference = "balanced",
        blocked_times: list[BlockedTime] | None = None,
        share: bool = True,
    ) -> str:
        """Plan a conflict-free timetable from course codes in one call and share it.

        course_codes are full codes such as CSC148H1; add a term suffix (CSC148H1-F) when a
        course runs in more than one term, or set term to F, S or Y for all of them. The tool
        finds each course's offering and section types, asks UofT's solver for lectures,
        tutorials and practicals, and returns the chosen meeting times with a weekly overview.
        preference is early, balanced or late. blocked_times use weekday names and 24-hour
        HH:MM times and are preferences the solver may not be able to honour; any broken
        ones are reported. With share=True (default) it creates a public anonymous
        ttb.utoronto.ca link (share_url). It does not enrol anyone.
        """
        requests = parse_course_requests(course_codes, term)
        blocked = blocked_times or []
        intervals = blocked_off(blocked)
        app = ctx.request_context.lifespan_context
        planned = await resolve_courses(app, requests)
        sessions = academic_sessions(planned)
        solved = await solve(app, planned, sessions, preference, blocked)
        solved, adjustments = repair_pairings(sessions, planned, solved, intervals)
        warnings = [
            *adjustments,
            *check_schedule(sessions, planned, solved, intervals),
            *restriction_warnings(planned),
            *seat_warnings(solved),
        ]
        lines = [render_schedule(sessions, planned, solved, preference)]
        if warnings:
            lines += ["", "Warnings:", *(f"- {w}" for w in warnings)]
        if share:
            lines += ["", await _share(app, sessions, planned, solved, preference, intervals)]
        lines += ["", "This is a plan only; register for sections on ACORN."]
        return "\n".join(lines)
