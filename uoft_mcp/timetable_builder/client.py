"""HTTP configuration and error handling for the public Timetable Builder API."""

import json
from typing import Any
from urllib.parse import quote

import httpx

from uoft_mcp.status import ServiceReadError, StatusKind, classify_exception, describe

BASE_URL = "https://api.easi.utoronto.ca/ttb/"
TTB_ORIGIN = "https://ttb.utoronto.ca"
TIMEOUT_SECONDS = 30.0
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; UofT-MCP/0.2)",
    "Origin": "https://ttb.utoronto.ca",
    "Referer": "https://ttb.utoronto.ca/",
}


class TimetableAPIError(ServiceReadError):
    """An upstream failure that can be explained to an MCP client."""


def create_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Create a client for the server lifespan; transport injection supports offline tests."""
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers=HEADERS,
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, str | int | None] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
    content: str | bytes | None = None,
    content_type: str | None = None,
) -> Any:
    """Request one endpoint and preserve its JSON without imposing a course schema.

    Optional query parameters are omitted, rather than sent as empty strings.
    Paths are internal endpoint constants, never arbitrary client-provided URLs.
    JSON bodies and raw content are mutually exclusive; raw content is used for
    the Timetable Builder's encoded share payload.
    """
    query = {key: value for key, value in (params or {}).items() if value is not None}
    headers = {"Content-Type": content_type} if content_type is not None else None
    try:
        response = await client.request(
            method,
            path,
            params=query,
            json=body,
            content=content,
            headers=headers,
        )
        response.raise_for_status()
    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise TimetableAPIError.from_status(
            classify_exception(exc, "timetable", where=path, timeout_seconds=TIMEOUT_SECONDS)
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        raise TimetableAPIError.from_status(
            describe(StatusKind.PAGE_CHANGED, "timetable", f"invalid JSON at {path}")
        ) from exc


async def shorten_state(client: httpx.AsyncClient, state: dict[str, Any]) -> dict[str, Any]:
    """Store a serialized Timetable Builder state the way the official site's share button does.

    The frontend posts encodeURIComponent of its own URL with the state JSON as the query.
    Returns the upstream share object, which always carries a non-empty string ``id``.
    """
    encoded = quote(
        f"{TTB_ORIGIN}/#!/?{json.dumps(state, ensure_ascii=False, separators=(',', ':'))}",
        safe="!~*'()",
    )
    data = await request_json(
        client, "POST", "tiny/shorten", content=encoded, content_type="text/plain"
    )
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
        raise TimetableAPIError.from_status(
            describe(StatusKind.PAGE_CHANGED, "timetable", "no share id returned by tiny/shorten")
        )
    return data
