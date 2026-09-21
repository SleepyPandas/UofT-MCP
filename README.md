# UofT MCP

A small Python MCP server for the public [UofT Timetable Builder](https://ttb.utoronto.ca/)
API, with read-only Degree Explorer and ACORN access and reusable UofT login.
It exposes **seven tools by default** over local stdio using the
[official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
Discover operations on demand, batch independent reads, and select or page short-lived
results without repeatedly fetching them. The existing 25-tool interface remains
available with `--tool-profile legacy`. Both profiles support the same timetable,
Degree Explorer, ACORN, and authentication capabilities.

See the [efficiency and architecture guide](EFFICIENCY.md) for migration, worked
examples, result limits, extension instructions, and reproducible benchmarks.

> **Work in progress:** Degree Explorer and three basic ACORN reads are available
> in this checkout. Degree Explorer writes and ACORN enrolment changes are not implemented.

Public timetable tools require no login, API key, database, web server, or environment
variables. This is an
unofficial wrapper; it does not enroll students or write to ACORN. Saving a timetable
creates an anonymous public share link on the Timetable Builder, not a personal
account record.

Read the [release notes](CHANGELOG.md) for the current version's scope and known
limitations.

## Connect an MCP Client

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then add this
configuration to any client that supports `mcpServers`:

```json
{
  "mcpServers": {
    "uoft-timetable": {
      "command": "uvx",
      "args": ["uoft-mcp@latest"],
      "env": {
        "UV_HTTP_TIMEOUT": "300"
      }
    }
  }
}
```

Restart the client after saving its configuration. On Windows, if the client cannot
find `uvx`, restart it after installing uv or replace `"uvx"` with the absolute path
reported by `where.exe uvx`.

`uvx` downloads the published package into an isolated environment and starts the
`uoft-mcp` command. No repository clone, virtual environment setup, API key, server
URL, or listening port is needed. The first start can take longer while uv downloads
Python and the dependencies; later starts use its cache.

To pin a release instead of following the newest release, use
`"args": ["uoft-mcp==0.6.0"]`.

## Local Development

From a clone of this repository:

```powershell
uv python install 3.13
uv sync --locked --managed-python
```

`uv sync` creates `.venv`, installs the package and development tools, and uses the
committed `uv.lock`. `.python-version` selects Python 3.13; the package supports
Python 3.13 and newer.

Run the local checkout with:

```powershell
uv run --locked python -m uoft_mcp
```

The installed `uoft-mcp` command is another entry point. The process waits for an
MCP client on stdin; a blank terminal is expected. Use Ctrl+C to stop a manual run.
Stdout carries protocol messages only, and logging goes to stderr.

## Connect Degree Explorer and ACORN

Degree Explorer tools reuse your saved UofT session to read academic records and
existing plans. To try them from this checkout, install Chromium once:

```powershell
uv run --locked python -m uoft_mcp auth setup
```

Connect your MCP client to this local checkout (the published package does not gain
branch changes until a release):

```json
{
  "mcpServers": {
    "uoft": {
      "command": "uv",
      "args": ["--directory", "C:/path/to/UofT-MCP", "run", "--locked", "python", "-m", "uoft_mcp"]
    }
  }
}
```

Ask your assistant to **"Connect my UofT account to Degree Explorer and ACORN."**
Complete the official UofT login and Duo prompts in the dedicated Chromium window.
After each service first verifies your login, the browser stays on that service for
five more seconds, rechecks access, and captures the latest session before moving
to the next service or closing. Login state is encrypted locally
and reused after browser closure and MCP restarts, while UofT still accepts it.
Passwords and Duo codes belong only on the official pages, never in chat or config.

Use **"Check my UofT connection"** to verify access, or **"Forget my saved UofT
session"** to delete local access. See [authentication setup and behavior](AUTHENTICATION.md)
for terminal commands, memory-only sessions, expiry, and troubleshooting.

## Default compact tools

| Tool | Purpose |
| --- | --- |
| `uoft_discover` | Search read operations; request their argument schemas only when needed. |
| `uoft_read` | Batch up to eight validated reads, with optional selection and bounded previews. |
| `uoft_result` | Inspect, filter, project, or page an in-memory snapshot without refetching. |
| `save_timetable` | Explicitly create an anonymous public timetable share. |
| `uoft_login` | Start or reuse official browser login. |
| `uoft_auth_status` | Check connection state and login progress. |
| `uoft_forget_session` | Delete saved access and invalidate private snapshots. |

For example, call `uoft_discover` with
`{"query":"get_course_details","detail":"schemas"}`, then `uoft_read` with:

```json
{"requests":[
  {"operation":"get_course_details","arguments":{"course_code":"CSC108H1"}},
  {"operation":"get_course_details","arguments":{"course_code":"CSC148H1"}}
]}
```

Results contain per-operation data or errors, retrieval timestamps, and transient
handles. Arrays default to 20 rows, with a maximum of 100; selected JSON data is
bounded to 16 KiB per item. Oversized objects return an explicit overview. Use
`uoft_result` with the handle and a narrower JSON Pointer, projection, or page.
Snapshots expire after five minutes and are never persisted to disk. Private
snapshots are invalidated on authentication changes. See [selection and result
contracts](EFFICIENCY.md#selection) before interpreting partial results.

## Legacy tools and operation names

Run `uv run --locked python -m uoft_mcp --tool-profile legacy` for the original
interface. The following read names are also valid `operation` values inside
compact `uoft_read`. `save_timetable` and the three auth controls remain direct
tools and cannot be included in a read batch.

| Tool | Arguments and purpose |
| --- | --- |
| `get_current_sessions` | No arguments. Get current session IDs; skip entries with `header: true`. |
| `get_reference_data` | No arguments. Get campus, division, delivery-mode, and sorting values. |
| `get_divisions` | No arguments. List recognized faculty/division codes. |
| `search_departments` | Required `term` keyword and `divisions` code. |
| `search_course_titles` | Required `term`, `divisions`, and `sessions` strings. Optional `lower_threshold=50`, `upper_threshold=200`. |
| `get_course_details` | Required `course_code`; optional `section_code` of `F`, `S`, or `Y`. The returned course `id` is required by `generate_timetable`. |
| `search_courses` | Optional code/title, section, description, division, session, campus, delivery, and pagination filters. |
| `generate_timetable` | Required `plans` array. Each plan has `courses` (`course_id` plus `activity_types`), optional `preference` of `early`, `balanced`, or `late`, and optional `blocked_times`. |
| `save_timetable` | Required `timetable` object with `sessions`, `timetables`, and `plans`. Returns the share `id` plus a `share_url`. |
| `retrieve_timetable` | Required `share_id` from `save_timetable`. |
| `uoft_login` | Optional `service` of `degree_explorer`, `acorn`, or `both` (default), plus `remember=true`. Starts or reuses official browser login and returns while you complete Duo. |
| `uoft_auth_status` | Optional `refresh=false`. Reports connection and login progress; `refresh=true` checks both services without opening a browser. |
| `uoft_forget_session` | No arguments. Cancels login and removes locally saved UofT session state and its encryption key. |
| `acorn_get_eligible_registrations` | Optional `fields` list. Read eligible registration periods; selection applies to each registration. |
| `acorn_get_dashboard_courses` | Optional `fields` list. Read dashboard enrolled courses for the current session. |
| `acorn_get_student_registration_info` | Optional `fields` list. Read registration/financial-hold status, person ID, and upcoming-exams flag. |
| `degree_explorer_get_academic_history` | No arguments. Read course history, sessions, marks, and requirements after Degree Explorer login. |
| `degree_explorer_get_student_data` | No arguments. Read the payload used by Degree Explorer's Current Status page. |
| `degree_explorer_get_student_record` | No arguments. Read the record payload used by Current Status; it is not a certified transcript. |
| `degree_explorer_get_student_user_data` | No arguments. Read menu/session user metadata. |
| `degree_explorer_get_student_menu` | No arguments. Read available Degree Explorer navigation entries. |
| `degree_explorer_get_messages` | No arguments. Read the UI string catalog, not a student inbox. |
| `degree_explorer_get_session_timeouts` | No arguments. Read client timeout settings; they do not extend a session. |
| `degree_explorer_get_planner` | No arguments. Read existing planner timelines and primary-plan flags. |
| `degree_explorer_get_cell_details` | No arguments. Read the unparameterized planner popup endpoint; it cannot target a cell. |

See [Degree Explorer tools and workflow](DEGREE_EXPLORER.md) for all nine authenticated
reads, their fixed API routes, and limitations. Each underlying operation accepts `{}` and reads only
the connected student's account.

See [ACORN tools and workflow](ACORN.md) for the three basic reads and field-selection
examples. They reuse the connected account without opening a login browser.

In legacy mode, each successful lookup, generation, and retrieve call returns one text block
containing the complete upstream JSON. The wrapper preserves fields and arrays,
including upstream `payload` and `status` envelopes. It does not summarize or
truncate course data. `save_timetable` keeps the upstream share object and adds
`share_url`.

Compact mode performs discovery, bounded batching, and local result selection before
returning data. It does not run arbitrary code or supply a code-execution sandbox.
[Measured fixture comparisons](EFFICIENCY.md#reproducible-verification) distinguish
model-facing output savings from upstream request counts.

### Legacy example workflow

1. Call `get_current_sessions` with `{}` and select a non-header entry's `value`.
2. Call `get_divisions` or `get_reference_data` for valid filter codes.
3. Call `search_course_titles` with these arguments, substituting the session value:

```json
{"term": "CSC108", "divisions": "ARTSC", "sessions": "SESSION_ID_FROM_STEP_1"}
```

4. Use the returned exact course code in `get_course_details`:

```json
{"course_code": "CSC108H1", "section_code": "F"}
```

5. For filtered, paginated results, call `search_courses`:

```json
{
  "course_code": "CSC108H1",
  "divisions": ["ARTSC"],
  "sessions": ["SESSION_ID_FROM_STEP_1"],
  "page": 1,
  "page_size": 2
}
```

`search_courses` also accepts `course_title`, `course_section_code`,
`search_course_description`, `campuses`, `delivery_modes`, and `direction` (`asc` or
`desc`). Pages start at **1**, page size defaults to **20**, and sorting defaults to
`asc`. Omitted collection filters become empty arrays. Course codes should be exact;
use autocomplete for prefixes or `course_title` for keyword searches.

6. Copy each selected offering's `id` from `get_course_details` into
`generate_timetable`. Activity types are the section types to fill, such as
`Lecture`, `Tutorial`, or `Practical`. Preference defaults to `balanced`. Optional
blocked intervals use weekday names and 24-hour `HH:MM` times:

```json
{
  "plans": [
    {
      "courses": [
        {
          "course_id": "COURSE_ID_FROM_STEP_4",
          "activity_types": ["Lecture", "Tutorial"]
        }
      ],
      "preference": "early",
      "blocked_times": [{"day": "Monday", "start": "8:00", "end": "10:00"}]
    }
  ]
}
```

Use one plan per term. A Fall and Winter year is two plans. The solver returns
chosen sections; it does not enroll students.

7. Store a Timetable Builder state with `save_timetable`. The `timetable` object
must include `sessions`, `timetables`, and `plans` in the frontend's serialized
shape. The tool returns `id` and `share_url` (`https://ttb.utoronto.ca/#!/?t=...`).

8. Reload that share later with `retrieve_timetable`:

```json
{"share_id": "SHARE_ID_FROM_STEP_7"}
```

## Checks

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
```

The [PR workflow](.github/workflows/tests.yml) runs these checks on pull requests
targeting `main` or `master`, using Python 3.13 on Ubuntu. New commits cancel an
older run for the same PR. Tests need no Chromium installation or UofT credentials.

The tests run offline. They cover the twenty-five legacy tools and seven compact tools, request mapping, raw JSON
preservation, validation, HTTP errors, timeouts, connection errors, invalid JSON,
shared-client cleanup, MCP discovery, and actual stdio subprocesses. Authentication
tests cover encrypted persistence, restoration, browser lifecycle, expiry, locking,
CLI controls, and sanitized status results using synthetic state and fake backends.
Degree Explorer tests also cover all nine GET mappings, MCP schemas and annotations,
JSON preservation, sanitized errors, response disposal, saved-session reuse, and
serialization with login and forget. These reads have not been verified against a
live authenticated account in this change. ACORN tests additionally cover compact JSON,
field selection, and expected root types. Both profiles have offline regression coverage; compact tests add batching, selection,
snapshot lifecycle, and efficiency budgets. A live ACORN read on
2026-09-19 returned a login redirect; authenticated live verification remains pending.

Previous timetable verification: 50 tests passed. Live checks of lookup, `generateYear`, `tiny/shorten`,
and `tiny/retrieve` succeeded, including generating CSC258H1 and CSC311H1, saving an
anonymous share, and retrieving that share. Live requests are deliberately not part
of the test suite, so tests remain reproducible.

Current authentication verification is recorded in [AUTHENTICATION.md](AUTHENTICATION.md#verification).

## API Notes

## Project layout

The MCP entry points remain `uoft_mcp.server` and `python -m uoft_mcp`. Service-specific
code is grouped below the package so integrations can grow independently:

```text
uoft_mcp/
├── timetable_builder/  # public Timetable Builder client and API reference
├── degree_explorer/    # allowlisted, read-only Degree Explorer client
├── acorn/              # allowlisted ACORN reads and field selection
├── utilities/          # shared authentication, browser, and secure session storage
├── server.py           # MCP tool registration and application wiring
└── cli.py              # stdio server and terminal authentication commands
```

- The supplied [Timetable Builder reference](uoft_mcp/timetable_builder/reference.json) remains the original
  reference. Live checks found two missing details: pagination starts at 1, and
  paginated search requires an empty `departmentProps` array when not filtering by
  department. The wrapper supplies it.
- `generateYear` accepts an array of plans. Each plan needs course `id` values from
  `get_course_details`, `sections` with `name: "*"` and a type, `fitnessFunctionOption`
  (`MORNING_WEIGHTED`, `BALANCED`, or `AFTERNOON_WEIGHTED`), and `blockedOff` intervals
  using weekday numbers 1-5 (Monday-Friday) and milliseconds since midnight.
- `tiny/shorten` stores the official `https://ttb.utoronto.ca/#!/?` URL for a
  serialized timetable and returns `{ "id": "..." }`. `tiny/retrieve?id=` loads it.
  These are anonymous share records, not ACORN enrolment.
- Get division codes from the API. For example, the live API uses `ERIN` and `SCAR`
  for Mississauga and Scarborough, rather than the reference's `UTM` and `UTSC` examples.
- Even one course can have a large response because all its sections are included.
  Choose narrow filters and small page sizes. Compact selection reduces returned data;
  neither profile fetches extra upstream pages automatically.
- HTTP failures become MCP tool errors containing the endpoint and status code.
  UofT may return HTTP 404 for no matching courses. Timeouts, connection failures,
  and malformed JSON get their own readable errors. No automatic retries occur.
- This API is not covered by an official support guarantee. Changes upstream may
  require updating the mappings. Successful HTTP responses are preserved as supplied,
  including any application-level status messages inside their JSON.
