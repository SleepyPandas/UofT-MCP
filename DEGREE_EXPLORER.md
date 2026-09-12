# Degree Explorer read tools

Nine tools read Degree Explorer using the connected student's saved UofT session.
Every tool accepts an empty argument object (`{}`); there are no student IDs,
credentials, URLs, arbitrary query parameters, filters, or pagination arguments.
Each call makes one GET to a fixed route and returns the complete upstream JSON
in one MCP text block. Objects, arrays, nulls, and application status fields are
preserved without interpretation or truncation. Calls do not retry automatically.

## Choose a tool

All routes below are relative to
`https://degreeexplorer.utoronto.ca/degreeExplorer/rest`.

| Tool | Purpose and usage | GET route |
| --- | --- | --- |
| `degree_explorer_get_academic_history` | Course history, sessions, marks, and requirement data. Start here for completed coursework. | `/dxStudent/getAcademicHistory` |
| `degree_explorer_get_student_data` | The student payload used by Current Status. Start here for current academic status. | `/dxStudent/getStudentData` |
| `degree_explorer_get_student_record` | The underlying record used by Current Status. Read when additional record details are needed; this is not a certified transcript. | `/dxStudent/getStudentRecord` |
| `degree_explorer_get_student_user_data` | Menu/session user metadata, potentially including identity. For app-shell context; use `uoft_auth_status` for connection checks. | `/dxMenu/getStudentUserData` |
| `degree_explorer_get_student_menu` | Available navigation entries; the observed root is an array. Does not report degree completion. | `/dxMenu/getStudentMenu` |
| `degree_explorer_get_messages` | UI string catalog for interpreting labels/message keys. This is not a student inbox. | `/messages/getMessages` |
| `degree_explorer_get_session_timeouts` | Web-client timeout settings. These are not remaining session lifetime and do not extend login. | `/dxMenu/getSessionTimeouts` |
| `degree_explorer_get_planner` | Existing planner timelines and primary-plan flags. Does not create, edit, select, or evaluate plans. | `/dxPlanner/getPlanner` |
| `degree_explorer_get_cell_details` | Unparameterized planner popup read. Cell selectors are undocumented, so this cannot select a specific cell; it may return empty data or fail if UI context is required. | `/dxPlanner/getCellDetails` |

The exact distinction and field schemas of the two Current Status payloads are not
documented by the registry. The tools preserve them rather than invent a normalized
student schema. Fetch only what the question needs; results can contain private
academic records and may be large.

## Example workflow

1. Call `uoft_login` with `{"service":"degree_explorer"}` if access has not been
   connected. Complete the official browser login and Duo. Never enter credentials
   in tool arguments or chat.
2. Check `uoft_auth_status` with `{}`, waiting a few seconds between checks until
   Degree Explorer reports `connected`. Saved access is also reused after restart
   without needing a new browser login while the university accepts the session.
3. Call `degree_explorer_get_academic_history` with `{}` for coursework, or
   `degree_explorer_get_student_data` with `{}` for Current Status.
4. For existing plans, call `degree_explorer_get_planner` with `{}`. The cell-details
   tool cannot drill into a chosen timeline or cell until selectors are verified.
5. Use `uoft_forget_session` with `{}` when you want to remove local saved access.

## Errors and session behavior

These tools never open a login browser, follow redirects, or wait for an active Duo
login. During login they return a tool error directing the caller to check status.
Expired login (HTTP 401, an SSO redirect, or a recognized login page) becomes a
tool error asking for `uoft_login`. HTTP 403 reports access denial; 429 and 5xx
report unavailability. Other HTTP failures, malformed JSON, unexpected redirects,
non-JSON responses, and network failures become sanitized MCP errors (`isError`).
The request timeout is 30 seconds. No upstream error bodies, headers, redirect
URLs, or raw exception details are returned in errors.

Reads serialize with login, refresh, and forget. Rotated cookies use the existing
encrypted session store, including its memory-only behavior. Student response
bodies are not logged or written to the store; Playwright's retained response is
disposed after every call. The returned academic data is visible to the MCP client.
Successful HTTP JSON, including any application-level error envelope, is returned
as supplied; it is not treated as a guarantee of academic eligibility or completion.

## Contract and verification

The implementation follows the
[MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools):
unique names, descriptions, object input schemas with no declared parameters,
read-only/non-destructive/idempotent annotations, an explicit external-service
(`openWorldHint`) annotation, text results, and tool execution errors. An output
schema is omitted because upstream JSON shapes are not fully documented. This
matches the repository's `structured_output=False` convention.

Routes and behavior are based on the registry's
[Degree Explorer JSON](https://github.com/SleepyPandas/unofficial-UofT-api-registry/blob/48dc6239b35f36cae0e662b25ec0c61000eab3fd/json/degree_explorer.json)
and [read client](https://github.com/SleepyPandas/unofficial-UofT-api-registry/blob/48dc6239b35f36cae0e662b25ec0c61000eab3fd/src/apis/degree_explorer.py).
The Python client permits arbitrary cell query parameters but does not define
their names or semantics; this MCP deliberately exposes no such parameter bag.

Offline tests cover all nine mappings through the MCP SDK, discovery, JSON
preservation, authentication errors, sanitized failures, response cleanup, saved
access restoration, cookie rotation, and concurrent login/forget. The existing
stdio subprocess tests verify discovery of all 22 server tools. No authenticated
live reads were performed for this change; the cell popup's usefulness without
UI context remains unverified. No Degree Explorer mutation routes are exposed.
