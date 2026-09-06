---
spec_id: adr-055-webmcp-bridge
title: "ADR-055 Spec 1 — WebMCP Bridge Over The Shared FastMCP Registry"
status: Draft
feature_branch: docs/2263-adr-055-specs
created: 2026-09-05
input: "Owner-directed live session: author the ADR-055 implementation spec set under umbrella issue #2263. Spec 1 transplants and hardens the hackathon demo's HTTP-to-WebMCP bridge (scistudio-web-demo commit 952f697b, read-only reference at .scratch-design/webmcp-recovery/scistudio-web-demo) per ADR-055 sections 4 and 9.2. Owner decisions recorded: new tools live in the shared FastMCP registry with tag-based per-transport visibility (no router-internal dispatch, no second registry); the bridge defines the session-substrate contract (one middleware, two identity backends: loopback token here, Hub OAuth in adr-055-lab-deployment); bridge URLs are prefix-aware per adr-055-prefix-independence; bounded call logging (operation identifiers and outcomes, never full arguments)."
owners:
  - "@jiazhenz026"
related_adrs:
  - 55
  - 40
related_specs:
  - adr-055-prefix-independence
  - adr-055-agent-context-workspace
  - adr-055-lab-deployment
scope:
  in:
    - The HTTP bridge router `src/scistudio/api/routes/webmcp.py` with `GET /api/webmcp/tools` and `POST /api/webmcp/call`, dispatching through the shared module-level FastMCP registry (`src/scistudio/ai/agent/mcp/server.py`).
    - "An explicit result adapter contract: structured results, non-text content blocks, and error propagation between FastMCP `ToolResult` and the browser-consumable response shape."
    - "Tag-based per-transport tool visibility: tools tagged for external audiences appear in the webmcp catalogue and are filtered out of the local socket transport's `tools/list`."
    - The browser registration module `frontend/src/webmcp/` (catalogue fetch, host capability probing for both `document.modelContext` and `navigator.modelContext`, registration with AbortController, superseded-attempt detection, reconnect and partial-failure tolerance), wired from `frontend/src/main.tsx`.
    - "Project binding for bridge calls: the caller presents the project it believes is active; mutation-tagged calls are rejected when that selection is stale."
    - "The session-substrate contract for bridge endpoints: one authentication middleware with pluggable identity backends; this spec implements the loopback token backend delivered through the served page."
    - A bounded call-logging policy for the bridge (operation identifiers and outcomes; never full arguments).
    - All bridge URLs built prefix-aware via adr-055-prefix-independence helpers.
  out:
    - "The domain tools themselves (`get_agent_context`, workspace, execution): adr-055-agent-context-workspace defines them; this spec provides only registration, catalogue filtering, and dispatch."
    - Hub OAuth and per-user routing (adr-055-lab-deployment); this spec defines only the middleware seam they plug into.
    - The AI-host presentation (deferred by owner; no spec in this set).
    - Any change to the local socket transport's wire protocol or to existing tool behavior.
governs:
  modules:
    - scistudio.ai.agent.mcp.server
    - scistudio.api.app
    - scistudio.api.spa
    - scistudio.api.routes.webmcp
  contracts:
    - scistudio.ai.agent.mcp.server.mcp
  entry_points: []
  files:
    - docs/specs/adr-055-webmcp-bridge.md
    - src/scistudio/ai/agent/mcp/server.py
    - src/scistudio/ai/agent/mcp/__init__.py
    - src/scistudio/api/app.py
    - src/scistudio/api/spa.py
    - src/scistudio/api/routes/webmcp.py
    - frontend/src/main.tsx
    - frontend/src/lib/api/core.ts
    - frontend/src/webmcp/**
  excludes: []
planned_governs:
  modules: []
  contracts: []
  entry_points: []
  files: []
  excludes: []
tests:
  - tests/api/test_webmcp.py
  - tests/ai/test_mcp_fastmcp.py
  - frontend/src/webmcp/register.test.ts
acceptance_source: adr
language_source: en
---

# ADR-055 Spec 1 — WebMCP Bridge Over The Shared FastMCP Registry

## 1. Change Summary

This spec comes from ADR-055 (sections 4 and 9.2) and umbrella issue #2263.

ADR-055 selected the demonstrated HTTP-to-WebMCP bridge architecture: the page
discovers tools over HTTP, registers browser callbacks with the host's WebMCP
API, and forwards invocations to the same module-level FastMCP registry that
serves the local socket transport. The hackathon demo
(`scistudio-web-demo` commit `952f697b`, kept read-only at
`.scratch-design/webmcp-recovery/scistudio-web-demo`) proved the shape: a
146-line router plus a small browser registration module. This spec is the
production version of that transplant, with the robustness findings of ADR-055
section 9.2 turned into requirements.

Four decisions go beyond the demo, all recorded with the owner:

1. **One registry, filtered per transport.** New external-facing tools register
   in the shared FastMCP instance with an audience tag; the webmcp catalogue
   includes them and the local socket transport filters them out. The demo's
   router-internal synthesized tools with if-chain dispatch are forbidden —
   ADR-055 section 4: the HTTP route does not become a second registry.
2. **An explicit adapter contract.** The demo serializes everything into a JSON
   text block and drops the top-level error flag; this spec defines the
   structured-result, non-text-content, and error mappings and makes them
   testable.
3. **A session substrate.** Bridge endpoints sit behind one middleware with two
   pluggable identity backends: a loopback token (this spec) and Hub OAuth
   (`adr-055-lab-deployment`). The token is delivered through the served page
   bootstrap, reusing the runtime-injection mechanism from
   `adr-055-prefix-independence`.
4. **Project binding.** A bridge call presents the project it believes is
   active; mutation-tagged calls with a stale selection are rejected, so
   opening another page cannot silently redirect an in-flight write.

The bridge depends on `adr-055-prefix-independence` for all URL construction
and is the foundation for `adr-055-agent-context-workspace` (the tools it
exposes) and `adr-055-lab-deployment` (its second identity backend).

## 2. User Scenarios & Testing

### User Story 1 - Catalogue and call parity through the bridge (Priority: P1)

An external AI host's page lists the same registry-backed tools a local agent
sees (minus external-only filtering) and invokes them through HTTP, receiving
the same results the local socket transport would produce.

**Why this priority**: This is the bridge itself; everything else in ADR-055
assumes it works.

**Independent Test**: With a project open, `GET /api/webmcp/tools` returns
entries whose names, descriptions, and input schemas match
`await mcp.list_tools()` for the non-external-tagged subset; calling a
representative read tool and a representative write tool via
`POST /api/webmcp/call` produces results equivalent to the local transport's
`tools/call` for the same tool and arguments.

**Acceptance Scenarios**:

1. **Given** a running backend with an active project, **When** a client
   requests `GET /api/webmcp/tools`, **Then** every entry carries `name`,
   `description`, and `inputSchema` taken from the FastMCP registry entry, plus
   derived `category` and `mutation` flags from the tool's tags.
2. **Given** the catalogue, **When** a client posts a valid call for a known
   tool, **Then** the router dispatches through `mcp.call_tool` and returns the
   adapted result; **When** the tool name is unknown, **Then** the response is
   a 404 with an unknown-tool detail (demo behavior preserved).
3. **Given** a tool that raises, **When** it is invoked through the bridge,
   **Then** the response follows the adapter error contract (below) rather than
   an HTTP 5xx.

### User Story 2 - The adapter contract preserves results, content, and errors (Priority: P1)

Result adaptation is defined and tested, fixing the demo's text-only mapping:
structured content survives as structured content, supported non-text content
has a declared representation, and a tool's top-level error flag propagates.

**Why this priority**: Silent loss of structured results or error flags makes
external agents act on wrong information — a correctness issue, not polish.

**Independent Test**: Fixture tools returning (a) a structured Pydantic result,
(b) mixed content blocks including a non-text block, and (c) a result with a
top-level error flag are invoked through the router; the responses match the
mapping table in FR-003 exactly.

**Acceptance Scenarios**:

1. **Given** a tool returning structured content, **When** called through the
   bridge, **Then** the response carries the structured object in the declared
   field with no lossy text round-trip.
2. **Given** a tool returning a non-text content block the host cannot consume,
   **When** called through the bridge, **Then** the response applies the
   declared fallback representation and marks the substitution.
3. **Given** a tool result with `isError` set, **When** called through the
   bridge, **Then** the response preserves the error flag at the top level.

### User Story 3 - Browser registration survives real host behavior (Priority: P2)

The registration module tolerates a missing or late WebMCP capability, repeated
registration, a superseded attempt, reconnects, and partial failures — and an
obsolete registration never reports itself as current.

**Why this priority**: ADR-055 section 9.2 found registration is invoked once
with no superseded-attempt check; hosts differ (Chrome 149 `navigator`, 150
`document`; ChatGPT desktop hit the `document` form) and trial-era APIs move.

**Independent Test**: `frontend/src/webmcp/register.test.ts` drives a fake
`ModelContext` through: capability absent at load then appearing, two
overlapping registration attempts where the first resolves after the second, a
host that rejects one tool of N, and a reconnect; assertions cover registered
counts, abort signaling, and no stale success reporting.

**Acceptance Scenarios**:

1. **Given** no `modelContext` on `document` or `navigator`, **When**
   registration runs, **Then** it reports zero registrations without erroring
   and a later capability appearance can trigger a successful retry.
2. **Given** a slow catalogue fetch, **When** a newer registration attempt
   starts before the older one finishes, **Then** the older attempt's
   controller is aborted and it does not report success.
3. **Given** a host that rejects one tool, **When** registration runs,
   **Then** the remaining tools register and the failure is logged with the
   tool name, not silently swallowed and not fatal to the batch.

### User Story 4 - Project binding rejects stale writes (Priority: P2)

A bridge call carries the caller's believed-active project; mutation-tagged
calls whose selection no longer matches the backend's active project are
rejected with an explicit stale-context error.

**Why this priority**: ADR-055 section 4 makes this a correctness requirement:
"Opening another page must not silently redirect an in-flight write to another
project."

**Independent Test**: Open project A, obtain a catalogue (which snapshots the
active project), switch the backend to project B (as a second page would),
then issue a mutation-tagged call with A's snapshot: it is rejected with a
stale-project response; a read-tagged call's behavior follows the declared
read policy; re-fetching the catalogue and retrying succeeds.

**Acceptance Scenarios**:

1. **Given** project A active and a catalogue snapshot for A, **When** the
   backend switches to B and a mutation-tagged call arrives with A's snapshot,
   **Then** the call is rejected with a defined stale-context error and no
   mutation executes.
2. **Given** the same switch, **When** the caller re-fetches the catalogue and
   retries, **Then** the call dispatches normally against B.

### User Story 5 - Loopback session handling without breaking local flows (Priority: P2)

Bridge endpoints require proof of a legitimate session: a per-launch token the
backend injects into the page it serves. Desktop and ordinary local-browser
flows acquire it transparently; arbitrary cross-origin pages cannot call the
bridge.

**Why this priority**: ADR-055 section 7: "loopback deployment does not justify
arbitrary cross-origin calls." It is also the seam the lab identity backend
plugs into, so its shape must be right the first time.

**Independent Test**: A call to `/api/webmcp/call` without the token is
rejected; the served `index.html` bootstrap carries the token; a page using the
injected value calls successfully; CORS policy stays unchanged and restrictive.

**Acceptance Scenarios**:

1. **Given** a running backend, **When** a client calls a bridge endpoint
   without the session token, **Then** the response is an authentication
   rejection, not a dispatch.
2. **Given** the served page, **When** it uses the injected token, **Then**
   bridge calls succeed with no user interaction.

### User Story 6 - External-only tools are visible only where intended (Priority: P3)

A tool tagged for external audiences appears in the webmcp catalogue and is
absent from the local socket transport's `tools/list`; untagged tools keep
appearing in both.

**Why this priority**: It resolves the owner decision that local agents (with
native file/shell capability) must not pay for external-only tools, while
keeping one registry as ADR-055 section 4 requires.

**Independent Test**: Register a fixture tool with the external audience tag;
assert presence in `GET /api/webmcp/tools` and absence from the socket
transport's `tools/list` response; assert an untagged fixture appears in both.

**Acceptance Scenarios**:

1. **Given** an external-tagged fixture tool, **When** both catalogues are
   listed, **Then** webmcp includes it and the local transport excludes it.
2. **Given** an untagged existing tool, **When** both catalogues are listed,
   **Then** both include it.

### Edge Cases

- A call arriving while no project is open: read tools that need a project fail
  with the existing no-active-project error mapped through the adapter;
  project-free tools dispatch normally.
- Catalogue fetch racing a project switch: the catalogue's context snapshot
  makes the race detectable by the caller (US4), so no server-side locking is
  introduced.
- A host whose `registerTool` ignores the abort signal: registration results
  are still reported per-attempt and superseded attempts never overwrite newer
  state.
- Duplicate registration after a page reload without a host reset: the module-
  level controller aborts the previous attempt first (demo behavior preserved
  and extended).
- Token-bearing pages saved or shared (saved HTML, screenshots of bootstrap):
  the token authorizes only this local instance's bridge; rotation happens on
  backend restart. Broader exfiltration concerns are documented, not solved
  here; the lab backend replaces this mechanism.

## 3. Requirements

### Functional Requirements

- **FR-001**: `GET /api/webmcp/tools` MUST enumerate the shared FastMCP
  registry (`mcp.list_tools()`), returning `name`, `description`,
  `inputSchema`, and derived `category`/`mutation` per entry, plus a context
  snapshot identifying the currently active project (or its absence).
- **FR-002**: `POST /api/webmcp/call` MUST accept `{name, arguments}`, reject
  unknown tools with 404, dispatch via `mcp.call_tool`, and adapt the result
  through one shared adapter function reused from
  `src/scistudio/ai/agent/mcp/server.py` (`_serialise_result` promoted to an
  importable, documented contract).
- **FR-003**: The adapter contract MUST define and test: structured content
  (preserved in a declared field), supported non-text content blocks (declared
  per-type representation or an explicitly marked substitution), top-level
  error flag propagation, and thrown-exception mapping to `isError` content.
- **FR-004**: Tool visibility MUST be tag-driven: the audience tag (for
  example `audience:external`) is defined once in the MCP package; the webmcp
  catalogue includes such tools and the local socket transport's `tools/list`
  excludes them. No router-internal tool synthesis or if-chain dispatch.
- **FR-005**: Bridge calls MUST carry the caller's believed-active project
  identifier (acquired from the catalogue snapshot); the router MUST reject
  mutation-tagged calls whose identifier is stale with a defined error, and
  MUST define the read-call policy explicitly.
- **FR-006**: Bridge endpoints MUST sit behind one authentication middleware
  with a pluggable identity-backend interface; this spec ships the loopback
  token backend: a per-launch random token injected into the served page
  bootstrap (via the `adr-055-prefix-independence` SPA injection) and required
  as a header on every bridge call. The Hub OAuth backend is out of scope here
  but the seam MUST NOT require router changes to add it.
- **FR-007**: Bridge call logging MUST record tool name, outcome, and bounded
  identifiers only; full arguments, file contents, and command bodies MUST NOT
  be logged (aligns with the existing request-logging middleware convention).
- **FR-008**: All frontend bridge URLs MUST be built through the
  `adr-055-prefix-independence` base-path helpers; root-relative literals are
  forbidden in `frontend/src/webmcp/`.
- **FR-009**: The registration module MUST probe both
  `document.modelContext` and `navigator.modelContext`, abort superseded
  attempts, tolerate per-tool registration failure, and expose a retry path for
  late capability availability; an obsolete attempt MUST NOT report success.
- **FR-010**: Registration wiring from `frontend/src/main.tsx` MUST remain
  fire-and-forget and non-blocking for app boot.
- **FR-011**: The router MUST be mounted through the standard
  `app.include_router` sequence with prefix `/api/webmcp`; the HTTP route adds
  no business logic beyond dispatch, adaptation, binding checks, and logging.

### Key Entities

- **ToolCatalogueEntry**: `name`, `description`, `inputSchema`, `category`,
  `mutation`; derived from the FastMCP registry entry and its tags. No new
  persistence.
- **BridgeCallContext**: caller-presented project identifier plus the
  authenticated session identity (loopback token subject or, later, Hub user).
  Transient per request; no new persistence.

## 4. Implementation Plan

### 4.1 Technical Approach

Transplant the demo router shape into `src/scistudio/api/routes/webmcp.py`,
replacing its demo-specific parts: keep the two-route structure, the 404
unknown-tool behavior, and the exception-to-`isError` mapping; replace the text
-only serialization with the FR-003 adapter; remove the synthesized tools
(`about_scistudio`, `import_data`, `write_file`, `read_file`, `run_bash` move
to real registry tools per `adr-055-agent-context-workspace`; the orientation
tool is replaced by `get_agent_context` per ADR-055 section 9.1). Promote
`_serialise_result` in `server.py` to the shared adapter and extend it per
FR-003. Add the audience-tag filter at the socket transport's `tools/list`
handler and the inverse filter in the webmcp catalogue.

Frontend: transplant `frontend/src/webmcp/register.ts` and `types.ts`, strip
all presentation coupling (badge code is excluded per ADR-055 section 9.1),
route all URLs through the base-path helpers, add the superseded-attempt check
after the catalogue await, and keep the dual host probing.

Session substrate: middleware scoped to `/api/webmcp/*` (designed so the lab
spec later widens it) that delegates to an identity-backend interface; the
loopback backend compares a header token against the per-launch value the SPA
injection delivered. The middleware interface lives beside the router; the Hub
backend lands in `src/scistudio/deployment/jupyterhub/` per the owner's
three-block split.

Project binding: the catalogue snapshot carries the active project identifier;
the router compares presented vs actual for mutation-tagged tools (tag-derived,
same source as the catalogue's `mutation` flag).

### 4.2 Affected Files

| File | Action | Rationale |
|---|---|---|
| `src/scistudio/api/routes/webmcp.py` | create | The bridge router (transplanted + hardened) |
| `src/scistudio/ai/agent/mcp/server.py` | modify | Promote `_serialise_result`; audience-tag filtering in socket `tools/list` |
| `src/scistudio/ai/agent/mcp/__init__.py` | modify | Define the audience tag constant next to registration imports |
| `src/scistudio/api/app.py` | modify | Mount the router; wire the bridge session middleware |
| `src/scistudio/api/spa.py` | modify | Extend bootstrap injection with the loopback session token |
| `frontend/src/webmcp/register.ts` | create | Transplanted registration module, hardened per FR-009 |
| `frontend/src/webmcp/types.ts` | create | Ambient WebMCP host types (dual `document`/`navigator` probing) |
| `frontend/src/main.tsx` | modify | Fire-and-forget registration wiring |
| `frontend/src/lib/api/core.ts` | modify | Expose/authenticate bridge fetches (token header, base path) |
| `tests/api/test_webmcp.py` | create | Catalogue, dispatch, adapter contract, binding, session |
| `tests/ai/test_mcp_fastmcp.py` | modify | Audience-tag visibility filtering |
| `frontend/src/webmcp/register.test.ts` | create | Registration lifecycle matrix |

### 4.3 Implementation Sequence

1. **T-001** (foundation, US1/US2): promote the adapter in `server.py`, create
   the router with catalogue + dispatch, mount it, `tests/api/test_webmcp.py`
   core cases.
2. **T-002** (US6): audience tag constant + both-side filtering + tests.
3. **T-003** (US5): session middleware interface + loopback token backend +
   SPA bootstrap injection.
4. **T-004** (US4): catalogue context snapshot + stale-selection rejection.
5. **T-005** (US3): frontend registration module + `register.test.ts` matrix +
   `main.tsx` wiring.
6. **T-006** (cross-cutting): bounded logging policy (FR-007), prefix-helper
   sweep (FR-008), full verification per ADR-055 section 11 Bridge parity and
   Registration rows.

### 4.4 Verification Plan

- `tests/api/test_webmcp.py`: catalogue parity against `mcp.list_tools()`,
  unknown-tool 404, adapter mapping fixtures (structured, non-text, error
  flag, thrown exception), stale-project rejection, token required/accepted.
- `tests/ai/test_mcp_fastmcp.py`: audience filtering both directions.
- `frontend/src/webmcp/register.test.ts`: the US3 scenario matrix.
- Manual: load the app in a WebMCP-capable host and record host/version/
  capability evidence per ADR-055 section 11.
- `gate_record check` tier-selected checks for the diff.

### 4.5 Risks And Rollback

- Risk: WebMCP host APIs are trial-era and move (Chrome 149→150 already moved
  the surface). Mitigation: probing isolates host differences; tested behavior
  is recorded per host without changing the transport (ADR-055 section 11).
- Risk: the audience filter accidentally hides a tool local agents need.
  Mitigation: default visibility is both transports; filtering is opt-in per
  tool tag with tests pinning the direction.
- Risk: token-in-bootstrap is weak if page HTML leaks. Mitigation: loopback-
  only threat model documented; lab deployments replace the backend; rotation
  on restart; documented in spec, accepted by owner direction.
- Rollback: the router and registration module are additive; removing them
  restores current behavior. No schema or data migration.

## 5. Success Criteria

### Measurable Outcomes

- **SC-001**: 100% of registry tools visible to the instance appear in
  `GET /api/webmcp/tools` with schema-identical `inputSchema` (audience
  filtering applied), verified by an automated parity assertion.
- **SC-002**: All adapter contract fixtures (structured, non-text, error flag,
  exception) pass with zero lossy text round-trips for structured content.
- **SC-003**: The registration test matrix covers missing/late capability,
  repeated registration, superseded attempts, reconnect, and partial failure —
  all passing, with zero stale-success reports.
- **SC-004**: A stale-project mutation call is rejected in 100% of test
  attempts; zero mutations execute against a non-selected project.
- **SC-005**: Bridge logs contain tool names and outcomes and zero full
  argument bodies, verified by a log-scanning test.

## 6. Assumptions

- The host page is same-origin with the backend (the SPA served by it);
  cross-origin hosting of the SciStudio page is out of scope (source: ADR-055
  sections 2 and 7).
- The loopback token threat model is single-user local; multi-user identity is
  the lab deployment's Hub OAuth backend (source: owner session, 2026-09-05).
- FastMCP's tag surface on registered tools is stable enough to derive
  category/mutation/audience (source: existing-system, current tools already
  use `category:*` and `read`/`write` tags).
- The demo repository remains a read-only reference; nothing is committed to
  it (source: owner directive, 2026-09-05).
