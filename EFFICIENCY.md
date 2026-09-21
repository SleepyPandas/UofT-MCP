# Efficient MCP interface and extension guide

The default `compact` profile exposes seven tools, backed by the same university
operations as the optional 25-tool `legacy` profile. It reduces upfront tool
definitions, combines independent reads, and selects data before it reaches the
client. It does not run generated code or add new university actions.

## Setup and migration

Run the checkout normally to use compact mode:

```powershell
uv run --locked python -m uoft_mcp
```

Existing clients that call individual operation names can retain their contracts:

```powershell
uv run --locked python -m uoft_mcp --tool-profile legacy
```

For an MCP configuration, append `"--tool-profile", "legacy"` after `"uoft_mcp"`
in the local checkout's argument list. With the installed `uoft-mcp` entry point,
pass those two arguments directly. Restart the MCP process after changing profiles.
Only one profile is exposed per process. This branch is not a published release;
`uvx uoft-mcp@latest` does not acquire these changes until a release is published.

The compact tools are `uoft_discover`, `uoft_read`, `uoft_result`, `save_timetable`,
`uoft_login`, `uoft_auth_status`, and `uoft_forget_session`. The latter four retain
their existing contracts and annotations. Sharing, login, and forgetting are never
dispatched through the read batch. There are no Degree Explorer or ACORN writes.

## Discovery and execution

`uoft_discover` is local and requires no login. Parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `query` | `""` | Search operation names and descriptions. Exact names return only that operation. |
| `service` | omitted | Restrict to `timetable`, `degree_explorer`, or `acorn`. |
| `detail` | `descriptions` | `names`, `descriptions`, or `schemas`. |
| `limit` | 5 | Maximum matches, from 1 to 25. |

Results contain `operations`, `total`, and `has_more`. Text searches rank by matched
words, with names breaking ties. Descriptions show service and authentication
requirements; schemas additionally include full documentation and the argument
JSON Schema. These schemas describe operations inside `uoft_read`, not additional
dynamically registered MCP tools. No client-specific tool-loading extension is needed.

`uoft_read` accepts `requests`, an array of one to eight objects containing
`operation`, `arguments` (default `{}`), and `selection` (optional). Operation names
are the existing names in the README's legacy table, excluding the four direct
tools. The registry allowlists handlers; names are never interpreted as URLs or code.

All operation arguments and selection syntax are validated before any request in
the batch executes. This includes existing cross-field checks such as reversed
autocomplete thresholds and invalid blocked-time intervals. Selection errors that
depend on the fetched JSON shape are reported afterward for the affected item.

Identical operations with equivalent validated arguments share one fetch within
the batch, even when their selections differ. There is no automatic cross-batch
cache, retry, or upstream pagination. Public reads run with a server-wide maximum
of four concurrent requests. Authenticated reads retain the existing shared lock
with login, refresh, and forget. Batches contain independent operations: they do
not reference outputs of earlier items or execute conditionals.

### Result envelope

Successful `uoft_read` calls return one JSON text block with a `results` array in
input order. Each item identifies its `operation` and contains either an `error`
or snapshot metadata and a selected view. Upstream failures are sanitized per
item; other items can succeed. Invalid batch arguments produce an MCP tool error.
Clients must check each item for `error`, even when MCP `isError` is false.

Snapshot metadata includes `handle`, `retrieved_at` (UTC), and `retained`. If a
result exceeds the memory budget, `handle` is null, `retained` is false, and a
`warning` explains that follow-up requests must refetch. A selection failure can
still include a retained handle, allowing the client to correct its selection.

Views contain `pointer`, `complete`, `projected`, `filtered`, `total`, `offset`, and
`next_offset`, plus either `data` or `overview` and `guidance`. `total` counts rows
after filtering; for non-array views it is null. `next_offset` is null on the final
page or when an oversized view requires a narrower selection. `complete` means
the entire selected view is included, not that every upstream field or page was
returned. It is false for nonzero offsets, partial array pages, and overviews.

`uoft_result` accepts a `handle` and optional `selection`. It returns the same view
metadata and the original retrieval timestamp, with no network access. Expired,
evicted, invalidated, or foreign-process handles produce an MCP tool error directing
the caller to fetch again. Handles represent snapshots, not current university data
or proof of a currently valid login.

### Selection

| Parameter | Default | Meaning |
| --- | --- | --- |
| `pointer` | `""` | RFC 6901 JSON Pointer; empty selects the root. Escape `/` as `~1` and `~` as `~0`. |
| `mode` | `preview` | `keys` converts an object into key/type records for inspection and paging. |
| `equals` | omitted | Match immediate record fields in an array; all supplied comparisons must match. |
| `offset` | 0 | Zero-based offset into the filtered array. |
| `limit` | 20 | Array page size, 1–100. |
| `fields` | omitted | Project immediate object keys, either on an object or each array record. |

The order is subtree selection, optional key inspection, filtering, pagination,
then projection. Equality does not coerce strings into numbers or booleans. Missing
filter keys do not match. Projection preserves selected values without summarizing
them. Heterogeneous rows omit missing projected keys; an unknown key across the
selected records is an error. Empty arrays permit any projection. Mixed scalar/object
pages cannot be projected. Nested values are accessible through `pointer`; dotted
field names are literal keys. No expressions, regular-expression filters, or code
evaluation are accepted.

Selected `data` is limited to 16 KiB of compact UTF-8 JSON per item. The surrounding
metadata is additional. Oversized data becomes a bounded type/size overview; JSON
strings and records are never sliced to fit. Object overviews show at most 20 keys
within a 4 KiB key-list budget, with `keys_complete` indicating omissions. Use
`mode: "keys"` with offsets to inspect additional keys, or select a known subtree.
Objects and scalars do not use row offsets unless converted to key records.

Selection happens after upstream fetching. It reduces client/model output, not
the university response size. The 16 KiB limit applies to compact selected data;
legacy responses and the explicit public-share action preserve their old contracts.

## Worked workflows

### Public courses

Discover operation schemas when unfamiliar:

```json
{"query":"get_course_details","detail":"schemas"}
```

Fetch known course codes together using `uoft_read`:

```json
{"requests":[
  {"operation":"get_course_details","arguments":{"course_code":"CSC108H1"}},
  {"operation":"get_course_details","arguments":{"course_code":"CSC148H1"}}
]}
```

For filtered searches, batch `get_current_sessions` and `get_reference_data` first,
then use their returned codes with `search_courses`. Do not guess session IDs.
The compact interface does not automatically choose a campus, term, or division.

When a response is an object overview, inspect its actual keys:

```json
{"handle":"HANDLE_FROM_READ","selection":{"mode":"keys","limit":20}}
```

Select a returned key with `pointer`, then inspect the resulting subtree as needed.
For an array, advance using `next_offset`:

```json
{"handle":"HANDLE_FROM_READ","selection":{"pointer":"/payload","offset":20,"limit":20}}
```

This last example applies only when the fetched payload actually has an array at
`/payload`; JSON shapes differ by endpoint. Use actual keys, not assumed normalized
schemas. Pass returned course IDs and activity types to `generate_timetable` via
`uoft_read`. Sharing the completed timetable remains an explicit `save_timetable` call.

### Academic records

Connect through `uoft_login` and check `uoft_auth_status` as before. Discover only
the required schemas rather than every available service:

```json
{"query":"academic history","service":"degree_explorer","detail":"schemas"}
```

Batch the needed academic reads:

```json
{"requests":[
  {"operation":"degree_explorer_get_academic_history"},
  {"operation":"degree_explorer_get_planner"}
]}
```

Inspect each handle's actual shape and page or project only the records needed.
Degree Explorer field meanings are not normalized or inferred by this layer.
For ACORN's documented registration array, projection can be applied directly:

```json
{"requests":[{
  "operation":"acorn_get_eligible_registrations",
  "selection":{"fields":["sessionDescription","registrationParams"],"limit":5}
}]}
```

Prefer `selection.fields` in compact mode: it retains the original fetched payload
for later selection. ACORN's legacy `arguments.fields` is still accepted, but it
projects inside the operation before retention; omitted fields cannot subsequently
be recovered from that handle. Missing keys return a selection error, not guessed data.

If the client provides code execution, it can call these tools and process selected
JSON in its own execution environment. Only return the final needed information to
the model. This server does not supply that environment or enforce what the client
subsequently shares with its model.

## Lifetime and privacy

Snapshots are held only in the running process: five-minute fixed expiry, 32 entries,
and a 32 MiB total serialized-data budget, evicted least-recently-used first. Access
does not extend expiry. Expired entries are purged on the next store access and all
entries are cleared on shutdown. Python object overhead and in-flight response
buffers are outside the serialized budget; this is not a total process memory cap.

Private snapshots are cleared when login begins, forget begins, an authentication
probe/read fails, the authentication backend closes, or the process shuts down.
An authentication generation counter rejects private reads that began before an
invalidation, including results already fetched within a still-running batch.
Public snapshots survive private-session invalidation. Backend errors invalidate
conservatively even if they do not prove session expiry.

Student response bodies are never written to the session store, benchmark files,
or logs. The encrypted store continues to contain authentication state only.
Transient result handles do not survive restarts and do not provide access across
MCP processes. Already-returned data in a client cannot be revoked by forgetting.

## Architecture and extending operations

`operations.py` owns registry metadata and derives argument models from the same
typed handlers used by legacy registration. Read handlers return parsed Python
data; only the profile boundary serializes JSON, avoiding a full encode/decode
round trip before compact selection. `server.py` wires service handlers,
authentication, and application lifetime; its registration adapter exposes all
handlers in legacy mode and only the four direct tools in compact mode.
`compact.py` implements discovery and dispatch. `results.py` implements pure selection
and the transient store. The underlying clients retain their existing allowlisted
routes, error sanitation, cookie handling, and connection pooling.

To add a read operation:

1. Add an allowlisted service-client method and a typed handler using the existing
   registration adapter. Its first documentation paragraph should explain when to
   use it; subsequent paragraphs document limitations. Service is derived from the
   `degree_explorer_` or `acorn_` prefix, otherwise `timetable`.
2. Add cross-field validation to `validate_operation` when validation depends on
   multiple arguments. It must run without I/O. The handler must also retain its
   validation for legacy callers.
3. Add request-mapping, error, and compact-selection tests. Check schemas through
   discovery and rerun the efficiency budgets. Adding a read should not add another
   default MCP tool.

Future writes must be added explicitly to the direct-tool policy and registered
with appropriate annotations. The registry rejects non-read handlers outside that
policy. Do not hide writes inside a read handler, even when the upstream method is
POST: classification is by effect, not HTTP verb. Timetable search and generation
are read-only POST operations; public share creation is a write.

## Reproducible verification

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m uoft_mcp.benchmark
```

The benchmark uses HTTP mocks and a synthetic authentication adapter. It requires
no network, browser, credentials, or personal data. Each workflow reads three
operations returning a synthetic 100-row nested payload. Compact mode selects code/term fields from five
records per response, includes cold schema discovery, and fetches one
additional retained page. The fixture demonstrates large-response selection; it
does not establish real Degree Explorer field shapes or live response sizes.

Measured with the repository's locked environment:

| Metric | Legacy | Compact |
| --- | ---: | ---: |
| Exposed tools | 25 | 7 |
| Serialized tool definitions, bytes | 17,374 | 5,953 |
| Course workflow response bytes, including discovery | 423,120 | 2,416 |
| Academic workflow response bytes, including discovery | 423,120 | 7,623 |
| University requests per workflow | 3 | 3 |
| MCP tool calls per cold workflow | 3 | 3 |

Compact's three calls are discovery, one batch, and one extra snapshot page. Without
the extra page it takes two cold calls; with known schemas it takes one. Paging adds
zero university requests. Legacy receives the complete data in its three calls.
Definition size is approximately 65.7% smaller. These deliberately large fixtures
show response reductions of 99.4% and 98.2%, respectively; small responses or repeated
discovery may have little benefit or greater overhead.

Definitions are measured as compact serialized tool metadata; responses are UTF-8
JSON text bytes, not tokenizer counts or total JSON-RPC wire bytes. MCP calls count
tool calls and exclude initialization and `tools/list`. No live latency improvement
or universal token saving is claimed. CI enforces at least 50% smaller definitions
and 75% smaller fixture responses including discovery, plus paging without refetching.

Tests also exercise both profiles over real stdio, batch validation before I/O,
partial errors, public concurrency, private serialization, selection boundaries,
expiry, eviction, and authentication races. Authenticated live verification is
separate and remains pending for this infrastructure iteration.

## Design sources

- [Anthropic: Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
  motivates progressive discovery and processing intermediate data outside model context.
- [Anthropic: Writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents)
  motivates focused interfaces, concise results, and measurable workflow evaluations.
- [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
  defines discovery, annotations, text results, and tool errors. Compact mode remains
  ordinary MCP and does not require client-specific dynamic registration or a sandbox.
