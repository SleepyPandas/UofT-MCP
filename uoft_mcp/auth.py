"""Shared, nonblocking authentication lifecycle for local stdio MCP clients."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from uoft_mcp.auth_browser import (
    SERVICES,
    BrowserClosed,
    BrowserError,
    PlaywrightSession,
    ProbeResult,
    ServiceChoice,
)
from uoft_mcp.auth_store import EMPTY_STATE, SessionStore, SessionStoreError, merge_api_cookies
from uoft_mcp.degree_explorer import DegreeExplorerEndpoint, DegreeExplorerError


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
    def __init__(self, store=None, backend_factory=None, login_timeout=300, poll_interval=1):
        self.store = store if store is not None else SessionStore()
        self.backend_factory = backend_factory or PlaywrightSession
        self.login_timeout = login_timeout
        self.poll_interval = poll_interval
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

    def status(self) -> dict:
        return deepcopy(
            {
                "services": self._services,
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
        self._services[name].update(state=result.state, message=result.message)
        if result.state == "connected":
            self._services[name]["last_verified"] = datetime.now(UTC).isoformat()

    async def _checkpoint(self, browser: bool = False) -> None:
        snapshot = await self._backend.snapshot()
        self._state = snapshot if browser else merge_api_cookies(self._state, snapshot)
        await _store_call(self.store.save, self._state)

    async def login(self, service: ServiceChoice = "both", remember: bool = True) -> dict:
        if service not in {*SERVICES, "both"}:
            raise ValueError("Choose degree_explorer, acorn, or both.")
        if self._task is not None and not self._task.done():
            return self.status()
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
                    result = await self._backend.probe(SERVICES[name])
                    self._record(name, result)
                    await self._checkpoint()
                    if result.state == "login_required":
                        pending.append(name)
                if pending:
                    async with asyncio.timeout(self.login_timeout):
                        await self._backend.begin_browser(self._state)
                        for name in pending:
                            self._login["message"] = (
                                f"Complete the official UofT login for {name} in the browser."
                            )
                            await self._backend.navigate(SERVICES[name])
                            while True:
                                if await self._backend.on_service(SERVICES[name]):
                                    result = await self._backend.probe(SERVICES[name])
                                    self._record(name, result)
                                    if result.state != "login_required":
                                        await self._checkpoint(browser=True)
                                        break
                                await asyncio.sleep(self.poll_interval)
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
                self._login.update(
                    state="timed_out", message="Login timed out; run uoft_login to retry."
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

    async def refresh(self) -> dict:
        if self._task is not None and not self._task.done():
            return self.status()
        async with self._operation:
            try:
                await self._ensure_open(self._remember)
                for name, service in SERVICES.items():
                    self._record(name, await self._backend.probe(service))
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
            raise DegreeExplorerError(
                "UofT login is in progress. Check uoft_auth_status before retrying.",
                "login_in_progress",
            )
        async with self._operation:
            try:
                await self._ensure_open(self._remember)
                try:
                    data = await self._backend.read_degree_explorer(endpoint)
                except DegreeExplorerError as exc:
                    self._record("degree_explorer", ProbeResult(exc.state, str(exc)))
                    raise
                else:
                    self._record(
                        "degree_explorer", ProbeResult("connected", "Connection verified.")
                    )
                    return data
                finally:
                    await self._checkpoint()
            except DegreeExplorerError:
                raise
            except (BrowserError, SessionStoreError) as exc:
                await self._close_backend()
                raise DegreeExplorerError(str(exc), "unavailable") from None
            except Exception:
                await self._close_backend()
                raise DegreeExplorerError(
                    "Could not read Degree Explorer using the local session. "
                    "Check uoft_auth_status before retrying.",
                    "unavailable",
                ) from None

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
