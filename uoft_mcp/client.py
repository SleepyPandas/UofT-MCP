"""HTTP configuration and error handling for the public Timetable Builder API."""

from typing import Any

import httpx

BASE_URL = "https://api.easi.utoronto.ca/ttb/"
TIMEOUT_SECONDS = 30.0
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; UofT-MCP/0.1)",
    "Origin": "https://ttb.utoronto.ca",
    "Referer": "https://ttb.utoronto.ca/",
}


class TimetableAPIError(Exception):
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
    body: dict[str, Any] | None = None,
) -> Any:
    """Request one endpoint and preserve its JSON without imposing a course schema.

    Optional query parameters are omitted, rather than sent as empty strings.
    Paths are internal endpoint constants, never arbitrary client-provided URLs.
    """
    query = {key: value for key, value in (params or {}).items() if value is not None}
    try:
        response = await client.request(method, path, params=query, json=body)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise TimetableAPIError(
            f"UofT timetable API timed out after {TIMEOUT_SECONDS:g} seconds at {path}."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise TimetableAPIError(
            f"UofT timetable API returned HTTP {exc.response.status_code} at {path}."
        ) from exc
    except httpx.RequestError as exc:
        raise TimetableAPIError(f"Could not reach the UofT timetable API at {path}.") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise TimetableAPIError(f"UofT timetable API returned invalid JSON at {path}.") from exc
