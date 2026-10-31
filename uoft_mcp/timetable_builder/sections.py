"""Pure checks over raw Timetable Builder sections: which sections may be taken together.

A section's ``linkedMeetingSections`` lists the sections it may be taken with, for example
a tutorial that only goes with one lecture. The solver ignores these links, so the planner
validates (and repairs) its output with them.
"""

import itertools
import re
from collections.abc import Iterable, Mapping
from typing import Any

from uoft_mcp.timetable_builder.format import is_open, plain_text

_NAME = re.compile(r"([A-Za-z]+)(.*)")
_RESTRICTION = re.compile(r"\bmust\s+(?:also\s+)?enrol", re.IGNORECASE)
_RESTRICTION_CHARS = 300

Identity = tuple[str, str]


def _identity(section: Mapping[str, Any]) -> Identity:
    method, number = section.get("teachMethod"), section.get("sectionNumber")
    if isinstance(method, str) and isinstance(number, str):
        return method, number
    name = str(section.get("name") or "")
    found = _NAME.fullmatch(name)
    return (found.group(1), found.group(2)) if found else ("", name)


def _links(section: Mapping[str, Any]) -> dict[str, set[Identity]]:
    """The sections this one may be taken with, grouped by their teaching method."""
    links = section.get("linkedMeetingSections")
    grouped: dict[str, set[Identity]] = {}
    for link in links if isinstance(links, list) else []:
        method = link.get("teachMethod") if isinstance(link, Mapping) else None
        number = link.get("sectionNumber") if isinstance(link, Mapping) else None
        if isinstance(method, str) and isinstance(number, str):
            grouped.setdefault(method, set()).add((method, number))
    return grouped


def _label(identity: Identity) -> str:
    return f"{identity[0]}{identity[1]}"


def pairing_problems(code: str, sections: Iterable[Mapping[str, Any]]) -> list[str]:
    """Explain every chosen section that is restricted to other sections than the ones chosen."""
    chosen = list(sections)
    identities = {_identity(section) for section in chosen}
    problems = []
    for section in chosen:
        for method, allowed in _links(section).items():
            if identities & allowed:
                continue
            present = sorted(_label(i) for i in identities if i[0] == method) or ["none"]
            problems.append(
                f"{code} {section.get('name')} is only for "
                f"{'/'.join(sorted(_label(i) for i in allowed))}, "
                f"but the plan has {'/'.join(present)}"
            )
    return problems


def compatible_combinations(
    sections: Iterable[Mapping[str, Any]], activity_types: list[str]
) -> list[list[Mapping[str, Any]]]:
    """Every choice of one usable section per type whose links agree with each other."""
    available = [s for s in sections if is_open(s)]
    by_type = [[s for s in available if s.get("type") == kind] for kind in activity_types]
    return [
        list(combination)
        for combination in itertools.product(*by_type)
        if not pairing_problems("", combination)
    ]


def unchecked_restriction(offering: Mapping[str, Any]) -> str | None:
    """Course-note enrolment instructions the section data does not encode, if any.

    Offerings whose sections carry links are validated from those links instead.
    """
    sections = [s for s in offering.get("sections") or [] if isinstance(s, Mapping)]
    if any(_links(section) for section in sections):
        return None
    for note in offering.get("notes") or []:
        text = plain_text(note.get("content")) if isinstance(note, Mapping) else None
        if text and _RESTRICTION.search(text):
            if len(text) > _RESTRICTION_CHARS:
                return text[:_RESTRICTION_CHARS].rstrip() + "..."
            return text
    return None
