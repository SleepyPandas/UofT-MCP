"""Small result projections reusable by MCP and programmatic callers."""

from typing import Any

from uoft_mcp.acorn.client import AcornError

# The registry documents these fields even when no registration rows are returned.
REGISTRATION_FIELDS = frozenset(
    {
        "candidacyPostCode",
        "candidacySessionCode",
        "sessionDescription",
        "post",
        "registrationParams",
    }
)


def validate_fields(fields: list[str] | None) -> None:
    if fields is not None and (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field.strip() for field in fields)
    ):
        raise AcornError("fields must be a nonempty list of field names.", "invalid_fields")


def select_fields(data: dict | list, fields: list[str] | None = None) -> Any:
    """Project objects or registration rows without modifying upstream values.

    For heterogeneous rows, a selected field is included only where present.
    Validate against their union; for an empty array use documented registration keys.
    """
    validate_fields(fields)
    if fields is None:
        return data
    rows = data if isinstance(data, list) else [data]
    if any(not isinstance(row, dict) for row in rows):
        raise AcornError("ACORN returned registration rows with an unexpected JSON shape.")
    available = set().union(*(row.keys() for row in rows)) if rows else REGISTRATION_FIELDS
    if any(field not in available for field in fields):
        # Do not echo upstream keys or user input into errors.
        raise AcornError(
            "Unknown field selection. Omit fields to inspect the available top-level keys.",
            "invalid_fields",
        )
    selected = [{field: row[field] for field in fields if field in row} for row in rows]
    return selected if isinstance(data, list) else selected[0]
