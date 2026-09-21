"""Typed operation registry shared by compact dispatch and legacy registration."""

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

DIRECT_TOOLS = frozenset(
    {
        "save_timetable",
        "uoft_login",
        "uoft_auth_status",
        "uoft_forget_session",
    }
)


@dataclass(frozen=True)
class Operation:
    name: str
    service: str
    description: str
    arguments: type[BaseModel]
    handler: Callable

    @property
    def private(self) -> bool:
        return self.service != "timetable"


class Registry:
    def __init__(self):
        self.operations: dict[str, Operation] = {}

    def add(self, handler: Callable) -> None:
        name = handler.__name__
        if name in self.operations or name in DIRECT_TOOLS:
            raise ValueError("Duplicate or non-read operation registration.")
        hints = get_type_hints(handler, include_extras=True)
        fields = {
            key: (
                hints[key],
                ... if parameter.default is inspect.Parameter.empty else parameter.default,
            )
            for key, parameter in inspect.signature(handler).parameters.items()
            if key != "ctx"
        }
        arguments = create_model(
            name + "Arguments", __config__=ConfigDict(extra="forbid"), **fields
        )
        service = (
            "degree_explorer"
            if name.startswith("degree_explorer_")
            else "acorn"
            if name.startswith("acorn_")
            else "timetable"
        )
        self.operations[name] = Operation(
            name, service, inspect.getdoc(handler) or name, arguments, handler
        )

    def decorator(self, server, profile, **options):
        def register(handler):
            if handler.__name__ not in DIRECT_TOOLS:
                if not options["annotations"].read_only_hint:
                    raise ValueError("Only read-only operations may enter the dispatcher.")
                self.add(handler)
            if profile == "legacy" or handler.__name__ in DIRECT_TOOLS:
                if handler.__name__ in DIRECT_TOOLS:
                    server.tool(**options)(handler)
                else:

                    @wraps(handler)
                    async def legacy(*args, **kwargs):
                        data = await handler(*args, **kwargs)
                        separators = (",", ":") if handler.__name__.startswith("acorn_") else None
                        return json.dumps(data, ensure_ascii=False, separators=separators)

                    server.tool(**options)(legacy)
            return handler

        return register

    def discover(self, query: str, service: str | None, detail: str, limit: int) -> dict:
        words = query.lower().split()
        matches = []
        for operation in self.operations.values():
            if service and operation.service != service:
                continue
            if query.strip() in self.operations and operation.name != query.strip():
                continue
            haystack = (operation.name + " " + operation.description).lower()
            exact = query.strip() == operation.name
            score = sum(word in haystack for word in words)
            if words and not score and not exact:
                continue
            matches.append((not exact, -score, operation.name, operation))
        matches.sort(key=lambda item: item[:3])
        items: list[Any] = []
        for _, _, _, operation in matches[:limit]:
            if detail == "names":
                items.append(operation.name)
                continue
            item = {
                "name": operation.name,
                "service": operation.service,
                "requires_auth": operation.private,
                "description": operation.description.split("\n\n")[0],
            }
            if detail == "schemas":
                item.update(
                    description=operation.description,
                    arguments=operation.arguments.model_json_schema(),
                )
            items.append(item)
        return {"operations": items, "total": len(matches), "has_more": len(matches) > limit}
