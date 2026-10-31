"""Shared, nonblocking authentication lifecycle for local stdio MCP clients."""

import asyncio
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from uoft_mcp.acorn.client import AcornEndpoint, AcornError
from uoft_mcp.degree_explorer.client import DegreeExplorerEndpoint, DegreeExplorerError
from uoft_mcp.status import (
    StatusKind,
    classify_exception,
    describe,
    invalidates_private_data,
    kind_for_state,
    legacy_state,
    service_label,
)
from uoft_mcp.utilities.auth_browser import (
    SERVICES,
    BrowserClosed,
    BrowserError,
    PlaywrightSession,
    ProbeResult,
    ServiceChoice,
)
from uoft_mcp.utilities.auth_store import (
    EMPTY_STATE,
    SessionStore,
    SessionStoreError,
    merge_api_cookies,
)


def _sign_in_prompt(previous: str | None, name: str) -> str:
    """What the person should do in the login window for this service.

    UofT's IdP is asked for a fresh sign-in (ForceAuthn) by each service, so connecting
    a second service always shows the password and Duo pages again.
    """
    label = service_label(name)
    if previous is None:
        return f"Complete the official UofT login for {label} in the browser window."
    return (
        f"{service_label(previous)} is connected. UofT asks for a separate sign-in to "
        f"{label}: enter your UTORid, password and Duo again in the same browser window."
    )


async def _store_call(function, *args):
    """Finish an OS keyring/file operation before cancellation releases the session lock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


class AuthManager:
    def __init__(
        self,
        store=None,
        backend_factory=None,
        login_timeout=300,
        poll_interval=1,
        login_settle_seconds=5.0,
        silent_refresh_cooldown=60.0,
    ):
        self.on_session_change = lambda: None
        self.store = store if store is not None else SessionStore()
        self.backend_factory = backend_factory or PlaywrightSession
        self.login_timeout = login_timeout
        self.poll_interval = poll_interval
        self.login_settle_seconds = login_settle_seconds
        self.silent_refresh_cooldown = silent_refresh_cooldown
        self._silent_blocked_until: dict[str, float] = {}
        self._backend: Any = None
        self._state = deepcopy(EMPTY_STATE)
        self._opened = False
        self._remember = True
        self._operation = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._services = {
            name: {
                "state": "not_checked",
                "message": "Connection not checked.",
                "last_verified": None,
            }
            for name in SERVICES
        }
        self._login = {"state": "idle", "message": "Use uoft_login to connect.", "services": []}

    def _plain_services(self) -> dict:
        """Add each service's status kind and next step to its existing fields."""
        services = {}
        for name, value in self._services.items():
            kind = value.get("kind")
            if kind is None or legacy_state(StatusKind(kind)) != value["state"]:
                kind = kind_for_state(value["state"])
            kind = StatusKind(kind)
            services[name] = {
                **value,
                "kind": kind.value,
                "next_action": describe(kind, name).next_action,
            }
        return services

    def status(self) -> dict:
        return deepcopy(
            {
                "services": self._plain_services(),
                "login": self._login,
                "persistence": {
                    "requested": self._remember,
                    "enabled": self.store.persistent,
                    "message": self.store.message,
                },
            }
        )

    async def _ensure_open(self, remember: bool) -> None:
        if not self._opened:
            await _store_call(self.store.acquire)
            self._state = await _store_call(self.store.load, remember)
            self._remember = remember
            self._opened = True
        elif remember != self._remember:
            # A memory-only login uses a fresh identity; never mix it with remembered cookies.
            await self._close_backend()
            self._state = await _store_call(self.store.load, remember)
            self._remember = remember
            for status in self._services.values():
                status.update(
                    state="not_checked", last_verified=None, message="Connection not checked."
                )
        if self._backend is None:
            self._backend = self.backend_factory()
            await self._backend.start(self._state)

    def _record(self, name: str, result: ProbeResult) -> None:
        kind = result.kind or kind_for_state(result.state)
        if invalidates_private_data(kind):
            self.on_session_change()
        self._services[name].update(state=result.state, message=result.message, kind=kind.value)
        if result.state == "connected":
            self._services[name]["last_verified"] = datetime.now(UTC).isoformat()

    async def _checkpoint(self, browser: bool = False) -> None:
        snapshot = await self._backend.snapshot()
        self._state = snapshot if browser else merge_api_cookies(self._state, snapshot)
        await _store_call(self.store.save, self._state)

    async def _silent_refresh(self, name: str, force: bool = False) -> bool:
        """Renew a lapsed app session from a still-valid SSO session, never asking for Duo.

        Failed attempts are rate limited so reads and status checks do not repeatedly
        start a hidden browser once UofT has really ended the session.
        """
        if not self._state["cookies"] or self._backend is None:
            return False
        if not force and time.monotonic() < self._silent_blocked_until.get(name, 0.0):
            return False
        try:
            result = await self._backend.silent_refresh(SERVICES[name])
        except asyncio.CancelledError:
            raise
        except Exception:
            result = ProbeResult("unavailable", "Could not renew the saved session.")
        if result.state != "connected":
            self._silent_blocked_until[name] = time.monotonic() + self.silent_refresh_cooldown
            return False
        self._silent_blocked_until.pop(name, None)
        await self._checkpoint()  # Persist the renewed cookies.
        return True

    async def _probe_renewing(self, name: str, force: bool = False) -> ProbeResult:
        result = await self._backend.probe(SERVICES[name])
        if result.state == "login_required" and await self._silent_refresh(name, force):
            result = await self._backend.probe(SERVICES[name])
        return result

    async def _read_renewing(self, name: str, error_type: type, read):
        try:
            return await read()
        except error_type as exc:
            if exc.state != "login_required" or not await self._silent_refresh(name):
                raise
        return await read()

    async def login(self, service: ServiceChoice = "both", remember: bool = True) -> dict:
        if service not in {*SERVICES, "both"}:
            raise ValueError("Choose degree_explorer, acorn, or both.")
        if self._task is not None and not self._task.done():
            return self.status()
        self.on_session_change()
        names = list(SERVICES) if service == "both" else [service]
        self._login = {
            "state": "in_progress",
            "message": "Preparing UofT login.",
            "services": names,
        }
        self._task = asyncio.create_task(self._run_login(names, remember))
        return self.status()

    async def _run_login(self, names: list[str], remember: bool) -> None:
        async with self._operation:
            try:
                await self._ensure_open(remember)
                pending = []
                for name in names:
                    result = await self._probe_renewing(name, force=True)
                    self._record(name, result)
                    await self._checkpoint()
                    if result.state == "login_required":
                        pending.append(name)
                if pending:
                    async with asyncio.timeout(self.login_timeout):
                        await self._backend.begin_browser(self._state)
                        for index, name in enumerate(pending):
                            prompt = _sign_in_prompt(pending[index - 1] if index else None, name)
                            self._login["message"] = prompt
                            await self._backend.navigate(SERVICES[name])
                            await self._wait_for_service(name, prompt)
                        await self._backend.finish_browser(self._state)
                count = sum(self._services[name]["state"] == "connected" for name in names)
                self._login.update(
                    state="complete" if count == len(names) else "partial" if count else "failed",
                    message="Login check finished. See each service's connection status.",
                )
            except asyncio.CancelledError:
                self._login.update(
                    state="cancelled", message="Login cancelled; run uoft_login to retry."
                )
                await self._close_backend()
                raise
            except TimeoutError:
                duo = describe(StatusKind.DUO_PENDING, "")
                self._login.update(state="timed_out", message=duo.message)
                for name in names:
                    if self._services[name]["state"] == "login_required":
                        self._record(
                            name,
                            ProbeResult(
                                duo.state, describe(StatusKind.DUO_PENDING, name).message, duo.kind
                            ),
                        )
                await self._close_backend()
            except BrowserClosed as exc:
                self._login.update(state="cancelled", message=str(exc))
                await self._close_backend()
            except (BrowserError, SessionStoreError) as exc:
                self._login.update(state="failed", message=str(exc))
                await self._close_backend()
            except Exception:
                self._login.update(
                    state="failed", message="Authentication could not complete; retry login."
                )
                await self._close_backend()

    async def _wait_for_service(self, name: str, prompt: str | None = None) -> None:
        """Verify, allow redirects/storage to settle, then capture the latest session.

        The caller's shared login timeout bounds retries and the settling delay.
        Only connected probes permit completion; an error is not proof of login.
        prompt stays visible in the login status while waiting for the person to sign in.
        """
        service = SERVICES[name]
        while True:
            if await self._backend.on_service(service):
                result = await self._backend.probe(service)
                self._record(name, result)
                if result.state == "connected":
                    # Retain verified access even if the user cancels during settling.
                    await self._checkpoint(browser=True)
                    self._login["message"] = (
                        f"Connected to {name}; waiting {self.login_settle_seconds:g} seconds "
                        "for the browser to finish loading before rechecking access."
                    )
                    await asyncio.sleep(self.login_settle_seconds)
                    if await self._backend.on_service(service):
                        result = await self._backend.probe(service)
                    else:
                        result = ProbeResult(
                            "login_required", "Waiting for the browser to return to the service."
                        )
                    self._record(name, result)
                    if result.state == "connected":
                        await self._checkpoint(browser=True)
                        return
            self._login["message"] = (
                prompt or f"Waiting for verified access to {name} in the browser."
            )
            await asyncio.sleep(self.poll_interval)

    async def refresh(self) -> dict:
        if self._task is not None and not self._task.done():
            return self.status()
        async with self._operation:
            try:
                await self._ensure_open(self._remember)
                for name in SERVICES:
                    self._record(name, await self._probe_renewing(name))
                    await self._checkpoint()
            except (BrowserError, SessionStoreError) as exc:
                self._login.update(state="failed", message=str(exc))
                await self._close_backend()
            except Exception:
                self._login.update(
                    state="failed", message="Could not check UofT connections; try again."
                )
                await self._close_backend()
        return self.status()

    async def read_degree_explorer(self, endpoint: DegreeExplorerEndpoint) -> Any:
        """Reuse saved access for one read, serialized with refresh, login, and forget.

        Reads never initiate login or wait for Duo. Only rotated cookies are saved;
        academic response data stays transient and is returned to the calling tool.
        """
        if not isinstance(endpoint, DegreeExplorerEndpoint):
            raise DegreeExplorerError("Unsupported Degree Explorer endpoint.")
        if self._task is not None and not self._task.done():
            raise DegreeExplorerError.from_status(
                describe(StatusKind.LOGIN_IN_PROGRESS, "degree_explorer")
            )
        async with self._operation:
            try:
                await self._ensure_open(self._remember)
                try:
                    data = await self._read_renewing(
                        "degree_explorer",
                        DegreeExplorerError,
                        lambda: self._backend.read_degree_explorer(endpoint),
                    )
                except DegreeExplorerError as exc:
                    # One failing route says nothing about the connection or the other routes.
                    if exc.kind is not StatusKind.ROUTE_ERROR:
                        self._record("degree_explorer", ProbeResult(exc.state, str(exc), exc.kind))
                    raise
                else:
                    connected = describe(StatusKind.CONNECTED, "degree_explorer")
                    self._record(
                        "degree_explorer",
                        ProbeResult(connected.state, connected.message, connected.kind),
                    )
                    return data
                finally:
                    await self._checkpoint()
            except DegreeExplorerError:
                raise
            except (BrowserError, SessionStoreError) as exc:
                await self._close_backend()
                raise DegreeExplorerError(str(exc), "unavailable") from None
            except Exception as exc:
                await self._close_backend()
                raise DegreeExplorerError.from_status(
                    classify_exception(exc, "degree_explorer")
                ) from None

    async def read_acorn(self, endpoint: AcornEndpoint) -> Any:
        """Reuse saved access for one read, serialized with refresh, login, and forget.

        Reads never initiate login or wait for Duo. Only rotated cookies are saved;
        academic response data stays transient and is returned to the calling tool.
        """
        if not isinstance(endpoint, AcornEndpoint):
            raise AcornError("Unsupported ACORN endpoint.")
        if self._task is not None and not self._task.done():
            raise AcornError.from_status(describe(StatusKind.LOGIN_IN_PROGRESS, "acorn"))
        async with self._operation:
            try:
                await self._ensure_open(self._remember)
                try:
                    data = await self._read_renewing(
                        "acorn", AcornError, lambda: self._backend.read_acorn(endpoint)
                    )
                except AcornError as exc:
                    if exc.kind is not StatusKind.ROUTE_ERROR:
                        self._record("acorn", ProbeResult(exc.state, str(exc), exc.kind))
                    raise
                else:
                    connected = describe(StatusKind.CONNECTED, "acorn")
                    self._record(
                        "acorn", ProbeResult(connected.state, connected.message, connected.kind)
                    )
                    return data
                finally:
                    await self._checkpoint()
            except AcornError:
                raise
            except (BrowserError, SessionStoreError) as exc:
                await self._close_backend()
                raise AcornError(str(exc), "unavailable") from None
            except Exception as exc:
                await self._close_backend()
                raise AcornError.from_status(classify_exception(exc, "acorn")) from None

    async def wait_for_login(self) -> dict:
        """Used by the terminal and tests; MCP login itself never waits for Duo."""
        if self._task is not None:
            await self._task
        return self.status()

    async def _cancel_login(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def forget(self) -> dict:
        self.on_session_change()
        await self._cancel_login()
        async with self._operation:
            await self._close_backend()
            self._state = deepcopy(EMPTY_STATE)
            try:
                await _store_call(self.store.acquire)
                await _store_call(self.store.forget)
                self._login.update(
                    state="idle",
                    message="Local session forgotten. University-wide logout is separate.",
                )
            except SessionStoreError as exc:
                self._login.update(state="failed", message=str(exc))
            finally:
                for value in self._services.values():
                    value.update(
                        state="not_checked", last_verified=None, message="Local session cleared."
                    )
                self._opened = False
                await _store_call(self.store.close)
        return self.status()

    async def _close_backend(self) -> None:
        self.on_session_change()
        if self._backend is not None:
            try:
                await self._backend.close()
            finally:
                self._backend = None

    async def close(self) -> None:
        await self._cancel_login()
        async with self._operation:
            await self._close_backend()
            self._state = deepcopy(EMPTY_STATE)
            self._opened = False
            await _store_call(self.store.close)
