"""plan_timetable against a simulated Timetable Builder: lookup, solve, format and share."""

import asyncio
import json
from urllib.parse import unquote

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from uoft_mcp.server import create_server
from uoft_mcp.timetable_builder.planner import (
    CourseRequest,
    choose_offering,
    derive_activity_types,
    parse_course_requests,
)

FALL, WINTER = "20269", "20271"
HOUR = 3_600_000
PROFILES = ["compact", "legacy"]


def meeting(day, start, end, session=FALL, room="BA 1130"):
    code, number = room.split()
    return {
        "start": {"day": day, "millisofday": int(start * HOUR)},
        "end": {"day": day, "millisofday": int(end * HOUR)},
        "building": {"buildingCode": code, "buildingRoomNumber": number, "buildingRoomSuffix": ""},
        "sessionCode": session,
        "repetition": "WEEKLY",
        "repetitionTime": "ONCE_A_WEEK",
    }


def section(
    name, kind, meetings=(), *, cancel="N", tba="N", people=(), mode="INPER", sessions=None
):
    return {
        "name": name,
        "type": kind,
        "meetingTimes": list(meetings),
        "instructors": [{"firstName": first, "lastName": last} for first, last in people],
        "cancelInd": cancel,
        "tbaInd": tba,
        "subTitle": "",
        "deliveryModes": [{"session": s, "mode": mode} for s in sessions or [FALL]],
    }


def course(identifier, code, section_code, sessions, sections, *, name=None, cancel="N"):
    return {
        "id": identifier,
        "code": code,
        "name": name or f"{code} title",
        "sectionCode": section_code,
        "sessions": sessions,
        "sections": sections,
        "cancelInd": cancel,
    }


CSC_F = course(
    "csc148f",
    "CSC148H1",
    "F",
    [FALL],
    [
        section(
            "LEC0101",
            "Lecture",
            [meeting(1, 10, 11, room="MP 102"), meeting(3, 9, 11)],
            people=[("Sadia", "Sharmin")],
        ),
        section("LEC0201", "Lecture", [meeting(2, 13, 14)], cancel="Y"),
        section("TUT0101", "Tutorial", [meeting(4, 9, 11, room="BA 2159")]),
        section("TUT0999", "Tutorial", tba="Y"),
        section("PRA0101", "Practical", [meeting(5, 9, 10)], cancel="Y"),
    ],
    name="Introduction to Computer Science",
)
CSC_S = course(
    "csc148s",
    "CSC148H1",
    "S",
    [WINTER],
    [section("LEC5101", "Lecture", [meeting(1, 18, 19, WINTER)], sessions=[WINTER])],
)
ENG_Y = course(
    "eng140y",
    "ENG140Y1",
    "Y",
    [FALL, WINTER],
    [
        section(
            "LEC0101",
            "Lecture",
            [meeting(5, 13, 15, FALL, "BT 101"), meeting(5, 13, 15, WINTER, "BT 101")],
            people=[("Adam", "Hammond"), ("Adam", "Hammond")],
            sessions=[FALL, WINTER],
        ),
        section(
            "TUT0101",
            "Tutorial",
            [meeting(5, 12, 13, FALL, "AH 103"), meeting(5, 12, 13, WINTER, "AH 306")],
            sessions=[FALL, WINTER],
        ),
    ],
)
MAT_S = course(
    "mat135s",
    "MAT135H1",
    "S",
    [WINTER],
    [
        section("LEC5101", "Lecture", [meeting(1, 18, 20, WINTER)], sessions=[WINTER]),
        section("TUT5201", "Tutorial", [meeting(3, 18, 19, WINTER)], sessions=[WINTER]),
    ],
)
STA_F = course(
    "sta130f",
    "STA130H1",
    "F",
    [FALL],
    [
        section("LEC0101", "Lecture", [meeting(1, 9, 10)]),
        section("LEC0201", "Lecture", [meeting(2, 9, 10)]),
    ],
)
CANCELLED = course("phy100f", "PHY100H1", "F", [FALL], [section("LEC0101", "Lecture")], cancel="Y")
SUMMER = course(
    "sum101", "SUM101H1", "F", ["20265F"], [section("LEC0101", "Lecture", sessions=["20265F"])]
)
ONLINE = course(
    "soc101s",
    "SOC101H1",
    "S",
    [WINTER],
    [section("LEC0101", "Lecture", mode="ASYNC", sessions=[WINTER])],
)
CATALOG = {
    "CSC148H1": [CSC_F, CSC_S],
    "ENG140Y1": [ENG_Y],
    "MAT135H1": [MAT_S],
    "STA130H1": [STA_F],
    "PHY100H1": [CANCELLED],
    "SUM101H1": [SUMMER],
    "SOC101H1": [ONLINE],
}
BY_ID = {c["id"]: c for offerings in CATALOG.values() for c in offerings}


def first_choice(body):
    """Mimic the solver: the first open, scheduled section of each requested type."""
    plans = []
    for plan in body:
        courses = []
        for requested in plan["courses"]:
            full = BY_ID[requested["id"]]
            chosen = [
                next(
                    s
                    for s in full["sections"]
                    if s["type"] == kind["type"] and s["cancelInd"] == "N" and s["tbaInd"] == "N"
                )
                for kind in requested["sections"]
            ]
            courses.append({**full, "sections": chosen})
        plans.append({"courses": courses})
    return plans


class Upstream:
    def __init__(self, solver=first_choice, shorten=None):
        self.solver = solver
        self.shorten = shorten or (lambda request: httpx.Response(200, json={"id": "share123"}))
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/ttb/")
        if path.startswith("getCoursesByCodeAndSectionCode/"):
            offerings = CATALOG.get(path.rsplit("/", 1)[1])
            if offerings is None:
                body = {"payload": None, "status": [{"code": 4404, "message": "No results"}]}
                return httpx.Response(404, json=body)
            return httpx.Response(
                200, json={"payload": {"pageableCourse": {"courses": offerings}}, "status": []}
            )
        if path == "generateYear":
            return httpx.Response(200, json=self.solver(json.loads(request.content)))
        if path == "tiny/shorten":
            return self.shorten(request)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def paths(self):
        return [r.url.path.removeprefix("/ttb/") for r in self.requests]

    def solves(self):
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("generateYear")]

    def shared_state(self):
        (request,) = [r for r in self.requests if r.url.path.endswith("tiny/shorten")]
        url = unquote(request.content.decode())
        assert url.startswith("https://ttb.utoronto.ca/#!/?")
        return json.loads(url.removeprefix("https://ttb.utoronto.ca/#!/?"))


async def plan(upstream, arguments, profile="compact"):
    async with Client(create_server(httpx.MockTransport(upstream), tool_profile=profile)) as client:
        return await client.call_tool("plan_timetable", arguments)


def text_of(result):
    assert len(result.content) == 1
    return result.content[0].text


def test_parse_course_requests():
    parsed = parse_course_requests(["csc148h1", "ENG140Y1-s", " mat135h1 f ", "STA130H1:Y"], "F")
    assert parsed == [
        CourseRequest("CSC148H1", "F"),
        CourseRequest("ENG140Y1", "S"),
        CourseRequest("MAT135H1", "F"),
        CourseRequest("STA130H1", "Y"),
    ]
    assert parse_course_requests(["CSC148H1"], None) == [CourseRequest("CSC148H1", None)]


@pytest.mark.parametrize(
    "codes,message",
    [
        (["CSC148"], "isn't a full UofT course code"),
        (["CSC148H1-Q"], "isn't a full UofT course code"),
        (["CSC148H1", "csc148h1-f"], "CSC148H1 is listed more than once"),
    ],
)
def test_parse_course_requests_rejects_bad_input(codes, message):
    with pytest.raises(ToolError, match=message):
        parse_course_requests(codes, None)


def test_parse_course_requests_reports_every_problem():
    with pytest.raises(ToolError) as error:
        parse_course_requests(["CSC148", "BIO", "CSC148H1"], None)
    assert "'CSC148'" in str(error.value) and "'BIO'" in str(error.value)


def test_choose_offering_by_term():
    both = [CSC_F, CSC_S]
    assert choose_offering(CourseRequest("CSC148H1", "F"), both) is CSC_F
    assert choose_offering(CourseRequest("CSC148H1", "S"), both) is CSC_S
    assert choose_offering(CourseRequest("CSC148H1", None), [CSC_F]) is CSC_F
    assert choose_offering(CourseRequest("ENG140Y1", "F"), [ENG_Y]) is ENG_Y
    assert choose_offering(CourseRequest("ENG140Y1", "S"), [ENG_Y]) is ENG_Y
    cancelled_fall = {**CSC_F, "cancelInd": "Y"}
    assert choose_offering(CourseRequest("CSC148H1", None), [cancelled_fall, CSC_S]) is CSC_S


def test_choose_offering_errors_are_plain_language():
    with pytest.raises(
        ToolError, match=r"offered more than once: F \(Fall 2026\), S \(Winter 2027\)"
    ):
        choose_offering(CourseRequest("CSC148H1", None), [CSC_F, CSC_S])
    with pytest.raises(ToolError, match=r"isn't offered for term Y. Offered: F \(Fall 2026\)"):
        choose_offering(CourseRequest("CSC148H1", "Y"), [CSC_F])
    with pytest.raises(ToolError, match=r"PHY100H1 is cancelled"):
        choose_offering(CourseRequest("PHY100H1", None), [CANCELLED])


def test_derive_activity_types_skips_cancelled_and_tba_sections():
    assert derive_activity_types(CSC_F) == ["Lecture", "Tutorial"]
    odd = course(
        "x",
        "ABC123H1",
        "F",
        [FALL],
        [
            section("SEM0101", "Seminar", [meeting(1, 9, 10)]),
            section("PRA0101", "Practical", [meeting(2, 9, 10)]),
        ],
    )
    assert derive_activity_types(odd) == ["Practical", "Seminar"]
    with pytest.raises(ToolError, match="no scheduled sections"):
        derive_activity_types(
            course("y", "ABC123H1", "F", [FALL], [section("LEC0101", "Lecture", tba="Y")])
        )


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_registered_in_both_profiles_with_share_annotations(profile):
    async with Client(create_server(tool_profile=profile)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    tool = tools["plan_timetable"]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    schema = tool.input_schema
    assert schema["required"] == ["course_codes"]
    assert schema["properties"]["course_codes"]["minItems"] == 1
    assert schema["properties"]["course_codes"]["maxItems"] == 10
    assert schema["properties"]["share"]["default"] is True
    assert "ctx" not in schema["properties"]


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_fall_plan_request_output_and_share(profile):
    upstream = Upstream()
    result = await plan(
        upstream,
        {
            "course_codes": ["CSC148H1-F"],
            "preference": "early",
            "blocked_times": [{"day": "Monday", "start": "8:00", "end": "9:00"}],
        },
        profile,
    )
    assert not result.is_error, result.content
    assert upstream.paths() == [
        "getCoursesByCodeAndSectionCode/CSC148H1",
        "generateYear",
        "tiny/shorten",
    ]
    assert upstream.solves() == [
        [
            {
                "courses": [
                    {
                        "id": "csc148f",
                        "sections": [
                            {"name": "*", "type": "Lecture"},
                            {"name": "*", "type": "Tutorial"},
                        ],
                    }
                ],
                "fitnessFunctionOption": "MORNING_WEIGHTED",
                "blockedOff": [
                    {
                        "start": {"day": 1, "millisofday": 8 * HOUR},
                        "end": {"day": 1, "millisofday": 9 * HOUR},
                    }
                ],
            }
        ]
    ]
    assert text_of(result) == (
        "Fall 2026 (early schedule)\n"
        "CSC148H1 - Introduction to Computer Science\n"
        "  Lecture LEC0101: Mon 10:00-11:00 MP 102; Wed 09:00-11:00 BA 1130 (Sadia Sharmin)\n"
        "  Tutorial TUT0101: Thu 09:00-11:00 BA 2159\n"
        "Weekly overview, Fall 2026:\n"
        "  Mon: 10:00-11:00 CSC148H1 LEC0101\n"
        "  Tue: free\n"
        "  Wed: 09:00-11:00 CSC148H1 LEC0101\n"
        "  Thu: 09:00-11:00 CSC148H1 TUT0101\n"
        "  Fri: free\n"
        "\n"
        "share_url: https://ttb.utoronto.ca/#!/?t=share123\n"
        "\n"
        "This is a plan only; register for sections on ACORN."
    )
    state = upstream.shared_state()
    assert state["sessions"] == [FALL, WINTER]
    fall, winter = state["timetables"]
    assert [s["name"] for s in fall["includedSections"] if not s.get("blocked")] == [
        "LEC0101",
        "TUT0101",
    ]
    assert [s["courseCode"] for s in fall["includedSections"] if s.get("blocked")] == [
        f"1_{8 * HOUR}_{9 * HOUR}"
    ]
    assert winter["containingCourses"] == []
    assert state["plans"][0]["selectedTimePreference"] == "MORNING_WEIGHTED"
    assert state["plans"][1]["courses"] == []


@pytest.mark.anyio
async def test_year_long_courses_join_the_fall_plan_and_block_winter_meetings():
    upstream = Upstream()
    result = await plan(upstream, {"course_codes": ["ENG140Y1", "CSC148H1-F", "MAT135H1"]})
    assert not result.is_error, result.content
    assert upstream.paths().count("generateYear") == 2
    fall, winter = upstream.solves()
    assert [c["id"] for c in fall[0]["courses"]] == ["eng140y", "csc148f"]
    assert [c["id"] for c in winter[0]["courses"]] == ["mat135s"]
    assert fall[0]["blockedOff"] == []
    assert winter[0]["blockedOff"] == [
        {
            "start": {"day": 5, "millisofday": 13 * HOUR},
            "end": {"day": 5, "millisofday": 15 * HOUR},
        },
        {
            "start": {"day": 5, "millisofday": 12 * HOUR},
            "end": {"day": 5, "millisofday": 13 * HOUR},
        },
    ]
    text = text_of(result)
    assert "ENG140Y1 [full year] - ENG140Y1 title" in text
    assert "Tutorial TUT0101: Fri 12:00-13:00 AH 103" in text
    assert "Tutorial TUT0101: Fri 12:00-13:00 AH 306" in text
    assert "Winter 2027 (balanced schedule)" in text
    assert "Lecture LEC0101: Fri 13:00-15:00 BT 101 (Adam Hammond)" in text
    state = upstream.shared_state()
    fall_table, winter_table = state["timetables"]
    assert [c["code"] for c in fall_table["containingCourses"]] == ["ENG140Y1", "CSC148H1"]
    assert [c["code"] for c in winter_table["containingCourses"]] == ["ENG140Y1", "MAT135H1"]
    assert [p["courses"][0]["code"] for p in state["plans"]] == ["ENG140Y1", "ENG140Y1"]


@pytest.mark.anyio
async def test_winter_only_plan_sends_one_solver_plan():
    upstream = Upstream()
    result = await plan(upstream, {"course_codes": ["MAT135H1"], "share": False})
    assert not result.is_error, result.content
    assert [[c["id"] for c in p["courses"]] for p in upstream.solves()[0]] == [["mat135s"]]
    assert len(upstream.solves()) == 1
    assert "Fall 2026" not in text_of(result)
    assert "Winter 2027 (balanced schedule)" in text_of(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments,solved_id",
    [
        ({"course_codes": ["CSC148H1-S"]}, "csc148s"),
        ({"course_codes": ["CSC148H1"], "term": "S"}, "csc148s"),
        ({"course_codes": ["CSC148H1-F"], "term": "S"}, "csc148f"),
        ({"course_codes": ["ENG140Y1"], "term": "F"}, "eng140y"),
    ],
)
async def test_term_selection(arguments, solved_id):
    upstream = Upstream()
    result = await plan(upstream, {**arguments, "share": False})
    assert not result.is_error, result.content
    assert upstream.solves()[0][0]["courses"][0]["id"] == solved_id


@pytest.mark.anyio
async def test_async_sections_are_online_in_share_state():
    upstream = Upstream()
    result = await plan(upstream, {"course_codes": ["SOC101H1"]})
    assert not result.is_error, result.content
    assert "Lecture LEC0101: online, asynchronous" in text_of(result)
    winter = upstream.shared_state()["timetables"][1]
    assert winter["includedSections"] == []
    assert [s["name"] for s in winter["onlineAsyncSections"]] == ["LEC0101"]


@pytest.mark.anyio
async def test_lookup_problems_are_reported_together_without_solving():
    upstream = Upstream()
    result = await plan(
        upstream,
        {"course_codes": ["CSC148H1", "XYZ999H1", "PHY100H1", "SUM101H1", "ENG140Y1"]},
    )
    assert result.is_error
    message = text_of(result)
    assert "CSC148H1 is offered more than once: F (Fall 2026), S (Winter 2027)" in message
    assert "CSC148H1-F or term F" in message
    assert "couldn't find a course with the code XYZ999H1" in message
    assert "PHY100H1 is cancelled" in message
    assert "SUM101H1 has no Fall or Winter offering" in message
    assert "ENG140Y1" not in message
    assert not any(p in {"generateYear", "tiny/shorten"} for p in upstream.paths())


@pytest.mark.anyio
async def test_lookups_use_bounded_concurrency():
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200, json={"payload": {"pageableCourse": {"courses": [STA_F]}}, "status": []}
        )

    transport = httpx.MockTransport(respond)
    codes = [f"STA{n}H1" for n in range(100, 108)]
    async with Client(create_server(transport)) as client:
        await client.call_tool("plan_timetable", {"course_codes": codes, "share": False})
    assert 1 < peak <= 4


@pytest.mark.anyio
async def test_lookup_outage_is_a_plain_tool_error():
    async with Client(
        create_server(httpx.MockTransport(lambda r: httpx.Response(503, text="private body")))
    ) as client:
        result = await client.call_tool("plan_timetable", {"course_codes": ["CSC148H1-F"]})
    assert result.is_error
    assert "Timetable Builder" in text_of(result)
    assert "private body" not in text_of(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "solver,expected",
    [
        (lambda body: [], "no lecture/tutorial section of CSC148H1 fits"),
        (
            lambda body: [{"courses": [{**CSC_F, "sections": [CSC_F["sections"][0]]}]}],
            "no tutorial section of CSC148H1 fits",
        ),
        (
            lambda body: [
                {
                    "courses": [
                        {**CSC_F, "sections": [CSC_F["sections"][0], CSC_F["sections"][2]]},
                        {
                            **STA_F,
                            "sections": [
                                section("LEC0301", "Lecture", [meeting(3, 10, 12, room="SS 1070")])
                            ],
                        },
                    ]
                }
            ],
            "CSC148H1 LEC0101 (Wed 09:00-11:00 BA 1130) overlaps STA130H1 LEC0301 "
            "(Wed 10:00-12:00 SS 1070)",
        ),
    ],
)
async def test_unusable_solver_output_is_reported_plainly(solver, expected):
    upstream = Upstream(solver)
    codes = ["CSC148H1-F", "STA130H1"] if "overlaps" in expected else ["CSC148H1-F"]
    result = await plan(upstream, {"course_codes": codes})
    assert result.is_error
    message = text_of(result)
    assert "I couldn't find a schedule without conflicts: " in message
    assert expected in message
    assert "fewer or shorter blocked times" in message
    assert "tiny/shorten" not in upstream.paths()


@pytest.mark.anyio
async def test_unmet_blocked_times_are_warnings_and_still_shared():
    upstream = Upstream()
    result = await plan(
        upstream,
        {
            "course_codes": ["CSC148H1-F"],
            "blocked_times": [{"day": "Wednesday", "start": "00:00", "end": "10:00"}],
        },
    )
    assert not result.is_error, result.content
    assert (
        "- CSC148H1 LEC0101 (Wed 09:00-11:00 BA 1130) falls inside your blocked time "
        "Wed 00:00-10:00; the solver could not avoid it."
    ) in text_of(result)
    assert "share_url:" in text_of(result)
    blocked = [
        s for s in upstream.shared_state()["timetables"][0]["includedSections"] if s.get("blocked")
    ]
    assert [b["courseCode"] for b in blocked] == [f"3_{8 * HOUR}_{10 * HOUR}"]


@pytest.mark.anyio
async def test_share_false_creates_no_share():
    upstream = Upstream()
    result = await plan(upstream, {"course_codes": ["CSC148H1-F"], "share": False})
    assert not result.is_error, result.content
    assert "share_url" not in text_of(result)
    assert "tiny/shorten" not in upstream.paths()


@pytest.mark.anyio
async def test_share_failure_keeps_the_schedule():
    upstream = Upstream(shorten=lambda request: httpx.Response(500, text="private"))
    result = await plan(upstream, {"course_codes": ["CSC148H1-F"]})
    assert not result.is_error
    assert "Lecture LEC0101" in text_of(result)
    assert "Share link unavailable: " in text_of(result)
    assert "private" not in text_of(result)


@pytest.mark.anyio
async def test_courses_from_different_years_are_rejected(monkeypatch):
    next_year = course(
        "csc148f27",
        "CSC148H1",
        "F",
        ["20279"],
        [section("LEC0101", "Lecture", [meeting(1, 9, 10)])],
    )
    upstream = Upstream()
    monkeypatch.setitem(CATALOG, "CSC148H1", [next_year])
    result = await plan(upstream, {"course_codes": ["CSC148H1", "MAT135H1"]})
    assert result.is_error
    assert "different academic years" in text_of(result)
    assert "generateYear" not in upstream.paths()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"course_codes": []},
        {"course_codes": [f"CSC{100 + n}H1" for n in range(11)]},
        {"course_codes": ["CSC148"]},
        {"course_codes": ["CSC148H1", "csc148h1"]},
        {"course_codes": [" "]},
        {"course_codes": ["CSC148H1-F"], "term": "X"},
        {"course_codes": ["CSC148H1-F"], "preference": "midnight"},
        {"course_codes": ["CSC148H1-F"], "share": "maybe"},
        {
            "course_codes": ["CSC148H1-F"],
            "blocked_times": [{"day": "Monday", "start": "10:00", "end": "9:00"}],
        },
        {
            "course_codes": ["CSC148H1-F"],
            "blocked_times": [{"day": "Monday", "start": "25:00", "end": "26:00"}],
        },
        {
            "course_codes": ["CSC148H1-F"],
            "blocked_times": [{"day": "Sunday", "start": "9:00", "end": "10:00"}],
        },
    ],
)
async def test_invalid_arguments_do_not_reach_upstream(arguments):
    upstream = Upstream()
    result = await plan(upstream, arguments)
    assert result.is_error
    assert upstream.requests == []
