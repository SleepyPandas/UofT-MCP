"""Private, fixed-endpoint Playwright adapter. Never automate credential entry."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

from uoft_mcp.status import (
    StatusKind,
    classify_exception,
    classify_http_status,
    describe,
    looks_like_maintenance,
)
from uoft_mcp.utilities.browser_select import BrowserUnavailable, launch_browser

if TYPE_CHECKING:
    from uoft_mcp.acorn.client import AcornEndpoint
    from uoft_mcp.degree_explorer.client import DegreeExplorerEndpoint

ServiceName = Literal["degree_explorer", "acorn"]
ServiceChoice = Literal["degree_explorer", "acorn", "both"]


@dataclass(frozen=True)
class Service:
    name: ServiceName
    app_url: str
    probe_url: str
    json_type: type


SERVICES = {
    "degree_explorer": Service(
        "degree_explorer",
        "https://degreeexplorer.utoronto.ca/",
        "https://degreeexplorer.utoronto.ca/degreeExplorer/rest/dxMenu/getStudentMenu",
        list,  # Live verification: the menu is an array, unlike the registry client's hint.
    ),
    "acorn": Service(
        "acorn",
        "https://acorn.utoronto.ca/sws",
        "https://acorn.utoronto.ca/sws/rest/enrolment/eligible-registrations",
        list,
    ),
}


CREDENTIAL_PROMPT = 'input[type="password"], iframe[src*="duosecurity.com"], #duo_iframe'
IDP_HOSTS = frozenset({"idpz.utorauth.utoronto.ca", "weblogin.utoronto.ca"})
SSO_REDIRECT_HOPS = 4
MAX_SAML_REQUEST_BYTES = 64 * 1024
_FORCE_AUTHN = re.compile(rb'ForceAuthn="([^"]*)"')


def forces_fresh_sign_in(location: str) -> bool:
    """True when a redirect to UofT's IdP carries a SAML request with ForceAuthn set.

    Such a request makes the IdP ask for a password and Duo even when its own session is
    still valid, so a hidden browser can never renew the application session.
    """
    parts = urlsplit(location)
    if parts.hostname not in IDP_HOSTS:
        return False
    values = parse_qs(parts.query).get("SAMLRequest")
    if not values:
        return False
    try:
        # HTTP-Redirect binding: raw DEFLATE, then base64; bounded against oversized input.
        xml = zlib.decompressobj(-15).decompress(
            base64.b64decode(values[0], validate=False), MAX_SAML_REQUEST_BYTES
        )
    except (binascii.Error, ValueError, zlib.error):
        return False
    match = _FORCE_AUTHN.search(xml)
    return match is not None and match.group(1).strip().lower() in {b"1", b"true"}


class BrowserError(Exception):
    """Only fixed, sanitized messages may reach the manager."""


class BrowserClosed(BrowserError):
    """The user closed the login window."""


@dataclass(frozen=True)
class ProbeResult:
    state: str
    message: str
    kind: StatusKind | None = None


def _probe(kind: StatusKind, service: Service, detail: str | None = None) -> ProbeResult:
    status = describe(kind, service.name, detail)
    return ProbeResult(status.state, status.message, status.kind)


def classify_response(service: Service, status: int, headers: dict, body: str) -> ProbeResult:
    """Consume a response transiently; return no payload, headers, or redirect URLs."""
    result, _ = classify_and_parse_response(service, status, headers, body)
    return result


def classify_and_parse_response(
    service: Service, status: int, headers: dict, body: str
) -> tuple[ProbeResult, Any]:
    """Classify once and return parsed JSON only on success, for transient reads.

    The result state distinguishes successful JSON null from a failed response.
    Authentication probes discard the payload through classify_response.
    """
    login = _probe(StatusKind.SIGNED_OUT, service)
    if status == 401:
        return login, None
    if 300 <= status < 400:
        destination = urlsplit(urljoin(service.probe_url, headers.get("location", "")))
        host = destination.hostname or ""
        if host in IDP_HOSTS or host == "duosecurity.com" or host.endswith(".duosecurity.com"):
            return login, None
        return _probe(StatusKind.PAGE_CHANGED, service, "unexpected redirect"), None
    lower = body.lower()
    if "json" not in headers.get("content-type", "").lower() and any(
        marker in lower
        for marker in ("j_password", "samlrequest", "samlresponse", "utorid", "duosecurity.com")
    ):
        return login, None
    is_json = "json" in headers.get("content-type", "").lower()
    failure = classify_http_status(service.name, status)
    # Access errors and outages win over body sniffing; other 4xx only mean the route moved.
    if failure is not None and failure.kind is not StatusKind.PAGE_CHANGED:
        return ProbeResult(failure.state, failure.message, failure.kind), None
    if not is_json and looks_like_maintenance(body):
        return _probe(StatusKind.SERVICE_DOWN, service, "maintenance page"), None
    if failure is not None:
        return ProbeResult(failure.state, failure.message, failure.kind), None
    if status != 200 or not is_json:
        return _probe(StatusKind.PAGE_CHANGED, service, "unexpected response"), None
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return _probe(StatusKind.PAGE_CHANGED, service, "malformed JSON"), None
    if not isinstance(payload, service.json_type):
        return _probe(StatusKind.PAGE_CHANGED, service, "unexpected JSON shape"), None
    return _probe(StatusKind.CONNECTED, service), payload


class PlaywrightSession:
    """An API context between calls, with a temporary visible browser only for login."""

    def __init__(self):
        self.playwright: Any = None
        self.api: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.user_agent: str | None = None

    def _context_options(self, state: dict) -> dict:
        options = {"storage_state": {"cookies": state["cookies"], "origins": state["origins"]}}
        if self.user_agent:
            options["user_agent"] = self.user_agent
        return options

    async def start(self, state: dict) -> None:
        try:
            from playwright.async_api import async_playwright

            self.user_agent = state.get("user_agent")
            self.playwright = await async_playwright().start()
            self.api = await self.playwright.request.new_context(**self._context_options(state))
        except Exception:
            await self.close()
            raise BrowserError("Could not start authentication. Run: uoft-mcp auth setup") from None

    async def probe(self, service: Service) -> ProbeResult:
        return await self._probe(self.api, service)

    async def _probe(self, api: Any, service: Service) -> ProbeResult:
        response = None
        try:
            response = await api.get(
                service.probe_url,
                headers={"Accept": "application/json"},
                timeout=15_000,
                max_redirects=0,
            )
            return classify_response(
                service, response.status, response.headers, await response.text()
            )
        except Exception as exc:
            status = classify_exception(exc, service.name, timeout_seconds=15)
            return ProbeResult(status.state, status.message, status.kind)
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass

    async def silent_refresh(self, service: Service, wait_seconds: float = 20.0) -> ProbeResult:
        """Re-validate saved cookies through UofT SSO in a hidden browser; never prompts.

        An app session that lapsed while the SSO session lives is renewed by the
        redirect chain, which needs a browser to run its auto-submitted forms. If SSO
        itself ended, the browser stops on the credential page and this reports
        `login_required`; no credentials or Duo are ever entered here.
        """
        if self.context is not None or self.api is None:
            return ProbeResult("login_required", "Interactive login is in progress.")
        if await self._requires_fresh_sign_in(service):
            return _probe(StatusKind.SIGNED_OUT, service, "UofT requires a fresh sign-in")
        browser = context = None
        try:
            state = await self.snapshot()
            browser = await launch_browser(self.playwright, headless=True)
            context = await browser.new_context(**self._context_options(state))
            page = await context.new_page()
            await page.goto(service.app_url, wait_until="domcontentloaded", timeout=30_000)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_seconds
            result = ProbeResult("login_required", "Saved session could not be renewed.")
            while loop.time() < deadline:
                if self._at_service(page.url, service):
                    # Redirects and cookies may still be settling; only a failure to
                    # reach a login page is final, so keep polling until the deadline.
                    result = await self._probe(context.request, service)
                    if result.state not in {"login_required", "unexpected_response"}:
                        break
                elif await page.query_selector(CREDENTIAL_PROMPT):
                    return result  # SSO ended: a person must sign in; stop waiting.
                await asyncio.sleep(1.0)
            if result.state != "connected":
                return result
            renewed = await context.storage_state()
            if self.user_agent:
                renewed["user_agent"] = self.user_agent
            await self.api.dispose()
            self.api = await self.playwright.request.new_context(**self._context_options(renewed))
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            return ProbeResult("unavailable", "Could not renew the saved session.")
        finally:
            for resource in (context, browser):
                if resource is not None:
                    try:
                        await resource.close()
                    except Exception:
                        pass

    async def _requires_fresh_sign_in(self, service: Service) -> bool:
        """Follow the service's own redirects to its SSO request, without a browser.

        Any failure or unexpected response returns False so the browser attempt still runs.
        """
        url = service.app_url
        for _ in range(SSO_REDIRECT_HOPS):
            response = None
            try:
                response = await self.api.get(url, timeout=15_000, max_redirects=0)
                location = response.headers.get("location")
                if not 300 <= response.status < 400 or not location:
                    return False
            except Exception:
                return False
            finally:
                if response is not None:
                    try:
                        await response.dispose()
                    except Exception:
                        pass
            url = urljoin(url, location)
            if urlsplit(url).hostname != urlsplit(service.app_url).hostname:
                return forces_fresh_sign_in(url)
        return False

    @staticmethod
    def _at_service(url: str, service: Service) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname == urlsplit(service.app_url).hostname
            and parts.path.startswith(
                "/degreeExplorer" if service.name == "degree_explorer" else "/sws"
            )
        )

    async def read_degree_explorer(self, endpoint: DegreeExplorerEndpoint) -> Any:
        """Read one allowlisted route without opening a browser or following SSO."""
        from uoft_mcp.degree_explorer.client import request_degree_explorer

        return await request_degree_explorer(self.api, endpoint)

    async def read_acorn(self, endpoint: AcornEndpoint) -> Any:
        """Read one allowlisted route without opening a browser or following SSO."""
        from uoft_mcp.acorn.client import request_acorn

        return await request_acorn(self.api, endpoint)

    async def snapshot(self) -> dict:
        if self.context is not None:
            state = await self.context.storage_state()
        else:
            state = await self.api.storage_state()
        if self.user_agent:
            state["user_agent"] = self.user_agent
        return state

    async def begin_browser(self, state: dict) -> None:
        try:
            self.browser = await launch_browser(self.playwright, headless=False)
            self.context = await self.browser.new_context(
                **self._context_options(state), accept_downloads=False
            )
            self.page = await self.context.new_page()
            self.user_agent = await self.page.evaluate("navigator.userAgent")
            await self.api.dispose()
            self.api = self.context.request
        except BrowserUnavailable as exc:
            raise BrowserError(str(exc)) from None
        except Exception:
            raise BrowserError(
                "Could not open the login window. "
                "Run uoft-mcp auth setup and use a desktop session."
            ) from None

    async def navigate(self, service: Service) -> None:
        try:
            await self.page.goto(service.app_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            if self.page.is_closed() or not self.browser.is_connected():
                raise BrowserClosed("Login window closed. Run uoft_login to try again.") from None
            raise BrowserError("Could not load the official login page; try again.") from None

    async def on_service(self, service: Service) -> bool:
        if self.page.is_closed() or not self.browser.is_connected():
            raise BrowserClosed("Login window closed. Run uoft_login to try again.")
        return self._at_service(self.page.url, service)

    async def finish_browser(self, state: dict) -> None:
        """Close after the manager's final checkpoint and reuse that captured state."""
        await self.context.close()
        await self.browser.close()
        self.context = self.browser = self.page = self.api = None
        self.api = await self.playwright.request.new_context(**self._context_options(state))

    async def close(self) -> None:
        # Cleanup must not expose Playwright errors containing URLs or response details.
        for resource, method in (
            (self.api, "dispose"),
            (self.context, "close"),
            (self.browser, "close"),
            (self.playwright, "stop"),
        ):
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception:
                    pass
        self.api = self.context = self.browser = self.page = self.playwright = None
