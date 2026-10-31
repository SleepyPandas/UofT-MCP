"""The share state must match what the official Timetable Builder serializes and reloads."""

import json
from urllib.parse import unquote

import httpx
import pytest

from uoft_mcp.timetable_builder.client import TimetableAPIError, create_http_client, shorten_state
from uoft_mcp.timetable_builder.generation import BlockedTime, blocked_off
from uoft_mcp.timetable_builder.share_state import build_share_state

FALL, WINTER = "20269", "20271"
HOUR = 3_600_000


def course(code, section_code, sessions, sections):
    return {
        "id": f"id-{code}",
        "code": code,
        "name": f"Name of {code}",
        "sectionCode": section_code,
        "sessions": sessions,
        "sections": sections,
    }


def section(name, kind, modes=None, **extra):
    return {
        "name": name,
        "type": kind,
        "subTitle": "",
        "deliveryModes": [{"session": s, "mode": m} for s, m in (modes or [])],
        **extra,
    }


CSC = course(
    "CSC148H1", "F", [FALL], [section("LEC0101", "Lecture"), section("TUT0101", "Tutorial")]
)
ENG = course(
    "ENG140Y1",
    "Y",
    [FALL, WINTER],
    [section("LEC0101", "Lecture", [(FALL, "INPER"), (WINTER, "INPER")])],
)
ASYNC = course(
    "SOC101H1",
    "S",
    [WINTER],
    [section("LEC0101", "Lecture", [(WINTER, "ASYNC")], subTitle="Online")],
)


def planned_sessions(*courses):
    return {s for c in courses for s in c["sessions"]}


def test_exact_state_shape():
    intervals = blocked_off([BlockedTime(day="Monday", start="09:00", end="10:00")])
    state = build_share_state(
        [FALL, WINTER], [CSC, ENG], [CSC, ENG], intervals, "BALANCED", planned_sessions(CSC, ENG)
    )
    csc_ref = {
        "code": "CSC148H1",
        "name": "Name of CSC148H1",
        "sectionCode": "F",
        "sessions": [FALL],
    }
    eng_ref = {
        "code": "ENG140Y1",
        "name": "Name of ENG140Y1",
        "sectionCode": "Y",
        "sessions": [FALL, WINTER],
    }

    def blocked(session):
        return {
            "courseCode": f"1_{9 * HOUR}_{10 * HOUR}",
            "blocked": True,
            "name": "Blocked Off",
            "meetingTimes": [
                {
                    "start": {"day": 1, "millisofday": 9 * HOUR},
                    "end": {"day": 1, "millisofday": 10 * HOUR},
                    "sessionCode": session,
                }
            ],
        }

    def sec(code, name, kind):
        return {"courseCode": code, "name": name, "type": kind, "subTitle": ""}

    assert state == {
        "sessions": [FALL, WINTER],
        "timetables": [
            {
                "session": FALL,
                "containingCourses": [csc_ref, eng_ref],
                "includedSections": [
                    sec("CSC148H1", "LEC0101", "Lecture"),
                    sec("CSC148H1", "TUT0101", "Tutorial"),
                    sec("ENG140Y1", "LEC0101", "Lecture"),
                    blocked(FALL),
                ],
                "onlineAsyncSections": [],
            },
            {
                "session": WINTER,
                "containingCourses": [eng_ref],
                "includedSections": [sec("ENG140Y1", "LEC0101", "Lecture"), blocked(WINTER)],
                "onlineAsyncSections": [],
            },
        ],
        "plans": [
            {
                "session": FALL,
                "courses": [csc_ref, eng_ref],
                "selectedTimePreference": "BALANCED",
            },
            {"session": WINTER, "courses": [eng_ref], "selectedTimePreference": "BALANCED"},
        ],
    }
    assert list(state["timetables"][0]["includedSections"][0]) == [
        "courseCode",
        "name",
        "type",
        "subTitle",
    ]


def test_state_without_courses_in_a_term_keeps_both_sessions():
    state = build_share_state([FALL, WINTER], [CSC], [CSC], [], "MORNING_WEIGHTED", {FALL})
    winter_timetable, winter_plan = state["timetables"][1], state["plans"][1]
    assert winter_timetable == {
        "session": WINTER,
        "containingCourses": [],
        "includedSections": [],
        "onlineAsyncSections": [],
    }
    assert winter_plan == {
        "session": WINTER,
        "courses": [],
        "selectedTimePreference": "MORNING_WEIGHTED",
    }


def test_async_sections_use_online_list_and_keep_subtitle():
    state = build_share_state([FALL, WINTER], [ASYNC], [ASYNC], [], "BALANCED", {WINTER})
    winter = state["timetables"][1]
    assert winter["includedSections"] == []
    assert winter["onlineAsyncSections"] == [
        {"courseCode": "SOC101H1", "name": "LEC0101", "type": "Lecture", "subTitle": "Online"}
    ]
    assert [c["code"] for c in winter["containingCourses"]] == ["SOC101H1"]


def test_blocked_times_are_clamped_to_the_frontend_grid_and_deduplicated():
    blocked = [
        BlockedTime(day="Monday", start="00:00", end="10:00"),
        BlockedTime(day="Monday", start="08:00", end="10:00"),
        BlockedTime(day="Tuesday", start="00:00", end="07:30"),
        BlockedTime(day="Friday", start="21:00", end="23:30"),
    ]
    state = build_share_state(
        [FALL, WINTER], [CSC], [CSC], blocked_off(blocked), "BALANCED", {FALL}
    )
    codes = [
        s["courseCode"] for s in state["timetables"][0]["includedSections"] if s.get("blocked")
    ]
    assert codes == [f"1_{8 * HOUR}_{10 * HOUR}", f"5_{21 * HOUR}_{22 * HOUR}"]
    assert not any(s.get("blocked") for s in state["timetables"][1]["includedSections"])


@pytest.mark.anyio
async def test_shorten_state_posts_the_encoded_official_url():
    state = {"sessions": [FALL, WINTER], "timetables": [], "plans": [], "note": "é & #"}
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "abc123"})

    async with create_http_client(httpx.MockTransport(respond)) as client:
        assert await shorten_state(client, state) == {"id": "abc123"}
    request = seen[0]
    assert request.url.path == "/ttb/tiny/shorten"
    assert request.headers["content-type"] == "text/plain"
    decoded = unquote(request.content.decode())
    assert decoded.startswith("https://ttb.utoronto.ca/#!/?")
    assert json.loads(decoded.removeprefix("https://ttb.utoronto.ca/#!/?")) == state


@pytest.mark.anyio
async def test_shorten_state_requires_a_share_id():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"id": ""}))
    async with create_http_client(transport) as client:
        with pytest.raises(TimetableAPIError, match="share id"):
            await shorten_state(client, {"sessions": []})
