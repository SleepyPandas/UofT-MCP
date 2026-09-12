# Changelog

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
