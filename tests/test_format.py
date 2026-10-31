"""Pure formatter tests: no network, no MCP server."""

import pytest

from uoft_mcp.timetable_builder.format import (
    MAX_SECTIONS_PER_TYPE,
    NOTE_CHARS,
    UnexpectedShape,
    clock,
    course_card,
    course_summary,
    current_session_codes,
    explicit_session_codes,
    extract_courses,
    format_meeting,
    format_section,
    lectures_full,
    matched_in,
    page_window,
    plain_text,
    seats,
    session_label,
    term_label,
    text_list,
)

CURRENT_SESSIONS = {
    "payload": [
        {"label": "Fall-Winter 2026-2027", "value": "Fall-Winter", "header": True},
        {"label": "Fall 2026 (F)", "value": "20269", "header": False},
        {"label": "Winter 2027  (S)", "value": "20271", "header": False},
        {"label": "Fall-Winter 2026-2027 (Y)", "value": "20269-20271", "header": False},
    ]
}


def meeting(day=4, start=32_400_000, end=39_600_000, code="BA", room="", **extra):
    return {
        "start": {"day": day, "millisofday": start},
        "end": {"day": day, "millisofday": end},
        "building": {"buildingCode": code, "buildingRoomNumber": room},
        "sessionCode": "20269",
        "repetition": "WEEKLY",
        **extra,
    }


def section(name="LEC0101", kind="Lecture", **overrides):
    return {
        "name": name,
        "type": kind,
        "meetingTimes": [meeting()],
        "instructors": [{"firstName": "Ada", "lastName": "Lovelace"}],
        "currentEnrolment": 37,
        "maxEnrolment": 40,
        "currentWaitlist": 0,
        "waitlistInd": "N",
        "deliveryModes": [{"session": "20269", "mode": "INPER"}],
        "cancelInd": "N",
        "notes": [{"name": "Section Note", "content": ""}],
        "enrolmentControls": [{"internal": True}],
        **overrides,
    }


def course(**overrides):
    return {
        "id": "abc123",
        "code": "CSC148H1",
        "name": "Introduction to Computer Science",
        "sectionCode": "F",
        "campus": "St. George",
        "sessions": ["20269"],
        "maxCredit": 0.5,
        "cmCourseInfo": {
            "description": "<p>Abstract data types &amp; recursion.</p>",
            "prerequisitesText": "<p>CSC108H1/ (equivalent&nbsp;experience)</p>",
            "corequisitesText": "",
            "exclusionsText": "<p>None</p>",
            "recommendedPreparation": None,
            "breadthRequirements": "['The Physical and Mathematical Universes (5)']",
            "distributionRequirements": ["Science"],
        },
        "notes": [{"name": "Course Note", "content": ""}],
        "primaryFull": False,
        "primaryWaitlistable": False,
        "fullyOnline": False,
        "cancelInd": "N",
        "sections": [section()],
        **overrides,
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<p>One</p><p>Two  <b>three</b></p>", "One Two three"),
        ("Tom &amp; Jerry&nbsp;&lt;3", "Tom & Jerry <3"),
        ("<ul><li>A</li><li>B</li></ul>", "A B"),
        ("plain\n  text", "plain text"),
        ("<script>alert(1)</script>Safe<style>p{}</style>", "Safe"),
        ("<p>unclosed <b>tags", "unclosed tags"),
        ("<p>None</p>", None),
        ("None.", None),
        ("   ", None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_plain_text(raw, expected):
    assert plain_text(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "['The Physical and Mathematical Universes (5)']",
            ["The Physical and Mathematical Universes (5)"],
        ),
        ("['A (1)', 'B (2)']", ["A (1)", "B (2)"]),
        (["A", "A", " B "], ["A", "B"]),
        ("[]", []),
        ("Science", ["Science"]),
        ("[not a list", ["[not a list"]),
        ("[__import__('os').system('echo hi')]", ["[__import__('os').system('echo hi')]"]),
        ("[" + "(" * 5000 + "]", ["[" + "(" * 5000 + "]"]),
        ("[1, None, 'ok']", ["ok"]),
        (None, []),
        (5, []),
        ({"a": 1}, []),
    ],
)
def test_text_list_never_evaluates_code(raw, expected):
    assert text_list(raw) == expected


@pytest.mark.parametrize(
    "code,label",
    [
        ("20269", "Fall 2026"),
        ("20271", "Winter 2027"),
        ("20265", "Summer 2026"),
        ("20269-20271", "Fall 2026 to Winter 2027"),
        ("20263", "20263"),
        ("junk", "junk"),
        ("20269-junk", "20269-junk"),
        (20269, "Fall 2026"),
    ],
)
def test_session_label(code, label):
    assert session_label(code) == label


def test_term_label():
    assert term_label(["20269"]) == "Fall 2026"
    assert term_label(["20269", "20271"]) == "Fall 2026 to Winter 2027"
    assert term_label(["20263"]) == "20263"
    assert term_label([]) is None
    assert term_label(None) is None


@pytest.mark.parametrize(
    "text,codes",
    [
        ("Fall 2026", ["20269"]),
        ("  winter   2027 ", ["20271"]),
        ("Summer 2026", ["20265"]),
        ("20269", ["20269"]),
        ("20269-20271", ["20269-20271"]),
        ("fall", None),
        ("Fall 26", None),
    ],
)
def test_explicit_session_codes(text, codes):
    assert explicit_session_codes(text) == codes


@pytest.mark.parametrize(
    "term,codes",
    [
        (None, ["20269", "20271", "20269-20271"]),
        ("fall", ["20269"]),
        ("Winter", ["20271"]),
        ("F", ["20269"]),
        ("s", ["20271"]),
        ("Y", ["20269-20271"]),
        ("year", ["20269-20271"]),
        ("Fall-Winter", ["20269-20271"]),
        ("Fall-Winter 2026-2027", ["20269-20271"]),
        ("fall 2026", ["20269"]),
        ("20271", ["20271"]),
    ],
)
def test_current_session_codes(term, codes):
    assert current_session_codes(CURRENT_SESSIONS, term) == codes


def test_current_session_codes_errors():
    with pytest.raises(ValueError, match=r"Unknown term 'spring'.*Fall 2026 \(F\), Winter 2027"):
        current_session_codes(CURRENT_SESSIONS, "spring")
    with pytest.raises(UnexpectedShape):
        current_session_codes({"payload": "nope"}, None)
    with pytest.raises(UnexpectedShape):
        current_session_codes([], None)


@pytest.mark.parametrize(
    "millis,text",
    [(0, "00:00"), (32_400_000, "09:00"), (50_400_000, "14:00"), (45_900_000, "12:45")],
)
def test_clock(millis, text):
    assert clock(millis) == text


@pytest.mark.parametrize("bad", [None, "9", True])
def test_clock_rejects_non_numbers(bad):
    assert clock(bad) is None


def test_format_meeting_variants():
    assert format_meeting(meeting(room="1130")) == "Thu 09:00-11:00 BA 1130"
    assert format_meeting(meeting()) == "Thu 09:00-11:00 BA"
    assert format_meeting(meeting(code="", room="")) == "Thu 09:00-11:00"
    assert format_meeting(meeting(building=None)) == "Thu 09:00-11:00"
    assert format_meeting(meeting(repetition="BIWEEKLY")) == "Thu 09:00-11:00 BA (biweekly)"
    assert format_meeting(meeting(day=1), with_term=True) == "Fall 2026: Mon 09:00-11:00 BA"
    assert format_meeting(meeting(day=7)) == "Sun 09:00-11:00 BA"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "Mon 9",
        {},
        {"start": {"day": 1, "millisofday": 0}},
        meeting(day=0),
        meeting(day=8),
        meeting(start=None),
        {"start": {"day": "1", "millisofday": 0}, "end": {"day": 1, "millisofday": 1}},
    ],
)
def test_format_meeting_skips_unusable_meetings(bad):
    assert format_meeting(bad) is None


def test_section_full_waitlist_cancelled_and_missing_fields():
    plain = format_section(section())
    assert plain == {
        "name": "LEC0101",
        "instructors": ["Ada Lovelace"],
        "meetings": ["Thu 09:00-11:00 BA"],
        "enrolment": "37/40",
        "delivery": "In Person",
    }
    full = format_section(
        section(
            currentEnrolment=40,
            currentWaitlist=3,
            waitlistInd="Y",
            cancelInd="Y",
            notes=[{"content": "<p>Restricted</p>"}],
            deliveryModes=[
                {"session": "20269", "mode": "ONLASYNC"},
                {"session": "20271", "mode": "ONLASYNC"},
                {"session": "20271", "mode": "NEWMODE"},
            ],
        )
    )
    assert full["full"] is True and full["cancelled"] is True and full["waitlist"] == 3
    assert full["note"] == "Restricted"
    assert full["delivery"] == "Online Asynchronous, NEWMODE"
    open_waitlist = format_section(section(waitlistInd="Y"))
    assert open_waitlist["waitlist"] == 0

    sparse = format_section(
        {
            "name": "LEC0101",
            "meetingTimes": None,
            "instructors": [None, {"firstName": "Only"}, {"lastName": "Last"}, {}],
            "currentEnrolment": None,
            "maxEnrolment": None,
            "deliveryModes": None,
            "notes": None,
        }
    )
    assert sparse == {"name": "LEC0101", "instructors": ["Only", "Last"], "meetings": []}
    unlimited = format_section(section(maxEnrolment=0, currentEnrolment=5))
    assert unlimited["enrolment"] == "5" and "full" not in unlimited


def test_section_meetings_are_sorted_and_tagged_for_year_courses():
    row = section(
        meetingTimes=[
            meeting(day=3, start=32_400_000, end=39_600_000),
            meeting(day=1, start=36_000_000, end=39_600_000),
            meeting(day=1, start=28_800_000, end=32_400_000, sessionCode="20271"),
        ]
    )
    assert format_section(row)["meetings"] == [
        "Mon 08:00-09:00 BA",
        "Mon 10:00-11:00 BA",
        "Wed 09:00-11:00 BA",
    ]
    tagged = format_section(row, multi_term=True)["meetings"]
    assert tagged[0] == "Winter 2027: Mon 08:00-09:00 BA"
    assert tagged[1] == "Fall 2026: Mon 10:00-11:00 BA"


def test_course_card_content_and_privacy():
    card = course_card(course())
    assert card == {
        "code": "CSC148H1",
        "title": "Introduction to Computer Science",
        "term": "Fall 2026",
        "campus": "St. George",
        "credits": 0.5,
        "id": "abc123",
        "description": "Abstract data types & recursion.",
        "prerequisites": "CSC108H1/ (equivalent experience)",
        "breadth": ["The Physical and Mathematical Universes (5)"],
        "distribution": ["Science"],
        "lectures_full": False,
        "sections": {
            "Lecture": [
                {
                    "name": "LEC0101",
                    "instructors": ["Ada Lovelace"],
                    "meetings": ["Thu 09:00-11:00 BA"],
                    "enrolment": "37/40",
                    "delivery": "In Person",
                }
            ]
        },
    }
    assert "enrolmentControls" not in str(card)


def test_course_card_flags_and_requirement_fields():
    card = course_card(
        course(
            primaryFull=True,
            primaryWaitlistable=True,
            fullyOnline=True,
            cancelInd="Y",
            notes=[{"content": "<p>First</p>"}, {"content": "First"}, {"content": "<b>Second</b>"}],
            cmCourseInfo={
                "corequisitesText": "<p>MAT137Y1</p>",
                "exclusionsText": "<p>MAT157Y1</p>",
                "recommendedPreparation": "<p>Calculus</p>",
            },
        )
    )
    assert card["lectures_full"] is True
    assert card["waitlistable"] is True and card["fully_online"] is True
    assert card["cancelled"] is True
    assert card["notes"] == ["First", "Second"]
    assert card["corequisites"] == "MAT137Y1"
    assert card["exclusions"] == "MAT157Y1"
    assert card["recommended_preparation"] == "Calculus"
    assert "prerequisites" not in card and "description" not in card
    assert "breadth" not in card and "distribution" not in card


def test_course_card_survives_missing_everything():
    card = course_card({})
    assert card["sections"] == {}
    assert card["term"] is None and card["code"] is None
    assert (
        course_card({"cmCourseInfo": None, "sections": None, "sessions": "bad"})["sections"] == {}
    )


def test_course_card_breadth_falls_back_to_campus_breadth_types():
    card = course_card(
        course(
            cmCourseInfo={},
            breadths=[
                {
                    "org": {"code": "ERIN"},
                    "breadthTypes": [
                        {"kind": "BREADTH", "type": "Humanities", "description": "Humanities"},
                        {"kind": "DISTRIBUTION", "type": "Science", "description": "Science"},
                        {"kind": "BREADTH", "type": "Humanities", "description": "Humanities"},
                        "junk",
                    ],
                },
                {"org": {"code": "SCAR"}, "breadthTypes": []},
                "junk",
            ],
        )
    )
    assert card["breadth"] == ["Humanities"]
    assert card["distribution"] == ["Science"]


def test_course_card_year_course_labels_term_and_meetings():
    card = course_card(
        course(sessions=["20269", "20271"], sections=[section(meetingTimes=[meeting()])])
    )
    assert card["term"] == "Fall 2026 to Winter 2027"
    assert card["sections"]["Lecture"][0]["meetings"] == ["Fall 2026: Thu 09:00-11:00 BA"]


def test_course_card_groups_sorts_and_truncates_sections():
    many = [section(name=f"TUT{n:04d}", kind="Tutorial") for n in range(MAX_SECTIONS_PER_TYPE + 5)]
    many.reverse()
    card = course_card(course(sections=[section(), *many, section(name="PRA0101", kind=None)]))
    tutorials = card["sections"]["Tutorial"]
    assert len(tutorials) == MAX_SECTIONS_PER_TYPE
    assert tutorials[0]["name"] == "TUT0000"
    assert tutorials[-1]["name"] == f"TUT{MAX_SECTIONS_PER_TYPE - 1:04d}"
    assert card["sections_omitted"] == {"Tutorial": 5}
    assert len(card["sections"]["Lecture"]) == 1
    assert [s["name"] for s in card["sections"]["Other"]] == ["PRA0101"]


def test_course_card_has_no_omitted_marker_when_everything_fits():
    assert "sections_omitted" not in course_card(course())


def test_course_summary_and_matched_in():
    long_description = "word " * 100
    row = course(cmCourseInfo={"description": long_description, "breadthRequirements": ["B (1)"]})
    summary = course_summary(row, "machine")
    assert summary["description"].endswith("...") and len(summary["description"]) <= 163
    assert summary["breadth"] == ["B (1)"]
    assert summary["lectures_full"] is False
    assert summary["matched_in"] == "description"
    assert "sections" not in summary and "id" not in summary
    assert "matched_in" not in course_summary(row)
    assert matched_in(row, "csc148") == "code"
    assert matched_in(row, "computer science") == "title"
    assert matched_in(row, "") == "description"


def test_extract_courses_shapes():
    rows, total = extract_courses(
        {"payload": {"pageableCourse": {"courses": [{"code": "A"}, "junk"], "total": 9}}}
    )
    assert rows == [{"code": "A"}] and total == 9
    assert extract_courses({"payload": {"pageableCourse": {"courses": [], "total": "x"}}}) == (
        [],
        None,
    )
    for bad in (None, [], {"payload": []}, {"payload": {"pageableCourse": {"courses": None}}}):
        with pytest.raises(UnexpectedShape):
            extract_courses(bad)


@pytest.mark.parametrize(
    "page,size,expected",
    [
        (1, 10, (1, 0)),
        (2, 10, (1, 10)),
        (3, 10, (2, 0)),
        (2, 15, (1, 15)),
        (3, 15, (2, 10)),
        (1, 20, (1, 0)),
    ],
)
def test_page_window(page, size, expected):
    assert page_window(page, size) == expected


def primary_course(*lectures, upstream=False, **overrides):
    sections = [section(f"LEC{n:04d}", teachMethod="LEC", **row) for n, row in enumerate(lectures)]
    return course(primaryTeachMethod="LEC", primaryFull=upstream, sections=sections, **overrides)


FULL_LECTURE = {"currentEnrolment": 196, "maxEnrolment": 196, "waitlistInd": "Y"}


def test_lectures_full_comes_from_section_counts_and_reports_upstream_disagreement():
    mat334 = primary_course(FULL_LECTURE, {**FULL_LECTURE, "currentWaitlist": 32})
    card = course_card(mat334)
    assert lectures_full(mat334) is True
    assert card["lectures_full"] is True and card["upstream_lectures_full"] is False
    assert course_summary(mat334)["upstream_lectures_full"] is False


def test_lectures_full_is_false_while_any_open_primary_section_has_seats():
    row = primary_course(FULL_LECTURE, {"currentEnrolment": 150, "maxEnrolment": 156})
    assert lectures_full(row) is False
    cancelled = primary_course(FULL_LECTURE, {"currentEnrolment": 1, "cancelInd": "Y"})
    assert lectures_full(cancelled) is True
    assert "upstream_lectures_full" not in course_card(primary_course(FULL_LECTURE, upstream=True))


def test_lectures_full_falls_back_to_upstream_without_primary_sections():
    assert lectures_full(course(primaryFull=True)) is True
    assert lectures_full(course(primaryFull=False, sections=[])) is False
    assert lectures_full(course(primaryFull=False, sections=[section(teachMethod=None)])) is False
    assert "upstream_lectures_full" not in course_card(course(primaryFull=True, sections=[]))


def test_seats_text():
    assert seats(section()) == "37/40 enrolled"
    assert seats(section(currentEnrolment=40)) == "40/40 enrolled, FULL, no waitlist"
    full = section(currentEnrolment=40, waitlistInd="Y", currentWaitlist=3)
    assert seats(full) == "40/40 enrolled, FULL, waitlist 3"
    assert seats(section(maxEnrolment=0)) is None
    assert seats(section(currentEnrolment=None)) is None


def test_long_course_notes_keep_instructions_and_mark_any_truncation():
    pairing = "Students enrolled in LEC0201 must enrol in TUT0201, TUT0202, TUT0203, or TUT0204"
    notes = [
        {"content": "<p><strong>Timetable Instructions</strong></p>" + f"<p>{pairing}</p>" * 5}
    ]
    kept = course_card(course(notes=notes))["notes"][0]
    assert kept.count(pairing) == 5
    assert "truncated" not in kept

    huge = "word " * NOTE_CHARS
    clipped = course_card(course(notes=[{"content": huge}]))["notes"][0]
    assert "[note truncated, " in clipped and clipped.endswith("more characters]")
    dropped = len(huge.strip()) - len(clipped.split(" [note truncated")[0])
    assert f"{dropped} more characters" in clipped


def test_extra_course_notes_are_counted_not_silently_dropped():
    notes = [{"content": f"Note {n}"} for n in range(5)]
    assert course_card(course(notes=notes))["notes"] == [
        "Note 0",
        "Note 1",
        "Note 2",
        "[2 more notes omitted]",
    ]
