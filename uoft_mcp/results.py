"""Bounded JSON views and transient, process-local snapshots. No record persistence."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VIEW_BYTES = 16 * 1024


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Selection(BaseModel):
    """Select a subtree, filter rows, page, then project immediate object fields."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["preview", "keys"] = "preview"
    pointer: str = Field(default="", max_length=1024)
    fields: list[str] | None = Field(default=None, min_length=1, max_length=100)
    equals: dict[str, Any] | None = Field(default=None, max_length=20)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("pointer")
    @classmethod
    def valid_pointer(cls, value: str) -> str:
        if value and (not value.startswith("/") or re.search(r"~(?![01])", value)):
            raise ValueError("Use an RFC 6901 JSON Pointer, or empty string for the root.")
        return value


class ResultError(ValueError):
    """Safe error text with no record values or caller input."""


def subtree(data: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:] if pointer else []:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(data, dict) and key in data:
            data = data[key]
        elif isinstance(data, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            index = int(key)
            if index >= len(data):
                raise ResultError("JSON Pointer does not exist in this result.")
            data = data[index]
        else:
            raise ResultError("JSON Pointer does not exist in this result.")
    return data


def kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def equal(left: Any, right: Any) -> bool:
    """JSON value equality: object order is irrelevant; booleans are not numbers."""
    if kind(left) != kind(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def overview(data: Any) -> dict:
    result: dict = {"type": kind(data)}
    if isinstance(data, (dict, list, str)):
        result["size"] = len(data)
    if isinstance(data, dict):
        # Huge keys must not make the overview itself exceed the response budget.
        keys = []
        for key, value in data.items():
            if (
                len(keys) >= 20
                or len(encode(keys + [{"key": key, "type": kind(value)}]).encode()) > 4096
            ):
                break
            keys.append({"key": key, "type": kind(value)})
        result.update(keys=keys, keys_complete=len(keys) == len(data))
    return result


def view(data: Any, selection: Selection) -> dict:
    selected = subtree(data, selection.pointer)
    if selection.mode == "keys":
        if not isinstance(selected, dict):
            raise ResultError("Key inspection requires an object.")
        selected = [{"key": key, "type": kind(value)} for key, value in selected.items()]
    is_array = isinstance(selected, list)
    if selection.equals is not None:
        if not is_array:
            raise ResultError("Equality filters require an array of records.")
        selected = [
            row
            for row in selected
            if isinstance(row, dict)
            and all(
                key in row and equal(row[key], value) for key, value in selection.equals.items()
            )
        ]
    total = len(selected) if is_array else None
    if not is_array and selection.offset:
        raise ResultError("Offset pagination requires an array.")
    page = selected[selection.offset : selection.offset + selection.limit] if is_array else selected
    if selection.fields is not None:
        rows = page if is_array else [page]
        if any(not isinstance(row, dict) for row in rows):
            raise ResultError("Field projection requires objects or an array of objects.")
        # Check the complete selected array, not just this page. Missing keys on
        # heterogeneous rows are omitted; empty arrays allow any field selection.
        source = selected if is_array else [selected]
        available = set().union(*(row.keys() for row in source if isinstance(row, dict)))
        if source and any(field not in available for field in selection.fields):
            raise ResultError("Unknown field selection. Inspect the result without fields.")
        projected = [{key: row[key] for key in selection.fields if key in row} for row in rows]
        page = projected if is_array else projected[0]
    fits = len(encode(page).encode("utf-8")) <= VIEW_BYTES
    next_offset = (
        selection.offset + len(page) if is_array and selection.offset + len(page) < total else None
    )
    result = {
        "pointer": selection.pointer,
        "complete": fits and (not is_array or (selection.offset == 0 and next_offset is None)),
        "projected": selection.fields is not None,
        "filtered": selection.equals is not None,
        "total": total,
        "offset": selection.offset if is_array else None,
        "next_offset": next_offset if fits else None,
    }
    if fits:
        result["data"] = page
    else:
        result.update(
            overview=overview(selected),
            guidance="Narrow the pointer, fields, or limit with uoft_result.",
        )
    return result


@dataclass
class Snapshot:
    data: Any
    private: bool
    generation: int
    retrieved_at: str
    expires: float
    size: int


class ResultStore:
    def __init__(
        self, *, clock=time.monotonic, ttl=300, max_entries=32, max_bytes=32 * 1024 * 1024
    ):
        self.clock = clock
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries: OrderedDict[str, Snapshot] = OrderedDict()
        self.generation = 0

    def invalidate_private(self) -> None:
        self.generation += 1
        for key in list(self.entries):
            if self.entries[key].private:
                del self.entries[key]

    def clear(self) -> None:
        self.generation += 1
        self.entries.clear()

    def _expire(self) -> None:
        for key in list(self.entries):
            if self.entries[key].expires <= self.clock():
                del self.entries[key]

    def put(
        self, data: Any, *, private: bool, generation: int
    ) -> tuple[str | None, str, str | None]:
        self._expire()
        retrieved = datetime.now(UTC).isoformat()
        if private and generation != self.generation:
            raise ResultError("Authentication changed during this read. Read again after login.")
        size = len(encode(data).encode("utf-8"))
        if size > self.max_bytes:
            return (
                None,
                retrieved,
                "Result exceeds the snapshot budget; follow-up reads must refetch.",
            )
        while self.entries and (
            len(self.entries) >= self.max_entries
            or sum(entry.size for entry in self.entries.values()) + size > self.max_bytes
        ):
            self.entries.popitem(last=False)
        handle = secrets.token_urlsafe(24)
        self.entries[handle] = Snapshot(
            data, private, generation, retrieved, self.clock() + self.ttl, size
        )
        return handle, retrieved, None

    def get(self, handle: str) -> Snapshot:
        self._expire()
        if handle not in self.entries:
            raise ResultError("Result unavailable or expired. Fetch it again with uoft_read.")
        self.entries.move_to_end(handle)
        return self.entries[handle]
