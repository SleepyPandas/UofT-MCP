"""Build the state object the Timetable Builder frontend serializes into a share link.

The shape mirrors ``serialize()`` in the ttb.utoronto.ca bundle: the calendar's two
sessions, one timetable per session (courses, chosen sections, async sections, blocked
off intervals) and one plan per session (courses plus time preference). Fields the
frontend leaves undefined are omitted, as ``JSON.stringify`` does.
"""

from typing import Any

# The frontend's grid runs 8:00 AM to 10:00 PM; blocked intervals outside it are not drawn.
GRID_START_MS = 8 * 3_600_000
GRID_END_MS = 22 * 3_600_000
ASYNC_MODES = frozenset({"ASYNC", "ASYIF"})
BLOCKED_NAME = "Blocked Off"


def _course_ref(course: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": course["code"],
        "name": course["name"],
        "sectionCode": course["sectionCode"],
        "sessions": course["sessions"],
    }


def _is_async(section: dict[str, Any], session: str) -> bool:
    return any(
        mode.get("session") == session and mode.get("mode") in ASYNC_MODES
        for mode in section.get("deliveryModes") or []
    )


def _section_ref(course: dict[str, Any], section: dict[str, Any]) -> dict[str, Any]:
    ref = {"courseCode": course["code"], "name": section["name"], "type": section["type"]}
    if "subTitle" in section:
        ref["subTitle"] = section["subTitle"]
    return ref


def _blocked_sections(blocked_off: list[dict[str, Any]], session: str) -> list[dict[str, Any]]:
    """Blocked-off pseudo sections, clamped to the frontend grid and deduplicated."""
    sections: dict[str, dict[str, Any]] = {}
    for interval in blocked_off:
        day = interval["start"]["day"]
        start = max(interval["start"]["millisofday"], GRID_START_MS)
        end = min(interval["end"]["millisofday"], GRID_END_MS)
        if start >= end:
            continue
        code = f"{day}_{start}_{end}"
        sections.setdefault(
            code,
            {
                "courseCode": code,
                "blocked": True,
                "name": BLOCKED_NAME,
                "meetingTimes": [
                    {
                        "start": {"day": day, "millisofday": start},
                        "end": {"day": day, "millisofday": end},
                        "sessionCode": session,
                    }
                ],
            },
        )
    return list(sections.values())


def build_share_state(
    sessions: list[str],
    planned: list[dict[str, Any]],
    solved: list[dict[str, Any]],
    blocked_off: list[dict[str, Any]],
    fitness_option: str,
    planned_sessions: set[str],
) -> dict[str, Any]:
    """Serialize solver output into the frontend's ``sessions``/``timetables``/``plans``.

    ``planned`` are the requested offerings, ``solved`` the solver's courses with only the
    chosen sections, and ``blocked_off`` generateYear intervals applied to every session in
    ``planned_sessions``.
    """
    timetables = []
    plans = []
    for session in sessions:
        containing = []
        included = []
        online = []
        for course in solved:
            if session not in course["sessions"]:
                continue
            for section in course["sections"]:
                target = online if _is_async(section, session) else included
                target.append(_section_ref(course, section))
            if course["sections"]:
                containing.append(_course_ref(course))
        if session in planned_sessions:
            included.extend(_blocked_sections(blocked_off, session))
        timetables.append(
            {
                "session": session,
                "containingCourses": containing,
                "includedSections": included,
                "onlineAsyncSections": online,
            }
        )
        plans.append(
            {
                "session": session,
                "courses": [_course_ref(c) for c in planned if session in c["sessions"]],
                "selectedTimePreference": fitness_option,
            }
        )
    return {"sessions": sessions, "timetables": timetables, "plans": plans}
