# ACORN read tools

The default compact profile exposes these capabilities as operations inside
`uoft_read`. Discover their schemas with `uoft_discover` using `service: "acorn"`.
For example:

```json
{"requests":[{"operation":"acorn_get_eligible_registrations",
"selection":{"fields":["sessionDescription","registrationParams"],"limit":5}}]}
```

Compact results have bounded previews and short-lived handles; use `uoft_result`
for further selection without refetching. Prefer `selection.fields` to preserve the
original response in the snapshot. Passing `arguments.fields` retains only the
operation's already-projected response. See [efficiency contracts](EFFICIENCY.md).

The direct calls and full-response behavior below describe `--tool-profile legacy`
and the underlying operations, whose arguments and route mappings are unchanged.

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
student payloads are neither logged nor saved to disk. Compact mode retains
bounded, expiring snapshots in process memory. Returned JSON is visible to the
MCP client. Field selection occurs locally after fetching the upstream response,
so it reduces model-facing output, not upstream bandwidth. Omitting fields may
still return a large response in legacy mode. Compact mode provides explicit
completeness and pagination metadata instead.

## Efficient composition

The shared registry exposes these reads through both profiles. Compact mode supports
local schema discovery, validated batches, JSON Pointer selection, filtering, and
paging of transient results. Authenticated reads remain serialized with session
changes; batching reduces MCP round trips rather than parallelizing cookie access.
The server provides no code sandbox. Client code execution can compose operations
and process results separately. See the [architecture guide](EFFICIENCY.md).

## Verification

The offline suite covers all three mappings through the MCP SDK, both tool profiles
over stdio, projections, unexpected responses, authentication
failures, cancellation cleanup, cookie rotation, and login/forget serialization.
Fixtures are synthetic; no student records are committed. Ruff lint and format
checks pass.

On 2026-09-19, a live read using saved access reached ACORN and returned an HTTP
302 login redirect. No browser was opened and no student data was printed.
Authenticated verification of all three endpoints remains pending a fresh login.
