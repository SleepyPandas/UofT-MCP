# Changelog

## Unreleased

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
