"""MCP-level tests for find_courses and course_info in both tool profiles, fully offline."""

import asyncio
import json

import httpx
import pytest
from mcp import Client

from uoft_mcp.server import create_server

PROFILES = ["compact", "legacy"]
CURRENT_SESSIONS = {
    "payload": [
        {"label": "Fall-Winter 2026-2027", "value": "Fall-Winter", "header": True},
        {"label": "Fall 2026 (F)", "value": "20269", "header": False},
        {"label": "Winter 2027  (S)", "value": "20271", "header": False},
        {"label": "Fall-Winter 2026-2027 (Y)", "value": "20269-20271", "header": False},
    ],
    "status": [{"code": 0, "message": "Success"}],
}


def section(name, kind, enrolled=10, maximum=40, **extra):
    return {
        "name": name,
        "type": kind,
        "meetingTimes": [
            {
                "start": {"day": 4, "millisofday": 32_400_000},
                "end": {"day": 4, "millisofday": 39_600_000},
                "building": {"buildingCode": "BA", "buildingRoomNumber": "1130"},
                "sessionCode": "20269",
                "repetition": "WEEKLY",
            }
        ],
        "instructors": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "currentEnrolment": enrolled,
        "maxEnrolment": maximum,
        "currentWaitlist": 0,
        "waitlistInd": "N",
        "deliveryModes": [{"session": "20269", "mode": "INPER"}],
        "cancelInd": "N",
        "enrolmentControls": [{"post": {"code": "*"}}],
        **extra,
    }


def course(code="CSC148H1", term="F", **overrides):
    return {
        "id": f"id-{code}-{term}",
        "code": code,
        "name": f"Title of {code}",
        "sectionCode": term,
        "campus": "St. George",
        "sessions": ["20269" if term == "F" else "20271"],
        "maxCredit": 0.5,
        "cmCourseInfo": {
            "description": f"<p>About {code}.</p>",
            "prerequisitesText": "<p>CSC108H1</p>",
            "breadthRequirements": "['The Physical and Mathematical Universes (5)']",
            "distributionRequirements": ["Science"],
        },
        "primaryFull": False,
        "sections": [
            section("TUT0101", "Tutorial", 40, 40),
            section("LEC0101", "Lecture"),
        ],
        **overrides,
    }


def envelope(courses, total=None):
    return {
        "payload": {
            "pageableCourse": {
                "courses": courses,
                "total": len(courses) if total is None else total,
                "page": 1,
                "pageSize": 20,
            }
        },
        "status": [{"code": 0, "message": "Success"}],
    }


class Upstream:
    """Records requests; answers the three endpoints these tools use."""

    def __init__(self, total_rows=45):
        self.rows = [course(f"CSC{n:03d}H1") for n in range(total_rows)]
        self.requests: list[httpx.Request] = []
        self.active = 0
        self.max_active = 0

    @property
    def searches(self):
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path.endswith("getPageableCourses")
        ]

    async def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/ttb/")
        if path == "current-session":
            return httpx.Response(200, json=CURRENT_SESSIONS)
        if path.startswith("getCoursesByCodeAndSectionCode/"):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.active -= 1
            code = path.rsplit("/", 1)[1]
            if code.startswith("NOPE"):
                return httpx.Response(404, text="private upstream body")
            if code.startswith("EMPTY"):
                return httpx.Response(200, json=envelope([]))
            terms = [request.url.params.get("sectionCode") or "F"]
            if code == "BOTH":
                terms = ["F", "S"]
            return httpx.Response(200, json=envelope([course(code, t) for t in terms]))
        if path == "getPageableCourses":
            body = json.loads(request.content)
            if body["courseCodeAndTitleProps"]["courseTitle"] == "nomatch":
                return httpx.Response(404, text="private upstream body")
            start = (body["page"] - 1) * 20
            return httpx.Response(
                200, json=envelope(self.rows[start : start + 20], total=len(self.rows))
            )
        raise AssertionError(f"unexpected request {request.method} {path}")


def make_server(upstream, profile):
    return create_server(httpx.MockTransport(upstream), tool_profile=profile)


async def call(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


async def call_error(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert result.is_error
    return result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_tools_are_listed_read_only_with_documented_arguments(profile):
    async with Client(make_server(Upstream(), profile)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("find_courses", "course_info"):
            tool = tools[name]
            assert tool.description
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert "ctx" not in tool.input_schema["properties"]
        assert tools["find_courses"].input_schema["required"] == ["query"]
        assert set(tools["find_courses"].input_schema["properties"]) == {
            "query",
            "term",
            "campus",
            "division",
            "page",
            "page_size",
        }
        assert tools["course_info"].input_schema["required"] == ["course_codes"]


@pytest.mark.anyio
async def test_compact_instructions_and_dispatcher_do_not_include_research_tools():
    server = make_server(Upstream(), "compact")
    assert "find_courses" in server.instructions and "course_info" in server.instructions
    async with Client(server) as client:
        operations = await call(client, "uoft_discover", {"detail": "names", "limit": 25})
        assert "find_courses" not in operations["operations"]
        assert "course_info" not in operations["operations"]


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_course_info_returns_compact_cards(profile):
    upstream = Upstream()
    async with Client(make_server(upstream, profile)) as client:
        result = await client.call_tool(
            "course_info", {"course_codes": ["csc148h1", "CSC165H1"], "section_code": "F"}
        )
    assert not result.is_error
    text = result.content[0].text
    assert ": " not in text and ", " not in text  # compact separators
    assert "enrolmentControls" not in text and "private" not in text
    data = json.loads(text)
    assert [c["code"] for c in data["courses"]] == ["CSC148H1", "CSC165H1"]
    card = data["courses"][0]
    assert card["id"] == "id-CSC148H1-F"
    assert card["term"] == "Fall 2026" and card["credits"] == 0.5
    assert card["description"] == "About CSC148H1."
    assert card["breadth"] == ["The Physical and Mathematical Universes (5)"]
    assert card["sections"]["Lecture"][0] == {
        "name": "LEC0101",
        "instructors": ["Ada Lovelace"],
        "meetings": ["Thu 09:00-11:00 BA 1130"],
        "enrolment": "10/40",
        "delivery": "In Person",
    }
    assert card["sections"]["Tutorial"][0]["full"] is True
    assert "errors" not in data
    assert [r.method for r in upstream.requests] == ["GET", "GET"]
    assert {r.url.path for r in upstream.requests} == {
        "/ttb/getCoursesByCodeAndSectionCode/CSC148H1",
        "/ttb/getCoursesByCodeAndSectionCode/CSC165H1",
    }
    assert all(r.url.params["sectionCode"] == "F" for r in upstream.requests)


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_course_info_without_section_code_sends_no_filter_and_dedupes(profile):
    upstream = Upstream()
    async with Client(make_server(upstream, profile)) as client:
        data = await call(client, "course_info", {"course_codes": ["BOTH", "both"]})
    assert [(c["code"], c["term"]) for c in data["courses"]] == [
        ("BOTH", "Fall 2026"),
        ("BOTH", "Winter 2027"),
    ]
    assert len(upstream.requests) == 1
    assert "sectionCode" not in upstream.requests[0].url.params


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_course_info_fetches_concurrently_but_bounded(profile):
    upstream = Upstream()
    codes = [f"CSC{n}H1" for n in range(100, 108)]
    async with Client(make_server(upstream, profile)) as client:
        data = await call(client, "course_info", {"course_codes": codes})
    assert [c["code"] for c in data["courses"]] == codes
    assert 1 < upstream.max_active <= 4


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_course_info_reports_partial_failures_in_plain_language(profile):
    async with Client(make_server(Upstream(), profile)) as client:
        data = await call(
            client,
            "course_info",
            {"course_codes": ["CSC108H1", "NOPE999H1", "EMPTY100H1"], "section_code": "Y"},
        )
        assert [c["code"] for c in data["courses"]] == ["CSC108H1"]
        errors = {e["code"]: e["error"] for e in data["errors"]}
        assert errors["NOPE999H1"].startswith("No course found for NOPE999H1")
        assert errors["EMPTY100H1"] == "No offering of EMPTY100H1 in section Y found."
        assert "private upstream body" not in json.dumps(data)

        message = await call_error(client, "course_info", {"course_codes": ["NOPE999H1"]})
        assert "No course found for NOPE999H1" in message
        assert "private upstream body" not in message


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,message",
    [
        (500, "HTTP 500"),
        ("timeout", "timed out after 30 seconds"),
        ("connection", "Can't reach Timetable Builder"),
        ("json", "invalid JSON"),
        ("shape", "UofT changed the Timetable Builder page"),
    ],
)
async def test_course_info_upstream_failures_are_tool_errors(failure, message):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("test connection", request=request)
        if failure == "json":
            return httpx.Response(200, text="<html>unavailable</html>")
        if failure == "shape":
            return httpx.Response(200, json={"payload": {"unexpected": True}})
        return httpx.Response(failure, text="upstream diagnostic body")

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        text = await call_error(client, "course_info", {"course_codes": ["CSC108H1"]})
    assert message in text
    assert "upstream diagnostic body" not in text


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("course_info", {}),
        ("course_info", {"course_codes": []}),
        ("course_info", {"course_codes": [f"CSC{n}H1" for n in range(9)]}),
        ("course_info", {"course_codes": ["../current-session"]}),
        ("course_info", {"course_codes": [""]}),
        ("course_info", {"course_codes": ["CSC108H1"], "section_code": "X"}),
        ("find_courses", {}),
        ("find_courses", {"query": ""}),
        ("find_courses", {"query": "  "}),
        ("find_courses", {"query": "ml", "term": " "}),
        ("find_courses", {"query": "ml", "campus": "UTM"}),
        ("find_courses", {"query": "ml", "division": "ART SC"}),
        ("find_courses", {"query": "ml", "division": "123"}),
        ("find_courses", {"query": "ml", "page": 0}),
        ("find_courses", {"query": "ml", "page_size": 0}),
        ("find_courses", {"query": "ml", "page_size": 21}),
    ],
)
async def test_invalid_arguments_do_not_reach_upstream(profile, name, arguments):
    upstream = Upstream()
    async with Client(make_server(upstream, profile)) as client:
        await call_error(client, name, arguments)
    assert upstream.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_find_courses_maps_filters_without_session_lookup(profile):
    upstream = Upstream()
    async with Client(make_server(upstream, profile)) as client:
        data = await call(
            client,
            "find_courses",
            {
                "query": "  machine   learning ",
                "term": "Fall 2026",
                "campus": "Mississauga",
                "division": "artsc",
            },
        )
    assert data["query"] == "machine learning"
    assert data["terms"] == ["Fall 2026"]
    assert len(upstream.requests) == 1
    assert upstream.requests[0].method == "POST"
    assert upstream.searches == [
        {
            "courseCodeAndTitleProps": {
                "courseCode": "",
                "courseTitle": "machine learning",
                "courseSectionCode": "",
                "searchCourseDescription": True,
            },
            "divisions": ["ARTSC"],
            "departmentProps": [],
            "sessions": ["20269"],
            "campuses": ["University of Toronto at Mississauga"],
            "deliveryModes": [],
            "page": 1,
            "pageSize": 20,
            "direction": "asc",
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "campus,value", [("St. George", "St. George"), ("Scarborough", "Scarborough")]
)
async def test_find_courses_other_campus_values(campus, value):
    upstream = Upstream()
    async with Client(make_server(upstream, "legacy")) as client:
        await call(client, "find_courses", {"query": "x", "term": "20271", "campus": campus})
    assert upstream.searches[0]["campuses"] == [value]
    assert upstream.searches[0]["sessions"] == ["20271"]


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_find_courses_resolves_current_sessions_when_term_is_omitted(profile):
    upstream = Upstream()
    async with Client(make_server(upstream, profile)) as client:
        data = await call(client, "find_courses", {"query": "csc"})
    assert [r.url.path for r in upstream.requests] == [
        "/ttb/current-session",
        "/ttb/getPageableCourses",
    ]
    assert upstream.searches[0]["sessions"] == ["20269", "20271", "20269-20271"]
    assert data["terms"] == ["Fall 2026", "Winter 2027", "Fall 2026 to Winter 2027"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "term,sessions", [("winter", ["20271"]), ("F", ["20269"]), ("Year", ["20269-20271"])]
)
async def test_find_courses_accepts_short_term_names(term, sessions):
    upstream = Upstream()
    async with Client(make_server(upstream, "compact")) as client:
        await call(client, "find_courses", {"query": "csc", "term": term})
    assert upstream.searches[0]["sessions"] == sessions


@pytest.mark.anyio
async def test_find_courses_rejects_unknown_term_with_available_choices():
    upstream = Upstream()
    async with Client(make_server(upstream, "compact")) as client:
        message = await call_error(client, "find_courses", {"query": "csc", "term": "spring"})
    assert "Unknown term 'spring'" in message and "Fall 2026" in message
    assert upstream.searches == []


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_find_courses_returns_short_summaries_with_paging_metadata(profile):
    upstream = Upstream()
    async with Client(make_server(upstream, profile)) as client:
        data = await call(client, "find_courses", {"query": "CSC00", "term": "Fall 2026"})
    assert (data["total"], data["page"], data["page_size"], data["has_more"]) == (45, 1, 10, True)
    assert [c["code"] for c in data["courses"]] == [f"CSC{n:03d}H1" for n in range(10)]
    first = data["courses"][0]
    assert first == {
        "code": "CSC000H1",
        "title": "Title of CSC000H1",
        "term": "Fall 2026",
        "campus": "St. George",
        "credits": 0.5,
        "description": "About CSC000H1.",
        "breadth": ["The Physical and Mathematical Universes (5)", "Science"],
        "lectures_full": False,
        "matched_in": "code",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "page,size,codes,has_more,upstream_pages",
    [
        (2, 10, range(10, 20), True, [1]),
        (3, 10, range(20, 30), True, [2]),
        (5, 10, range(40, 45), False, [3]),
        (2, 15, range(15, 30), True, [1, 2]),
        (3, 20, range(40, 45), False, [3]),
        (6, 10, [], False, [3]),
    ],
)
async def test_find_courses_pages_across_fixed_size_upstream_pages(
    page, size, codes, has_more, upstream_pages
):
    upstream = Upstream()
    async with Client(make_server(upstream, "legacy")) as client:
        data = await call(
            client,
            "find_courses",
            {"query": "csc", "term": "20269", "page": page, "page_size": size},
        )
    assert [c["code"] for c in data["courses"]] == [f"CSC{n:03d}H1" for n in codes]
    assert data["has_more"] is has_more and data["page"] == page
    assert [s["page"] for s in upstream.searches] == upstream_pages
    assert {s["pageSize"] for s in upstream.searches} == {20}


@pytest.mark.anyio
@pytest.mark.parametrize("profile", PROFILES)
async def test_find_courses_no_matches_is_an_empty_result_not_an_error(profile):
    async with Client(make_server(Upstream(), profile)) as client:
        data = await call(client, "find_courses", {"query": "nomatch", "term": "Fall 2026"})
    assert data["courses"] == [] and data["total"] == 0 and data["has_more"] is False


@pytest.mark.anyio
async def test_find_courses_upstream_failure_is_a_tool_error():
    def respond(request):
        return httpx.Response(503, text="upstream diagnostic body")

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        message = await call_error(client, "find_courses", {"query": "csc", "term": "Fall 2026"})
        assert "HTTP 503" in message and "upstream diagnostic body" not in message
        message = await call_error(client, "find_courses", {"query": "csc"})
        assert "current-session" in message


@pytest.mark.anyio
async def test_find_courses_unexpected_shape_is_page_changed_error():
    def respond(request):
        return httpx.Response(200, json={"payload": []})

    async with Client(create_server(httpx.MockTransport(respond))) as client:
        message = await call_error(client, "find_courses", {"query": "csc", "term": "Fall 2026"})
        assert "UofT changed the Timetable Builder page" in message
        message = await call_error(client, "find_courses", {"query": "csc"})
        assert "UofT changed the Timetable Builder page" in message
