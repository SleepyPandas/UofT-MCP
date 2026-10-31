"""Pure, I/O-free normalizers that turn raw Timetable Builder courses into readable cards.

The upstream course object is large and awkward (HTML fragments, stringified lists,
milliseconds-since-midnight, internal enrolment-control tables). These helpers keep
only what answers research questions: what a course is, what it requires, when it
meets, and whether it is full.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from typing import Any

MAX_SECTIONS_PER_TYPE = 30
SUMMARY_DESCRIPTION_CHARS = 160
NOTE_CHARS = 400
UPSTREAM_PAGE_SIZE = 20

_SEASON_MONTH_DIGITS = {"fall": "9", "winter": "1", "summer": "5"}
_MONTH_DIGIT_SEASONS = {
    digit: season.capitalize() for season, digit in _SEASON_MONTH_DIGITS.items()
}
_SESSION_CODE = re.compile(r"^(\d{4})([159])$")
_RAW_SESSION = re.compile(r"^\d{5}(?:-\d{5})?$")
_SEASON_YEAR = re.compile(r"^(fall|winter|summer)\s+(\d{4})$")
_LABEL_SUFFIX = re.compile(r"\s*\(([A-Za-z])\)\s*$")
_EMPTY_TEXT = re.compile(r"^(?:none|n/a|nil)\.?$", re.IGNORECASE)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DELIVERY_LABELS = {
    "INPER": "In Person",
    "ONLSYNC": "Online Synchronous",
    "ONLASYNC": "Online Asynchronous",
    "HYBRID": "Hybrid",
}
_BLOCK_TAGS = frozenset(
    {"p", "br", "li", "ul", "ol", "div", "tr", "td", "th", "table", "h1", "h2", "h3", "h4", "h5"}
)
_MAX_LIST_TEXT = 2000
_MILLIS_PER_MINUTE = 60_000


class UnexpectedShape(ValueError):
    """The upstream payload no longer looks like the course shape this module knows."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def plain_text(value: Any) -> str | None:
    """Strip HTML, collapse whitespace, and drop empty or "None" values."""
    if not isinstance(value, str):
        return None
    extractor = _TextExtractor()
    extractor.feed(value)
    extractor.close()
    text = " ".join("".join(extractor.parts).split())
    return None if not text or _EMPTY_TEXT.match(text) else text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,;:.") + "..."


def text_list(value: Any) -> list[str]:
    """Normalize a list, or a stringified Python list such as "['A (5)']", into clean strings."""
    items: Iterable[Any]
    if isinstance(value, str):
        stripped = value.strip()
        items = [stripped]
        if stripped.startswith("[") and stripped.endswith("]") and len(stripped) <= _MAX_LIST_TEXT:
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                parsed = None
            if isinstance(parsed, list | tuple):
                items = parsed
    elif isinstance(value, list | tuple):
        items = value
    else:
        return []
    cleaned: list[str] = []
    for item in items:
        text = plain_text(item) if isinstance(item, str) else None
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def session_label(code: Any) -> str:
    """Map a session code such as 20269 to "Fall 2026"; unknown codes are returned unchanged."""
    text = str(code)
    match = _SESSION_CODE.match(text)
    if match is not None:
        return f"{_MONTH_DIGIT_SEASONS[match.group(2)]} {match.group(1)}"
    first, _, last = text.partition("-")
    if _SESSION_CODE.match(first) and _SESSION_CODE.match(last):
        return f"{session_label(first)} to {session_label(last)}"
    return text


def term_label(codes: Any) -> str | None:
    """Label a course's sessions: one term, or "first to last" for year-long courses."""
    if not isinstance(codes, list) or not codes:
        return None
    labels = [session_label(code) for code in codes]
    return labels[0] if len(labels) == 1 else f"{labels[0]} to {labels[-1]}"


def clock(millis: Any) -> str | None:
    if isinstance(millis, bool) or not isinstance(millis, int | float):
        return None
    minutes = int(millis) // _MILLIS_PER_MINUTE
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _meeting_sort_key(meeting: Mapping[str, Any]) -> tuple[int, int]:
    start = meeting.get("start")
    start = start if isinstance(start, Mapping) else {}
    day, millis = start.get("day"), start.get("millisofday")
    return (day if isinstance(day, int) else 99, millis if isinstance(millis, int | float) else 0)


def format_meeting(meeting: Any, *, with_term: bool = False) -> str | None:
    """Render one meeting as "Thu 09:00-11:00 BA 1130"; None when it has no usable time."""
    if not isinstance(meeting, Mapping):
        return None
    start, end = meeting.get("start"), meeting.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    day = start.get("day")
    begin, finish = clock(start.get("millisofday")), clock(end.get("millisofday"))
    if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= len(_WEEKDAYS):
        return None
    if begin is None or finish is None:
        return None
    parts = [f"{_WEEKDAYS[day - 1]} {begin}-{finish}"]
    building = meeting.get("building")
    if isinstance(building, Mapping):
        place = " ".join(
            str(building.get(key) or "").strip() for key in ("buildingCode", "buildingRoomNumber")
        ).strip()
        if place:
            parts.append(place)
    repetition = meeting.get("repetition")
    if isinstance(repetition, str) and repetition and repetition.upper() != "WEEKLY":
        parts.append(f"({repetition.lower().replace('_', ' ')})")
    text = " ".join(parts)
    code = meeting.get("sessionCode")
    return f"{session_label(code)}: {text}" if with_term and code else text


def _instructors(section: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for person in section.get("instructors") or []:
        if not isinstance(person, Mapping):
            continue
        name = f"{person.get('firstName') or ''} {person.get('lastName') or ''}".strip()
        if name and name not in names:
            names.append(name)
    return names


def _delivery(section: Mapping[str, Any]) -> str | None:
    labels: list[str] = []
    for entry in section.get("deliveryModes") or []:
        mode = entry.get("mode") if isinstance(entry, Mapping) else None
        if isinstance(mode, str) and mode:
            label = _DELIVERY_LABELS.get(mode, mode)
            if label not in labels:
                labels.append(label)
    return ", ".join(labels) or None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def format_section(section: Mapping[str, Any], *, multi_term: bool = False) -> dict[str, Any]:
    meetings = sorted(
        (m for m in section.get("meetingTimes") or [] if isinstance(m, Mapping)),
        key=_meeting_sort_key,
    )
    card: dict[str, Any] = {
        "name": section.get("name"),
        "instructors": _instructors(section),
        "meetings": [
            line
            for line in (format_meeting(m, with_term=multi_term) for m in meetings)
            if line is not None
        ],
    }
    current, maximum = _count(section.get("currentEnrolment")), _count(section.get("maxEnrolment"))
    if current is not None:
        card["enrolment"] = f"{current}/{maximum}" if maximum else str(current)
        if maximum and current >= maximum:
            card["full"] = True
    waitlist = _count(section.get("currentWaitlist")) or 0
    if waitlist or section.get("waitlistInd") == "Y":
        card["waitlist"] = waitlist
    delivery = _delivery(section)
    if delivery:
        card["delivery"] = delivery
    if section.get("cancelInd") == "Y":
        card["cancelled"] = True
    notes = _notes(section.get("notes"), limit=200, count=1)
    if notes:
        card["note"] = notes[0]
    return card


def _notes(value: Any, *, limit: int = NOTE_CHARS, count: int = 3) -> list[str]:
    notes: list[str] = []
    for note in value or []:
        text = plain_text(note.get("content")) if isinstance(note, Mapping) else None
        if text and text not in notes:
            notes.append(_truncate(text, limit))
    return notes[:count]


def _info(course: Mapping[str, Any]) -> Mapping[str, Any]:
    info = course.get("cmCourseInfo")
    return info if isinstance(info, Mapping) else {}


def _breadth_items(course: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    info = _info(course)
    breadth = text_list(info.get("breadthRequirements"))
    distribution = text_list(info.get("distributionRequirements"))
    if breadth or distribution:
        return breadth, distribution
    # Some offerings only carry per-campus breadth types, not the catalogue fields.
    for campus in course.get("breadths") or []:
        if not isinstance(campus, Mapping):
            continue
        for entry in campus.get("breadthTypes") or []:
            if not isinstance(entry, Mapping):
                continue
            label = plain_text(entry.get("description") or entry.get("type"))
            bucket = breadth if entry.get("kind") == "BREADTH" else distribution
            if label and label not in bucket:
                bucket.append(label)
    return breadth, distribution


def _sorted_sections(course: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sections = [s for s in course.get("sections") or [] if isinstance(s, Mapping)]
    return sorted(sections, key=lambda s: str(s.get("name") or ""))


def course_card(course: Mapping[str, Any]) -> dict[str, Any]:
    """Build the full readable card for one course offering."""
    info = _info(course)
    codes = course.get("sessions")
    card: dict[str, Any] = {
        "code": course.get("code"),
        "title": course.get("name") or info.get("title"),
        "term": term_label(codes),
        "campus": course.get("campus"),
        "credits": course.get("maxCredit"),
        "id": course.get("id"),
    }
    description = plain_text(info.get("description"))
    if description:
        card["description"] = description
    for key, field in (
        ("prerequisites", "prerequisitesText"),
        ("corequisites", "corequisitesText"),
        ("exclusions", "exclusionsText"),
        ("recommended_preparation", "recommendedPreparation"),
    ):
        text = plain_text(info.get(field))
        if text:
            card[key] = text
    breadth, distribution = _breadth_items(course)
    if breadth:
        card["breadth"] = breadth
    if distribution:
        card["distribution"] = distribution
    notes = _notes(course.get("notes"))
    if notes:
        card["notes"] = notes
    card["lectures_full"] = bool(course.get("primaryFull"))
    if course.get("primaryWaitlistable"):
        card["waitlistable"] = True
    if course.get("fullyOnline"):
        card["fully_online"] = True
    if course.get("cancelInd") == "Y":
        card["cancelled"] = True

    multi_term = isinstance(codes, list) and len(codes) > 1
    grouped: dict[str, list[dict[str, Any]]] = {}
    totals: dict[str, int] = {}
    for section in _sorted_sections(course):
        kind = str(section.get("type") or section.get("teachMethod") or "Other")
        totals[kind] = totals.get(kind, 0) + 1
        bucket = grouped.setdefault(kind, [])
        if len(bucket) < MAX_SECTIONS_PER_TYPE:
            bucket.append(format_section(section, multi_term=multi_term))
    card["sections"] = grouped
    omitted = {
        kind: totals[kind] - len(listed)
        for kind, listed in grouped.items()
        if totals[kind] > len(listed)
    }
    if omitted:
        card["sections_omitted"] = omitted
    return card


def course_summary(course: Mapping[str, Any], keyword: str | None = None) -> dict[str, Any]:
    """Build the short search-result row for one course offering."""
    info = _info(course)
    title = course.get("name") or info.get("title")
    summary: dict[str, Any] = {
        "code": course.get("code"),
        "title": title,
        "term": term_label(course.get("sessions")),
        "campus": course.get("campus"),
        "credits": course.get("maxCredit"),
    }
    description = plain_text(info.get("description"))
    if description:
        summary["description"] = _truncate(description, SUMMARY_DESCRIPTION_CHARS)
    breadth, distribution = _breadth_items(course)
    if breadth or distribution:
        summary["breadth"] = breadth + distribution
    summary["lectures_full"] = bool(course.get("primaryFull"))
    if keyword:
        summary["matched_in"] = matched_in(course, keyword)
    return summary


def matched_in(course: Mapping[str, Any], keyword: str) -> str:
    """Say where the keyword hit: the code, the title, or (otherwise) the description."""
    words = keyword.casefold().split()
    code = str(course.get("code") or "").casefold()
    title = str(course.get("name") or "").casefold()
    if words and all(word in code for word in words):
        return "code"
    if words and all(word in title for word in words):
        return "title"
    return "description"


def extract_courses(data: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    """Return the course rows and upstream total from a getPageableCourses-shaped payload."""
    payload = data.get("payload") if isinstance(data, Mapping) else None
    pageable = payload.get("pageableCourse") if isinstance(payload, Mapping) else None
    courses = pageable.get("courses") if isinstance(pageable, Mapping) else None
    if not isinstance(courses, list):
        raise UnexpectedShape("no pageableCourse.courses list")
    total = pageable.get("total")
    return (
        [course for course in courses if isinstance(course, Mapping)],
        total if isinstance(total, int) and not isinstance(total, bool) else None,
    )


def _current_entries(payload: Any) -> list[Mapping[str, Any]]:
    entries = payload.get("payload") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        raise UnexpectedShape("no current-session list")
    return [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and not entry.get("header") and entry.get("value")
    ]


def explicit_session_codes(text: str) -> list[str] | None:
    """Resolve a raw code ("20269") or "Fall 2026"-style name without any lookup."""
    cleaned = " ".join(text.split())
    if _RAW_SESSION.match(cleaned):
        return [cleaned]
    match = _SEASON_YEAR.match(cleaned.casefold())
    if match is None:
        return None
    return [f"{match.group(2)}{_SEASON_MONTH_DIGITS[match.group(1)]}"]


def current_session_codes(payload: Any, term: str | None) -> list[str]:
    """Pick session codes from current-session data; no term selects every current session."""
    entries = _current_entries(payload)
    if term is None:
        return [str(entry["value"]) for entry in entries]
    wanted = " ".join(term.casefold().split())
    aliases = {"year": "y", "full year": "y", "fall-winter": "y", "fall/winter": "y"}
    wanted = aliases.get(wanted, wanted)
    matched: list[str] = []
    for entry in entries:
        label = " ".join(str(entry.get("label") or "").split())
        suffix = _LABEL_SUFFIX.search(label)
        name = _LABEL_SUFFIX.sub("", label).casefold()
        letter = suffix.group(1).casefold() if suffix else ""
        # The first word is "fall" for "Fall 2026" but "fall-winter" for the year-long entry,
        # so a bare season never selects the Y entry.
        first_word = name.split(" ")[0]
        if wanted in {str(entry["value"]).casefold(), name, letter, first_word}:
            matched.append(str(entry["value"]))
    if matched:
        return matched
    available = ", ".join(
        " ".join(str(entry.get("label") or entry["value"]).split()) for entry in entries
    )
    raise ValueError(f"Unknown term {term!r}. Current terms: {available}.")


def page_window(page: int, page_size: int) -> tuple[int, int]:
    """Map a caller page onto (first upstream page, row offset within it)."""
    offset = (page - 1) * page_size
    return offset // UPSTREAM_PAGE_SIZE + 1, offset % UPSTREAM_PAGE_SIZE
