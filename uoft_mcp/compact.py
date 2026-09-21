"""Progressive discovery, bounded read batches, and snapshot selection over MCP."""

import asyncio
import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from uoft_mcp.operations import Registry
from uoft_mcp.results import ResultError, Selection, encode, view

READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    selection: Selection = Field(default_factory=Selection)


def register_compact(server, registry: Registry) -> None:
    @server.tool(structured_output=False, annotations=READ)
    async def uoft_discover(
        query: str = "",
        service: Literal["timetable", "degree_explorer", "acorn"] | None = None,
        detail: Literal["names", "descriptions", "schemas"] = "descriptions",
        limit: Annotated[int, Field(ge=1, le=25)] = 5,
    ) -> str:
        """Find read operations locally; request schemas before calling unfamiliar operations."""
        return encode(registry.discover(query, service, detail, limit))

    @server.tool(structured_output=False, annotations=READ)
    async def uoft_read(
        ctx: Context,
        requests: Annotated[list[ReadRequest], Field(min_length=1, max_length=8)],
    ) -> str:
        """Batch discovered reads with optional selection. Snapshot handles last five minutes."""
        app = ctx.request_context.lifespan_context
        validated = []
        for request in requests:
            operation = registry.operations.get(request.operation)
            if operation is None:
                raise ToolError("Unknown or non-read operation. Use uoft_discover.")
            try:
                model = operation.arguments.model_validate(request.arguments)
                arguments = {key: getattr(model, key) for key in type(model).model_fields}
                # Semantic validation must also precede *any* batch I/O.
                app.validate_operation(operation.name, arguments)
                encode(request.selection.model_dump())
            except (ValidationError, ValueError, ToolError):
                raise ToolError(
                    "Invalid operation arguments or selection. Check its schema."
                ) from None
            validated.append((operation, arguments, model.model_dump(mode="json")))

        generation = app.results.generation
        tasks = {}

        async def fetch(operation, arguments):
            try:
                if operation.private:
                    data = await operation.handler(ctx=ctx, **arguments)
                else:
                    async with app.public_reads:
                        data = await operation.handler(ctx=ctx, **arguments)
                handle, retrieved_at, warning = app.results.put(
                    data, private=operation.private, generation=generation
                )
                return data, handle, retrieved_at, warning
            except (ToolError, ResultError) as exc:
                return {"error": str(exc)}
            except Exception:
                return {"error": "Read failed. Check connection status and retry explicitly."}

        keys = []
        for operation, arguments, serialized in validated:
            key = (operation.name, json.dumps(serialized, sort_keys=True, ensure_ascii=False))
            keys.append(key)
            if key not in tasks:
                tasks[key] = asyncio.create_task(fetch(operation, arguments))
        try:
            await asyncio.gather(*tasks.values())
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        results = []
        for request, (operation, _, _), key in zip(requests, validated, keys, strict=True):
            fetched = tasks[key].result()
            item = {"operation": operation.name}
            if isinstance(fetched, dict):
                item.update(fetched)
            elif operation.private and generation != app.results.generation:
                item["error"] = "Authentication changed during this batch. Fetch again."
            else:
                data, handle, retrieved_at, warning = fetched
                item.update(handle=handle, retrieved_at=retrieved_at, retained=handle is not None)
                if warning:
                    item["warning"] = warning
                try:
                    item.update(view(data, request.selection))
                except ResultError as exc:
                    item["error"] = str(exc)
            results.append(item)
        return encode({"results": results})

    @server.tool(structured_output=False, annotations=READ)
    async def uoft_result(ctx: Context, handle: str, selection: Selection | None = None) -> str:
        """Select or page a snapshot without network access. Refetch when the handle expires."""
        try:
            snapshot = ctx.request_context.lifespan_context.results.get(handle)
            return encode(
                {
                    "handle": handle,
                    "retrieved_at": snapshot.retrieved_at,
                    **view(snapshot.data, selection or Selection()),
                }
            )
        except ResultError as exc:
            raise ToolError(str(exc)) from None
