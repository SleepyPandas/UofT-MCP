# UofT Timetable Builder MCP

A small Python MCP server for the public [UofT Timetable Builder](https://ttb.utoronto.ca/)
API. It exposes ten tools over local stdio using the
[official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): seven
course-lookup tools, a schedule solver wrapper, and share save/retrieve tools.

> **Work in progress:** ACORN and Degree Explorer authentication is available, but
> their student-data and planning tools are still under development. The current
> ACORN and Degree Explorer controls only connect, check, and forget local sessions.

Public timetable tools require no login, API key, database, web server, or environment
variables. This is an
No API key, database, web server, or environment variables are required. This is an
unofficial wrapper; it does not enroll students or write to ACORN. Saving a timetable
creates an anonymous public share link on the Timetable Builder, not a personal
account record.

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
`"args": ["uoft-mcp==0.3.1"]`.

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

## Tools

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

Each successful lookup, generation, and retrieve call returns one text block
containing the complete upstream JSON. The wrapper preserves fields and arrays,
including upstream `payload` and `status` envelopes. It does not summarize or
truncate course data. `save_timetable` keeps the upstream share object and adds
`share_url`.

### Example Workflow

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

The tests run offline. They cover all ten tools, request mapping, raw JSON
preservation, validation, HTTP errors, timeouts, connection errors, invalid JSON,
shared-client cleanup, MCP discovery, and actual stdio subprocesses.

Verification: 50 tests passed. Live checks of lookup, `generateYear`, `tiny/shorten`,
and `tiny/retrieve` succeeded, including generating CSC258H1 and CSC311H1, saving an
anonymous share, and retrieving that share. Live requests are deliberately not part
of the test suite, so tests remain reproducible.

## API Notes

- The supplied [timetable_builder.json](timetable_builder.json) remains the original
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
  Choose narrow filters and small page sizes. The wrapper never fetches extra pages.
- HTTP failures become MCP tool errors containing the endpoint and status code.
  UofT may return HTTP 404 for no matching courses. Timeouts, connection failures,
  and malformed JSON get their own readable errors. No automatic retries occur.
- This API is not covered by an official support guarantee. Changes upstream may
  require updating the mappings. Successful HTTP responses are preserved as supplied,
  including any application-level status messages inside their JSON.

For a walkthrough of the code and how to extend it, read [EXPLAINED.md](EXPLAINED.md).
