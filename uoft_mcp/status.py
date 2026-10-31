"""Plain-language status kinds for every UofT service the server talks to.

One place decides what a failure means (signed out, Duo pending, service down, no
internet, page changed, ...) and how to say it. Messages never contain upstream
bodies, headers, redirect URLs, or credentials. Each kind also maps to the
machine-readable ``state`` strings the auth/status tools already expose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Never

import httpx

SERVICE_LABELS: Mapping[str, str] = {
    "timetable": "Timetable Builder",
    "degree_explorer": "Degree Explorer",
    "acorn": "ACORN",
}
# Services that need uoft_login; the public Timetable Builder never does.
PRIVATE_SERVICES = frozenset({"degree_explorer", "acorn"})

_NETWORK_MARKERS = (
    "enotfound",
    "eai_again",
    "econnrefused",
    "econnreset",
    "enetunreach",
    "ehostunreach",
    "getaddrinfo",
    "name or service not known",
    "err_name_not_resolved",
    "err_internet_disconnected",
    "err_network_changed",
    "err_connection",
    "socket hang up",
)
_MAINTENANCE_MARKERS = (
    "maintenance",
    "scheduled downtime",
    "service unavailable",
    "temporarily unavailable",
    "be right back",
)


class StatusKind(StrEnum):
    CONNECTED = "connected"
    NOT_CHECKED = "not_checked"
    SIGNED_OUT = "signed_out"
    DUO_PENDING = "duo_pending"
    LOGIN_IN_PROGRESS = "login_in_progress"
    ACCESS_DENIED = "access_denied"
    SERVICE_DOWN = "service_down"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    PAGE_CHANGED = "page_changed"
    REQUEST_REJECTED = "request_rejected"
    UNKNOWN = "unknown"


def legacy_state(kind: StatusKind) -> str:
    """The long-standing machine-readable state string for this kind."""
    match kind:
        case StatusKind.CONNECTED:
            return "connected"
        case StatusKind.NOT_CHECKED:
            return "not_checked"
        case StatusKind.SIGNED_OUT | StatusKind.DUO_PENDING:
            return "login_required"
        case StatusKind.LOGIN_IN_PROGRESS:
            return "login_in_progress"
        case StatusKind.ACCESS_DENIED:
            return "access_denied"
        case (
            StatusKind.SERVICE_DOWN
            | StatusKind.RATE_LIMITED
            | StatusKind.NETWORK
            | StatusKind.UNKNOWN
        ):
            return "unavailable"
        case StatusKind.PAGE_CHANGED | StatusKind.REQUEST_REJECTED:
            return "unexpected_response"
        case _:
            unused: Never = kind
            raise ValueError(f"Unhandled status kind: {unused}")


def kind_for_state(state: str) -> StatusKind:
    """Best-effort kind for a legacy state string whose producer did not supply one."""
    for kind in (
        StatusKind.CONNECTED,
        StatusKind.NOT_CHECKED,
        StatusKind.SIGNED_OUT,
        StatusKind.LOGIN_IN_PROGRESS,
        StatusKind.ACCESS_DENIED,
        StatusKind.SERVICE_DOWN,
        StatusKind.PAGE_CHANGED,
    ):
        if legacy_state(kind) == state:
            return kind
    return StatusKind.UNKNOWN


@dataclass(frozen=True)
class Status:
    kind: StatusKind
    service: str
    summary: str
    next_action: str

    @property
    def state(self) -> str:
        return legacy_state(self.kind)

    @property
    def message(self) -> str:
        return f"{self.summary} {self.next_action}".strip()

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "state": self.state,
            "message": self.message,
            "next_action": self.next_action,
        }


class ServiceReadError(Exception):
    """Sanitized failure carrying its status state and kind, never an upstream body."""

    def __init__(
        self, message: str, state: str = "unexpected_response", kind: StatusKind | None = None
    ):
        super().__init__(message)
        self.state = state
        self.kind = kind if kind is not None else kind_for_state(state)

    @classmethod
    def from_status(cls, status: Status):
        return cls(status.message, status.state, status.kind)


def service_label(service: str) -> str:
    return SERVICE_LABELS.get(service, "UofT")


def describe(kind: StatusKind, service: str, detail: str | None = None) -> Status:
    """Write the short, friendly message and next step for one status kind."""
    name = service_label(service)
    extra = f" ({detail})" if detail else ""
    login = "Run uoft_login to sign in again."
    match kind:
        case StatusKind.CONNECTED:
            summary, action = f"Connected to {name}.", ""
        case StatusKind.NOT_CHECKED:
            summary = f"{name} has not been checked yet."
            action = "Run uoft_auth_status with refresh=true to check."
        case StatusKind.SIGNED_OUT:
            summary, action = f"Signed out of {name}.", login
        case StatusKind.DUO_PENDING:
            target = f" for {SERVICE_LABELS[service]}" if service in SERVICE_LABELS else ""
            summary = f"Duo sign-in pending or failed{target}."
            action = "Run uoft_login and approve the Duo prompt."
        case StatusKind.LOGIN_IN_PROGRESS:
            summary = "UofT login is in progress."
            action = "Finish it in the browser window, then check uoft_auth_status."
        case StatusKind.ACCESS_DENIED:
            summary = f"{name} denied access{extra}."
            action = (
                "Run uoft_login to sign in again; if it keeps happening, "
                "your account may not have access."
                if service in PRIVATE_SERVICES
                else "Try again later."
            )
        case StatusKind.SERVICE_DOWN:
            summary, action = f"{name} is down or not responding{extra}.", "Try again later."
        case StatusKind.RATE_LIMITED:
            summary = f"{name} is busy and asked us to slow down{extra}."
            action = "Wait a minute, then try again."
        case StatusKind.NETWORK:
            summary = (
                f"Can't reach {name}: no internet connection, "
                f"or UofT servers can't be reached{extra}."
            )
            action = "Check your connection (or VPN) and try again."
        case StatusKind.PAGE_CHANGED:
            summary = f"UofT changed the {name} page, so the server couldn't read it{extra}."
            action = "Try again later; if it keeps happening, update uoft-mcp or report an issue."
        case StatusKind.REQUEST_REJECTED:
            summary = f"{name} didn't accept the request{extra}."
            action = "Check the course code or filters and try again."
        case StatusKind.UNKNOWN:
            summary = f"Something went wrong while talking to {name}{extra}."
            action = "Try again; if it keeps happening, run uoft_auth_status."
        case _:
            unused: Never = kind
            raise ValueError(f"Unhandled status kind: {unused}")
    return Status(kind, service, summary, action)


def classify_http_status(service: str, status: int, where: str = "") -> Status | None:
    """Map a non-success HTTP status; None means the caller must inspect the body."""
    suffix = f" at {where}" if where else ""
    detail = f"HTTP {status}{suffix}"
    if status == 401:
        return describe(StatusKind.SIGNED_OUT, service)
    if status == 403:
        return describe(StatusKind.ACCESS_DENIED, service, detail)
    if status == 429:
        return describe(StatusKind.RATE_LIMITED, service, detail)
    if status == 408 or status >= 500:
        return describe(StatusKind.SERVICE_DOWN, service, detail)
    if 400 <= status < 500:
        # A fixed private route that now fails means UofT moved it; a public search may
        # simply have no match.
        kind = (
            StatusKind.PAGE_CHANGED if service in PRIVATE_SERVICES else StatusKind.REQUEST_REJECTED
        )
        return describe(kind, service, detail)
    return None


def looks_like_maintenance(body: str) -> bool:
    lower = body.lower()
    return any(marker in lower for marker in _MAINTENANCE_MARKERS)


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return True
    # Playwright's TimeoutError is its own class; avoid importing the browser stack here.
    if any(cls.__name__ == "TimeoutError" for cls in type(exc).__mro__):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _is_network(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | OSError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _NETWORK_MARKERS)


def classify_exception(
    exc: BaseException, service: str, *, where: str = "", timeout_seconds: float | None = None
) -> Status:
    """Turn a transport/parse exception into a status without exposing its text."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = classify_http_status(service, exc.response.status_code, where)
        if status is not None:
            return status
    if _is_timeout(exc):
        suffix = f" at {where}" if where else ""
        limit = f" after {timeout_seconds:g} seconds" if timeout_seconds else ""
        return describe(StatusKind.SERVICE_DOWN, service, f"timed out{limit}{suffix}")
    if _is_network(exc):
        return describe(StatusKind.NETWORK, service, f"at {where}" if where else None)
    if isinstance(exc, ValueError):
        suffix = f" at {where}" if where else ""
        return describe(StatusKind.PAGE_CHANGED, service, f"invalid JSON{suffix}")
    return describe(StatusKind.UNKNOWN, service)
