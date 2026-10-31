"""Typed request models and body builder for the Timetable Builder ``generateYear`` solver."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Never

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

NonEmptyString = Annotated[str, Field(min_length=1, pattern=r"\S")]
SectionCode = Literal["F", "S", "Y"]
TimePreference = Literal["early", "balanced", "late"]
Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ClockTime = Annotated[str, Field(pattern=r"^\d{1,2}:\d{2}$")]
ObjectId = Annotated[str, Field(pattern=r"^[A-Za-z0-9]+$")]


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


def weekday_number(day: Weekday) -> int:
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


def millis_of_day(clock: str) -> int:
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
        start = millis_of_day(blocked.start)
        end = millis_of_day(blocked.end)
        if start >= end:
            raise ToolError(
                f"{blocked.day} blocked time {blocked.start} must be earlier than {blocked.end}."
            )
        day = weekday_number(blocked.day)
        intervals.append(
            {
                "start": {"day": day, "millisofday": start},
                "end": {"day": day, "millisofday": end},
            }
        )
    return intervals


def generation_body(plans: list[GenerationPlan]) -> list[dict[str, Any]]:
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
