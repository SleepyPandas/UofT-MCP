# Status messages

UofT MCP reports failures in plain words with one suggested next step. Every message
comes from one catalogue (`uoft_mcp/status.py`), so the same problem reads the same way
for Timetable Builder, Degree Explorer, and ACORN. Messages never include upstream
response bodies, redirect URLs, cookies, or student records.

| Kind | Machine-readable `state` | Example message |
| --- | --- | --- |
| `connected` | `connected` | Connected to ACORN. |
| `not_checked` | `not_checked` | ACORN has not been checked yet. Run uoft_auth_status with refresh=true to check. |
| `signed_out` | `login_required` | Signed out of ACORN. Run uoft_login to sign in again. |
| `duo_pending` | `login_required` | Duo sign-in pending or failed for ACORN. Run uoft_login and approve the Duo prompt. |
| `login_in_progress` | `login_in_progress` | UofT login is in progress. Finish it in the browser window, then check uoft_auth_status. |
| `access_denied` | `access_denied` | ACORN denied access (HTTP 403). Run uoft_login to sign in again; if it keeps happening, your account may not have access. |
| `service_down` | `unavailable` | ACORN is down or not responding (HTTP 503). Try again later. |
| `rate_limited` | `unavailable` | ACORN is busy and asked us to slow down (HTTP 429). Wait a minute, then try again. |
| `network` | `unavailable` | Can't reach ACORN: no internet connection, or UofT servers can't be reached. Check your connection (or VPN) and try again. |
| `page_changed` | `unexpected_response` | UofT changed the ACORN page, so the server couldn't read it (malformed JSON). Try again later; if it keeps happening, update uoft-mcp or report an issue. |
| `request_rejected` | `unexpected_response` | Timetable Builder didn't accept the request (HTTP 404 at getCoursesByCodeAndSectionCode/X). Check the course code or filters and try again. |
| `unknown` | `unavailable` | Something went wrong while talking to ACORN. Try again; if it keeps happening, run uoft_auth_status. |

## How problems are told apart

- **Signed out:** HTTP 401, a redirect to the UofT or Duo sign-in host, or a sign-in page
  where JSON was expected.
- **Duo pending:** a login that timed out before the service was verified.
- **Service down:** HTTP 408 or 5xx, a timeout, or a maintenance page.
- **Rate limited:** HTTP 429.
- **Network:** DNS, connection refused/reset, or no route to the host.
- **Page changed:** an unexpected redirect, non-JSON content, malformed JSON, or JSON of an
  unexpected shape from a fixed Degree Explorer or ACORN route.
- **Request rejected:** a Timetable Builder 4xx other than 403, such as no match for a course.

## Where they appear

- `uoft_auth_status` (and `uoft-mcp auth status`): each service keeps its `state`,
  `message`, and `last_verified` fields, and also reports `kind` and `next_action`.
- Tool errors from Degree Explorer, ACORN, and Timetable Builder reads, including
  `uoft_read` batch items, use the same messages. Exception classes keep their `state`
  attribute and gain `kind`.
