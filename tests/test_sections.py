"""Section link checks over raw Timetable Builder sections: no network, no MCP server."""

from uoft_mcp.timetable_builder.sections import (
    compatible_combinations,
    pairing_problems,
    unchecked_restriction,
)


def row(name, kind, links=(), **extra):
    """A section shaped like the upstream one: lectures link nothing, others link lectures."""
    return {
        "name": name,
        "type": kind,
        "teachMethod": name[:3],
        "sectionNumber": name[3:],
        "linkedMeetingSections": [
            {"teachMethod": "LEC", "sectionNumber": number, "type": None} for number in links
        ],
        **extra,
    }


CSC236 = [
    row("LEC0101", "Lecture"),
    row("LEC0201", "Lecture"),
    row("TUT0101", "Tutorial", ["0101"]),
    row("TUT0201", "Tutorial", ["0201"]),
    row("TUT0202", "Tutorial", ["0201"]),
    row("TUT0401", "Tutorial", ["0401"]),
]
CSC258 = [
    row("LEC0101", "Lecture"),
    row("LEC0201", "Lecture"),
    row("LEC5101", "Lecture"),
    row("PRA5101", "Practical", ["0101"]),
    row("PRA5201", "Practical", ["0201"]),
    row("PRA5301", "Practical", ["5101"]),
]


def pick(sections, *names):
    return [s for s in sections if s["name"] in names]


def test_csc236_lec0201_with_tut0401_is_rejected():
    problems = pairing_problems("CSC236H1", pick(CSC236, "LEC0201", "TUT0401"))
    assert problems == ["CSC236H1 TUT0401 is only for LEC0401, but the plan has LEC0201"]


def test_csc258_pairings_follow_links_not_numeric_suffixes():
    assert pairing_problems("CSC258H1", pick(CSC258, "LEC5101", "PRA5301")) == []
    assert pairing_problems("CSC258H1", pick(CSC258, "LEC0201", "PRA5201")) == []
    assert pairing_problems("CSC258H1", pick(CSC258, "LEC5101", "PRA5201")) == [
        "CSC258H1 PRA5201 is only for LEC0201, but the plan has LEC5101"
    ]
    assert pairing_problems("CSC258H1", pick(CSC258, "LEC0201", "PRA5101")) == [
        "CSC258H1 PRA5101 is only for LEC0101, but the plan has LEC0201"
    ]


def test_sections_without_links_are_unrestricted():
    free = [row("LEC0101", "Lecture"), row("TUT0101", "Tutorial"), row("TUT0102", "Tutorial")]
    assert pairing_problems("MAT334H1", free) == []
    assert pairing_problems("MAT334H1", [{"name": "LEC0101", "linkedMeetingSections": None}]) == []


def test_missing_partner_type_is_reported_as_none():
    assert pairing_problems("CSC236H1", pick(CSC236, "TUT0101")) == [
        "CSC236H1 TUT0101 is only for LEC0101, but the plan has none"
    ]


def test_malformed_links_are_ignored():
    odd = {"name": "TUT0101", "linkedMeetingSections": ["junk", {"teachMethod": 1}, None]}
    assert pairing_problems("X", [odd]) == []


def test_identity_falls_back_to_the_section_name():
    lecture = {"name": "LEC0201", "type": "Lecture"}
    tutorial = {
        "name": "TUT0201",
        "type": "Tutorial",
        "linkedMeetingSections": [{"teachMethod": "LEC", "sectionNumber": "0201"}],
    }
    assert pairing_problems("X", [lecture, tutorial]) == []
    other = {"name": "LEC0301", "type": "Lecture"}
    assert pairing_problems("X", [other, tutorial])


def test_compatible_combinations_only_pair_allowed_sections():
    names = sorted(
        tuple(s["name"] for s in combination)
        for combination in compatible_combinations(CSC236, ["Lecture", "Tutorial"])
    )
    assert names == [
        ("LEC0101", "TUT0101"),
        ("LEC0201", "TUT0201"),
        ("LEC0201", "TUT0202"),
    ]


def test_compatible_combinations_skip_cancelled_and_tba_sections():
    sections = [
        row("LEC0101", "Lecture"),
        row("TUT0101", "Tutorial", ["0101"], cancelInd="Y"),
        row("TUT0102", "Tutorial", ["0101"], tbaInd="Y"),
        row("TUT0103", "Tutorial", ["0101"]),
    ]
    (only,) = compatible_combinations(sections, ["Lecture", "Tutorial"])
    assert [s["name"] for s in only] == ["LEC0101", "TUT0103"]
    assert compatible_combinations(sections, ["Lecture", "Practical"]) == []


def test_unchecked_restriction_reports_notes_the_sections_do_not_encode():
    note = "<p>Students in LEC0101 must enrol in TUT0101.</p>"
    offering = {"sections": [row("LEC0101", "Lecture")], "notes": [{"content": note}]}
    assert unchecked_restriction(offering) == "Students in LEC0101 must enrol in TUT0101."
    linked_offering = {**offering, "sections": CSC236}
    assert unchecked_restriction(linked_offering) is None
    assert unchecked_restriction({"sections": [], "notes": [{"content": "<p>Hello</p>"}]}) is None
    assert unchecked_restriction({"sections": None, "notes": None}) is None
    long_note = {"notes": [{"content": "You must enrol in " + "x" * 400}], "sections": []}
    text = unchecked_restriction(long_note)
    assert text.endswith("...") and len(text) == 303
