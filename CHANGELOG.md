# Changelog

## Unreleased

### Added

- `find_courses` and `course_info`: read-only, question-shaped course research tools in
  both the compact and legacy profiles. `find_courses` searches codes, titles, and
  descriptions by keyword with human term names ("Fall 2026"), campus and division
  filters, and small pages (default 10). `course_info` returns up to eight compact
  course cards with plain-text requirements, breadth, and sections grouped by type with
  instructors, meeting times, enrolment, waitlist, and full/cancelled flags. A card is
  about 3 KB instead of the raw 27 KB.
- Pure formatter module `uoft_mcp/timetable_builder/format.py` with offline unit tests.

### Changed

- Tool counts are now nine (compact) and 27 (legacy). Tool definitions grow by 2,578
  bytes in both profiles, so compact definitions are 57.2% smaller than legacy (was
  65.7%); the 50% regression budget in the tests is unchanged.
- Compact instructions tell models to prefer `find_courses` and `course_info` for course
  questions.

## 0.6.0 — 2026-09-20

### Changed

- Default to seven compact MCP tools (`uoft_discover`, `uoft_read`, `uoft_result`,
  `save_timetable`, `uoft_login`, `uoft_auth_status`, `uoft_forget_session`) with
  progressive operation discovery, validated read batches, and short-lived result handles.
  Preserve the original 25-tool contract through `--tool-profile legacy`; restart
  clients when switching profiles.
- Share typed operation handlers and schemas between compact and legacy profiles.
  Batch up to eight reads, deduplicate equivalent requests, cap public concurrency
  at four, and preserve authenticated read serialization and sanitized partial errors.

### Added

- Bounded in-memory result snapshots and selection with JSON Pointer selection, key
  inspection, equality filters, projection, and paging with a 16 KiB selected-data
  budget and explicit completeness metadata via `uoft_result`.
- In-memory snapshot retention for five minutes, bounded to 32 entries / 32 MiB of
  serialized data. Invalidate private snapshots on session changes and prevent
  in-flight reads from restoring invalidated private results.
- Offline benchmark budgets and regression tests for both profiles, batching,
  selection, retention, authentication races, and stdio (285 tests passing).
- Comprehensive migration, workflow, and architecture documentation in
  [EFFICIENCY.md](EFFICIENCY.md).

## 0.5.0 — 2026-09-19

### Added

- Three authenticated, read-only ACORN tools for eligible registrations, current
  dashboard courses, and student registration info, reusing saved UofT sessions.
- Compact JSON with optional top-level field selection, short tool descriptions,
  and reusable client/projection helpers for future programmatic clients.
- ACORN transport, session lifecycle, field-selection, and MCP contract coverage;
  the full offline suite passes 240 tests. Live verification remains pending:
  the saved-session attempt returned an ACORN login redirect on 2026-09-19.

### Changed

- Reorganized the package into Timetable Builder, Degree Explorer, ACORN, and
  shared utility namespaces. The `uoft-mcp` command, `uoft_mcp.server` entry
  point, and all MCP tool names remain unchanged.

## 0.4.1 — 2026-09-14

### Fixed

- Browser login now requires verified access, waits five seconds on each newly
  connected service, and rechecks before capturing the latest session and moving
  on or closing. Failed checks retry within the existing five-minute timeout.
- Late cookies and local storage are included in the saved session and API
  handoff; cancellation retains earlier verified checkpoints when persistence
  is available. Valid saved sessions bypass the browser and delay.

### Changed

- Degree Explorer responses are parsed once while preserving complete JSON and
  sanitized errors. All 22 MCP tool interfaces remain unchanged.
- Authentication and architecture documentation now describe settling, retries,
  and the distinction between offline coverage and live verification.

### Added

- Offline regression coverage for browser settling, rechecks, late session state,
  cancellation, and single-pass Degree Explorer JSON parsing.
- PR tests, lint, and formatting checks for `main` and `master`, using Python 3.13
  on Ubuntu with no UofT credentials or browser installation required.

## 0.4.0 — 2026-09-12

### Added

- Nine read-only Degree Explorer MCP tools for academic history, Current Status
  payloads, menu data, client messages and timeouts, and planner reads.
- Encrypted saved-session reuse for those tools, with no browser opening during a
  read and no persistence of student response bodies.
- A [Degree Explorer tool guide](DEGREE_EXPLORER.md) covering routes, purpose,
  authentication, errors, privacy behavior, and the MCP contract.

### Changed

- Package metadata, README tool index, and pinned-install example now describe
  the full 22-tool server.

### Known limitations

- Degree Explorer reads have offline test coverage but were not verified against
  a live authenticated account for this release.
- The planner cell-popup endpoint has no documented selectors, so it cannot read
  a chosen cell. Degree Explorer writes and ACORN student-data tools are absent.
