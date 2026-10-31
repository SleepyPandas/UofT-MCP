"""Discovery ranking and truncation hints, offline and through the compact MCP tool."""

import pytest
from mcp import Client
from test_compact import call

from uoft_mcp.operations import Registry
from uoft_mcp.server import create_server


def registry() -> Registry:
    async def degree_explorer_get_planner() -> dict:
        """Planner timelines."""

    async def acorn_get_courses() -> dict:
        """Enrolled courses."""

    async def plan_schedule() -> dict:
        """Combine acorn and degree explorer data into one schedule."""

    async def lookup_things() -> dict:
        """Search public data."""

    async def build_timetable() -> dict:
        """Uses ACORN course data."""

    registered = Registry()
    for handler in (
        degree_explorer_get_planner,
        acorn_get_courses,
        plan_schedule,
        lookup_things,
        build_timetable,
    ):
        registered.add(handler)
    return registered


def names(result: dict) -> list[str]:
    return result["operations"]


def test_service_and_name_matches_outrank_description_only_matches():
    found = registry().discover("acorn degree explorer", None, "names", 5)
    assert names(found) == [
        "degree_explorer_get_planner",
        "acorn_get_courses",
        "plan_schedule",
        "build_timetable",
    ]


def test_service_name_in_query_prefers_that_service():
    found = registry().discover("acorn", None, "names", 5)
    assert names(found) == ["acorn_get_courses", "build_timetable", "plan_schedule"]
    found = registry().discover("timetable", None, "names", 5)
    assert names(found)[0] == "build_timetable"


def test_exact_name_still_returns_only_that_operation():
    found = registry().discover("acorn_get_courses", None, "names", 5)
    assert names(found) == ["acorn_get_courses"]
    assert "hint" not in found


def test_complete_results_have_no_truncation_fields():
    found = registry().discover("acorn", None, "names", 5)
    assert found["total"] == 3 and found["has_more"] is False
    assert "more" not in found and "hint" not in found


@pytest.mark.parametrize(
    "limit,service,total,hint",
    [
        (2, None, 5, "3 more matched. Raise limit or set service to see them."),
        (2, "timetable", 3, "1 more matched. Raise limit to see them."),
        (25, None, 30, "5 more matched. Narrow the query or set service to see them."),
        (25, "timetable", 28, "3 more matched. Narrow the query to see them."),
    ],
)
def test_truncated_results_say_how_many_more_and_how_to_get_them(limit, service, total, hint):
    registered = registry()
    if limit == 25:
        for index in range(25):

            async def padding() -> dict:
                """Padding."""

            padding.__name__ = f"pad_{index:02}"
            registered.add(padding)
    found = registered.discover("", service, "names", limit)
    assert (found["total"], found["has_more"]) == (total, True)
    assert found["more"] == total - limit
    assert found["hint"] == hint


@pytest.mark.anyio
async def test_compact_acorn_query_lists_acorn_operations_first_and_counts_the_rest():
    async with Client(create_server(tool_profile="compact")) as client:
        found = await call(client, "uoft_discover", {"query": "acorn", "detail": "names"})
        assert found["operations"][:3] == [
            "acorn_get_dashboard_courses",
            "acorn_get_eligible_registrations",
            "acorn_get_student_registration_info",
        ]
        narrow = await call(
            client, "uoft_discover", {"query": "acorn", "detail": "names", "limit": 3}
        )
        assert narrow["operations"] == found["operations"][:3]
        assert narrow["more"] == 1 and "limit" in narrow["hint"]


@pytest.mark.anyio
async def test_compact_degree_explorer_default_limit_reports_the_remaining_four():
    async with Client(create_server(tool_profile="compact")) as client:
        found = await call(client, "uoft_discover", {"query": "degree explorer"})
        assert len(found["operations"]) == 5
        assert all(op["service"] == "degree_explorer" for op in found["operations"])
        assert (found["total"], found["more"], found["has_more"]) == (9, 4, True)
        assert found["hint"] == "4 more matched. Raise limit or set service to see them."
        rest = await call(
            client,
            "uoft_discover",
            {"query": "degree explorer", "service": "degree_explorer", "limit": 25},
        )
        assert len(rest["operations"]) == 9 and "hint" not in rest
