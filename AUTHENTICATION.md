# UofT session reuse

This branch prepares the MCP for Degree Explorer and ACORN. It can sign in, verify
connections, remember a session, and forget it. It does not yet expose academic
records, enrolment, or account changes as tools.

## First connection

From this checkout:

```powershell
uv sync --locked
uv run --locked python -m uoft_mcp auth setup
uv run --locked python -m uoft_mcp auth login
```

The installed `uoft-mcp` command accepts the same `auth` subcommands. Run setup
again after upgrading Playwright if it requests a different browser build. Desktop
Linux may also require Playwright's browser system dependencies installed by the
machine's administrator.

Login opens a dedicated Chromium window only when authentication is required. Type
your credentials and complete Duo on the official UofT pages. For `both`, Degree
Explorer connects first, followed by ACORN in the same browser context. This lets
UofT reuse its SSO session where permitted; an application can still request Duo.
The window closes when the checks finish. Closing it yourself cancels the current
login; an already verified service remains saved. The interactive flow has a
five-minute timeout across both services.

In an MCP client, `uoft_login({"service":"both","remember":true})` returns
immediately. Poll `uoft_auth_status({})` every few seconds while login is in
progress. No credentials or MFA codes are accepted as tool arguments.

## Checking and reconnecting

```powershell
uv run --locked python -m uoft_mcp auth status
uv run --locked python -m uoft_mcp auth login --service acorn
```

Terminal `status` verifies both services. In MCP, `uoft_auth_status({})` reports
cached metadata; `uoft_auth_status({"refresh":true})` makes connection checks.
Neither opens a browser or follows authentication redirects. The two checks each
have a 15-second network timeout. The terminal exits with 0 for complete success,
1 for partial success or a failed check, and 130 for Ctrl+C.

Per-service states distinguish `not_checked`, `connected`, `login_required`,
`access_denied`, `unavailable`, and `unexpected_response`. Login progress separately
reports `idle`, `in_progress`, `complete`, `partial`, `failed`, `cancelled`, or
`timed_out`. `last_verified` records a past successful check, not a guaranteed
expiration time. An outage or access denial does not erase the other app's session.

Each process starts with `not_checked` until a connection check succeeds. Cookies
on disk do not prove that UofT still accepts the login. When authentication expires,
explicitly run Login again. It checks saved sessions before opening the browser.
There is no periodic keepalive or automatic Duo retry loop.

UofT's current [MFA FAQ](https://security.utoronto.ca/services/utormfa/faqs/)
describes 24-hour remembered-device support for Standard applications and fresh
MFA at each login for Enhanced applications. Application sessions and SSO sessions
can have different lifetimes. Saved state cannot extend these server-side limits.

## Remembering and forgetting

Remembering is enabled by default. Browser cookies, local storage, and the browser
user agent are encrypted using Fernet. The key lives in Windows Credential Locker,
macOS Keychain, or a supported Linux Secret Service/KWallet backend. Plaintext
keyring plugins are rejected. Authentication response bodies, screenshots, traces,
and IndexedDB snapshots are not stored.

The encrypted file is `session.enc` in the `auth` subdirectory of the per-user
`uoft-mcp` data directory:

- Windows: `%LOCALAPPDATA%\uoft-mcp\auth`
- macOS: `~/Library/Application Support/uoft-mcp/auth`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/uoft-mcp/auth`

This location is independent of the checkout, working directory, and uv cache.
Sessions belong to the OS user running the MCP. Treat your OS account and its
credential store as the security boundary; this is not protection against programs
already running with that account's access.

If secure storage is missing or locked, status explicitly reports a memory-only
session. Such a session works while the MCP runs but cannot be restored after it
stops. To intentionally start a fresh memory-only login:

```powershell
uv run --locked python -m uoft_mcp auth login --no-remember
```

For MCP, use `remember=false`. This neither loads nor overwrites an earlier saved
login. The terminal process closes after login, so memory-only mode is mainly useful
through a long-running MCP client. Use Forget first when switching accounts and
wanting to remove the previous saved identity.

```powershell
uv run --locked python -m uoft_mcp auth forget
```

`uoft_forget_session({})` does the same: cancel login, close managed authentication
resources, remove local saved state, and delete the encryption key for both apps.
It does not log you out of other browsers or guarantee university-wide revocation.
If key deletion fails, status reports the incomplete cleanup and asks you to unlock
the OS store and retry.

Only one authenticated MCP process can own the session at a time. Close the other
MCP client before running terminal login/status/forget commands. A second client
gets a `session in use` message; its public timetable tools still work. Locks are
released on normal shutdown or process exit; do not delete a live lock file.

## Implementation references

- [Degree Explorer registry PR #2](https://github.com/SleepyPandas/unofficial-UofT-api-registry/pull/2): the connection check uses `GET /degreeExplorer/rest/dxMenu/getStudentMenu`. Live verification found an array, so the adapter validates an array rather than the registry client's object type hint.
- [ACORN exploration branch](https://github.com/SleepyPandas/unofficial-UofT-api-registry/blob/scratch/acornAPI/src/apis/acorn.py): the connection check uses `GET /sws/rest/enrolment/eligible-registrations`, expecting an array, including an empty array.
- [Playwright authentication](https://playwright.dev/python/docs/auth) and [API requests](https://playwright.dev/python/docs/api/class-apirequestcontext): browser state is restored into an API context after Chromium closes.

These are unofficial application endpoints. Connection probes discard their
payloads and return only status metadata. No arbitrary URL or authenticated request
tool is exposed. The application probes do not perform account mutations; the
official browser login naturally submits the required authentication forms.

## Verification

Automated verification uses synthetic cookies, fake secure storage, and simulated
browser/API adapters. It requires neither Chromium nor a real UofT account.

User-assisted acceptance sequence:

1. Connect both services and complete Duo in Chromium.
2. After Chromium closes, verify both services repeatedly.
3. Stop the MCP and start a fresh process; verify the saved sessions again.
4. Confirm expiry produces a reconnection instruction, and Forget removes local access.

Verified on Windows on September 11, 2026 (America/Toronto):

- 97 offline tests pass; Ruff lint and formatting checks pass.
- Source distribution and wheel build successfully; the wheel contains the auth
  modules and no saved-session files.
- Chromium setup succeeds. Before login, both live probes report `login_required`.
- Official browser login establishes both service sessions. The initial Degree
  Explorer check exposed the registry's incorrect object type hint; an HTTP 200
  array was verified without printing its contents, and the adapter was corrected.
- After Chromium closes, a fresh terminal process verifies both connections.
- Two separate real stdio MCP processes each complete two authenticated connection
  checks successfully: four calls using the saved state.
- Calling Login again with saved sessions completes without another interactive
  login. Saved access is retained for subsequent use.

Natural university-side expiry and live Forget were not exercised in this run;
expiry handling, local deletion, cancellation, and unavailable keyrings are covered
by offline tests. macOS and Linux OS-keyring/browser integrations have not been
live-tested on those platforms. No credentials, cookies, or student response bodies
are included in this verification record.
