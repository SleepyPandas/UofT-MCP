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
- `plan_timetable`, a direct tool in both profiles that turns course codes into a
  conflict-free schedule in one call: resolves each code and term, derives lecture,
  tutorial and practical types from open sections, solves per term with the UofT solver
  (year-long courses are scheduled once and block their winter meetings for the winter
  solve), reports chosen sections, instructors and a weekly overview, and by default
  returns a verified `https://ttb.utoronto.ca/#!/?t=` share link (`share=false` skips it).
  Blocked times the solver cannot honour are returned as warnings; overlaps or missing
  section types are reported as "no schedule without conflicts".
- `UOFT_MCP_BROWSER=auto|msedge|chrome|chromium` selects the sign-in browser.

### Changed

- Tool counts are now ten (compact) and 28 (legacy). The course research tools and
  `plan_timetable` add 4,499 bytes of tool definitions to both profiles, so compact
  definitions are 52.2% smaller than legacy (was 65.7%); the 50% regression budget in
  the tests is unchanged.
- Compact instructions tell models to prefer `find_courses` and `course_info` for course
  questions.
- Sign in with an installed Microsoft Edge or Google Chrome, falling back to Chromium
  from `uoft-mcp auth setup`, so most people no longer need a browser download. Edge is
  tried first on Windows and Chrome first on macOS and Linux. Interactive login and
  quiet renewal share the same choice. The browser still starts with a temporary,
  empty profile, never your own.
- `uoft-mcp doctor` reports which browser sign-in will use and recommends `auth setup`
  only when none is available.
- `uoft-mcp auth setup` skips the Chromium download when Edge or Chrome is installed;
  use `--force` to download anyway.
- The install scripts no longer suggest downloading Chromium. `UOFT_MCP_WITH_BROWSER=1`
  still downloads it (by running `auth setup` with `UOFT_MCP_BROWSER=chromium`, which
  also works with already-published releases).
- Move the `generateYear` request models and share posting into
  `timetable_builder/generation.py` and `timetable_builder/client.py`; `generate_timetable`
  and `save_timetable` behave exactly as before.

### Fixed

- Course cards no longer hide enrolment instructions: course notes keep up to 1,500
  characters (was 400), and anything cut or dropped is marked with how much was omitted.
- `lectures_full` on course cards and search rows is now derived from the lecture
  enrolment counts instead of copying upstream's `primaryFull`, which can say a course
  has seats when every lecture is full. When the two disagree the row also carries
  `upstream_lectures_full`.
- `plan_timetable` no longer returns section pairs the course forbids (for example
  CSC236H1 LEC0201 with TUT0401). The upstream solver ignores each section's
  `linkedMeetingSections`, so the planner validates its output against them, swaps in the
  best allowed combination that clashes with nothing else (reported under Warnings), and
  fails with a plain explanation when none exists. Courses whose notes carry enrolment
  instructions the section data does not encode now get a warning quoting the note.
- `plan_timetable` shows each chosen section's enrolment (`37/37 enrolled, FULL,
  waitlist 3`) and warns about every full section, noting that seat counts are a
  snapshot.

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
