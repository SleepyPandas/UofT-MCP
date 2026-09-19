# ACORN read tools

These three tools use the connected student's ACORN account. They expose no
course enrolment, account changes, arbitrary URLs, or arbitrary query parameters.

| Tool | GET route under `https://acorn.utoronto.ca/sws/rest` |
| --- | --- |
| `acorn_get_eligible_registrations` | `/enrolment/eligible-registrations` |
| `acorn_get_dashboard_courses` | `/dashboard/courseRegistration/enrolledCourses` |
| `acorn_get_student_registration_info` | `/profile/studentRegistrationInfo` |

Routes and expected JSON roots follow the
[pinned ACORN registry](https://github.com/SleepyPandas/unofficial-UofT-api-registry/blob/48dc6239b35f36cae0e662b25ec0c61000eab3fd/json/acorn.json).
Eligible registrations returns an array; the other two return objects. Dashboard
courses concern the current session and do not provide registration-specific
waitlist details. Student registration info provides registration/financial-hold
status, `personId`, and `studentHasFutureExams`; it is not a name/email directory
or an academic transcript. Upstream status fields are preserved, not interpreted
as a guarantee of eligibility.

## Calling the tools

1. Call `uoft_login` with `{"service":"acorn"}` if access is needed, and check
   `uoft_auth_status` until login completes. Use the official browser for Duo.
2. Call any read tool with `{}` for complete upstream JSON, or choose fields:

```text
acorn_get_eligible_registrations({"fields":["sessionDescription","registrationParams"]})
acorn_get_dashboard_courses({"fields":["enrolledCourses","sessionDescription"]})
acorn_get_student_registration_info({"fields":["registrationStatusList","studentHasFutureExams"]})
```

Each successful call returns one compact JSON text block. `fields` selects exact,
case-sensitive top-level keys; it does not select nested paths. Arrays apply the
selection to each registration object. Unknown fields and empty selections are
tool errors. Omit `fields` to discover keys rather than guessing them.

For heterogeneous registration rows, a field is valid if present in any row;
rows lacking it omit that key. Empty registration arrays stay empty and validate
selections against the registry's five documented keys: `candidacyPostCode`,
`candidacySessionCode`, `sessionDescription`, `post`, and `registrationParams`.
Empty objects are preserved without selection; selecting a missing key is an
error. Values, nested structures, nulls, and ordering within arrays are preserved.
Selection does not change authentication status or mutate the fetched payload.

## Authentication and errors

Each tool performs one allowlisted GET with a 30-second timeout, no retries,
no followed redirects, and no automatic browser login. Reads serialize with
login, refresh, and forget. A login in progress returns a tool error immediately.
Expired access directs the caller to `uoft_login`; access denial, unavailable
services, malformed JSON, and unexpected root types return sanitized tool errors.
Playwright response bodies are disposed even when reads fail or are cancelled.

Only session cookies/storage use the existing encrypted persistence mechanism;
student payloads are neither logged nor saved. Returned JSON is visible to the
MCP client. Field selection occurs locally after fetching the upstream response,
so it reduces model-facing output, not upstream bandwidth. Omitting fields may
still return a large response; there is no implicit truncation.

## Efficient composition

The transport in `uoft_mcp.acorn.client`, authenticated reads through
`AuthManager.read_acorn`, and projection in `uoft_mcp.acorn.selection` are separate
from MCP wrappers. Tools have short descriptions and expose only one optional
argument. A future agent execution environment can call MCP tools, parse their
JSON text, filter or combine results, and return only the needed summary.

This follows the direction of Anthropic's
[Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp).
The server itself provides no execution sandbox, dynamic tool-discovery system,
or automatic privacy boundary around data a client returns to its model.
Existing Degree Explorer interfaces remain unchanged.

## Verification

The full offline suite passes 240 tests, including all three mappings through the
MCP SDK, 25-tool stdio discovery, projections, unexpected responses, authentication
failures, cancellation cleanup, cookie rotation, and login/forget serialization.
Fixtures are synthetic; no student records are committed. Ruff lint and format
checks pass.

On 2026-09-19, a live read using saved access reached ACORN and returned an HTTP
302 login redirect. No browser was opened and no student data was printed.
Authenticated verification of all three endpoints remains pending a fresh login.
