"""Quiet renewal: a lapsed app session is refreshed through SSO without Duo or a login window."""

from copy import deepcopy

import pytest
from test_auth import FakeBrowser, Keys, manager

from uoft_mcp.acorn.client import AcornEndpoint, AcornError
from uoft_mcp.degree_explorer.client import DegreeExplorerEndpoint, DegreeExplorerError
from uoft_mcp.utilities.auth_browser import SERVICES, PlaywrightSession, ProbeResult


class RenewableBrowser(FakeBrowser):
    """An app session can lapse while UofT SSO stays alive (`sso_alive`)."""

    def __init__(self):
        super().__init__()
        self.sso_alive = True
        self.silent_calls = []
        self.renewals = 0

    async def silent_refresh(self, service):
        self.silent_calls.append(service.name)
        if not self.sso_alive:
            return ProbeResult("login_required", "SSO ended.")
        self.renewals += 1
        self.expired.discard(service.name)
        self.state["cookies"].append({"name": f"renewed-{service.name}", "value": "NEW"})
        return ProbeResult("connected", "Verified.")

    async def read_acorn(self, endpoint):
        if "acorn" in self.expired:
            raise AcornError("ACORN session lapsed.", "login_required")
        return ["rows"]

    async def read_degree_explorer(self, endpoint):
        if "degree_explorer" in self.expired:
            raise DegreeExplorerError("Degree Explorer session lapsed.", "login_required")
        return {"ok": True}


async def signed_in(tmp_path, **kwargs):
    keys = Keys()
    browser = RenewableBrowser()
    auth, browser = manager(tmp_path, browser, keys, **kwargs)
    await auth.login()
    assert (await auth.wait_for_login())["login"]["state"] == "complete"
    browser.visited.clear()
    return auth, browser, keys


def saved_values(auth):
    return {c["name"]: c["value"] for c in auth.store.load()["cookies"]}


@pytest.mark.anyio
async def test_status_check_renews_lapsed_session_and_persists_cookies(tmp_path):
    auth, browser, keys = await signed_in(tmp_path)
    browser.expired.add("acorn")
    result = await auth.refresh()
    assert result["services"]["acorn"]["state"] == "connected"
    assert browser.silent_calls == ["acorn"]
    assert browser.visited == [] and not browser.browser_open  # No login window.
    assert "renewed-acorn" in saved_values(auth)
    await auth.close()

    restarted, again = manager(tmp_path, RenewableBrowser(), keys)
    again.expired.add("degree_explorer")
    result = await restarted.refresh()  # A new process also renews, from the stored cookies.
    assert result["services"]["degree_explorer"]["state"] == "connected"
    await restarted.close()


@pytest.mark.anyio
async def test_read_renews_once_and_retries_without_reauthenticating(tmp_path):
    auth, browser, _ = await signed_in(tmp_path)
    browser.expired.add("acorn")
    assert await auth.read_acorn(AcornEndpoint.ELIGIBLE_REGISTRATIONS) == ["rows"]
    assert browser.silent_calls == ["acorn"]
    assert browser.visited == []
    assert "renewed-acorn" in saved_values(auth)
    assert auth.status()["services"]["acorn"]["state"] == "connected"

    browser.expired.add("degree_explorer")
    endpoint = next(iter(DegreeExplorerEndpoint))
    assert await auth.read_degree_explorer(endpoint) == {"ok": True}
    assert browser.silent_calls == ["acorn", "degree_explorer"]
    await auth.close()


@pytest.mark.anyio
async def test_login_uses_quiet_renewal_before_opening_a_window(tmp_path):
    auth, browser, _ = await signed_in(tmp_path)
    browser.expired.update({"acorn", "degree_explorer"})
    await auth.login()
    assert (await auth.wait_for_login())["login"]["state"] == "complete"
    assert browser.visited == [] and not browser.browser_open
    assert sorted(browser.silent_calls) == ["acorn", "degree_explorer"]
    await auth.close()


@pytest.mark.anyio
async def test_ended_sso_requires_interactive_login_and_is_rate_limited(tmp_path):
    auth, browser, _ = await signed_in(tmp_path)
    browser.expired.add("acorn")
    browser.sso_alive = False
    for _ in range(2):
        with pytest.raises(AcornError) as raised:
            await auth.read_acorn(AcornEndpoint.ELIGIBLE_REGISTRATIONS)
        assert raised.value.state == "login_required"
    assert browser.silent_calls == ["acorn"]  # The second read skipped the cooldown window.
    assert auth.status()["services"]["acorn"]["state"] == "login_required"
    assert browser.visited == []

    await auth.login("acorn")  # An explicit login always retries, then asks a person.
    await auth.wait_for_login()
    assert browser.silent_calls == ["acorn", "acorn"]
    assert browser.visited == ["acorn"]
    await auth.close()


@pytest.mark.anyio
async def test_no_saved_cookies_means_no_hidden_browser(tmp_path):
    auth, browser = manager(tmp_path, RenewableBrowser())
    result = await auth.refresh()
    assert result["services"]["acorn"]["state"] == "login_required"
    assert browser.silent_calls == []
    await auth.close()


@pytest.mark.anyio
async def test_renewal_crash_degrades_to_login_required(tmp_path):
    auth, browser, _ = await signed_in(tmp_path)
    browser.expired.add("acorn")

    async def explode(service):
        raise RuntimeError("SECRET page contents")

    browser.silent_refresh = explode
    result = await auth.refresh()
    assert result["services"]["acorn"]["state"] == "login_required"
    assert "SECRET" not in str(result)
    await auth.close()


@pytest.mark.anyio
async def test_other_errors_never_trigger_renewal(tmp_path):
    auth, browser, _ = await signed_in(tmp_path)

    async def unavailable(endpoint):
        raise AcornError("down", "unavailable")

    browser.read_acorn = unavailable
    with pytest.raises(AcornError):
        await auth.read_acorn(AcornEndpoint.ELIGIBLE_REGISTRATIONS)
    assert browser.silent_calls == []
    await auth.close()


# --- The real adapter, with Playwright replaced by recording fakes -------------------------


class FakeResponse:
    def __init__(self, status=200, body="[]"):
        self.status, self.headers, self._body = status, {"content-type": "application/json"}, body

    async def text(self):
        return self._body

    async def dispose(self):
        pass


class FakeApi:
    def __init__(self, state=None, response=None):
        self.state = state or {"cookies": [], "origins": []}
        self.response = response or FakeResponse()
        self.disposed = False

    async def get(self, url, **kwargs):
        return self.response

    async def storage_state(self):
        return deepcopy(self.state)

    async def dispose(self):
        self.disposed = True


class FakeContext:
    def __init__(self, page, api):
        self.page, self.request, self.closed = page, api, False

    async def new_page(self):
        return self.page

    async def storage_state(self):
        return await self.request.storage_state()

    async def close(self):
        self.closed = True


class FakePage:
    def __init__(self, url, prompt=False):
        self.url, self.prompt = url, prompt

    async def goto(self, url, **kwargs):
        pass

    async def query_selector(self, selector):
        return object() if self.prompt else None


class FakeChromium:
    def __init__(self, page, context_api):
        self.page, self.context_api, self.launched = page, context_api, []
        self.browser_closed = False

    async def launch(self, headless):
        self.launched.append(headless)
        return self

    async def new_context(self, **options):
        self.options = options
        return FakeContext(self.page, self.context_api)

    async def close(self):
        self.browser_closed = True


def adapter(page, context_api):
    session = PlaywrightSession()
    session.api = FakeApi({"cookies": [{"name": "old"}], "origins": []})
    session.user_agent = "Saved UA"
    chromium = FakeChromium(page, context_api)
    created = []

    class Request:
        async def new_context(self, **options):
            created.append(options)
            return FakeApi(options["storage_state"])

    session.playwright = type("P", (), {"chromium": chromium, "request": Request()})()
    return session, chromium, created


@pytest.mark.anyio
async def test_real_adapter_renews_hidden_and_swaps_in_new_cookies():
    renewed = FakeApi({"cookies": [{"name": "new"}], "origins": []})
    session, chromium, created = adapter(FakePage("https://acorn.utoronto.ca/sws/x"), renewed)
    previous = session.api
    result = await session.silent_refresh(SERVICES["acorn"])
    assert result.state == "connected"
    assert chromium.launched == [True]  # Headless: nothing for the user to see or type into.
    assert chromium.options["user_agent"] == "Saved UA"
    assert chromium.options["storage_state"]["cookies"] == [{"name": "old"}]
    assert previous.disposed
    assert created[0]["storage_state"]["cookies"] == [{"name": "new"}]
    assert (await session.snapshot())["cookies"] == [{"name": "new"}]
    assert chromium.browser_closed


@pytest.mark.anyio
async def test_real_adapter_stops_at_credential_prompt_without_touching_state():
    session, chromium, created = adapter(
        FakePage("https://idpz.utorauth.utoronto.ca/idp/profile/SAML2", prompt=True), FakeApi()
    )
    previous = session.api
    result = await session.silent_refresh(SERVICES["degree_explorer"])
    assert result.state == "login_required"
    assert session.api is previous and not previous.disposed and not created
    assert chromium.browser_closed


@pytest.mark.anyio
async def test_real_adapter_never_runs_during_interactive_login():
    session, chromium, _ = adapter(FakePage("https://acorn.utoronto.ca/sws/"), FakeApi())
    session.context = object()
    assert (await session.silent_refresh(SERVICES["acorn"])).state == "login_required"
    assert chromium.launched == []
