---
spec_id: adr-055-prefix-independence
title: "ADR-055 Spec 0 — Prefix Independence For UI, API, WebSocket, And Worker Callback URLs"
status: Draft
feature_branch: docs/2263-adr-055-specs
created: 2026-09-05
input: "Owner-directed live session: author the ADR-055 implementation spec set under umbrella issue #2263. The owner directed that prefix independence is Spec 0 and lands first: it is not a server feature but the core's implicit 'I am mounted at the root path' assumption; moving it into the deployment layer would force the deployment layer to reach back into frontend and CLI URL construction, which is dirtier. It has zero JupyterHub dependency and is independently acceptable. Known surface (owner-cited, verified during authoring): root-relative fetch('/api/...') call sites including frontend/src/lib/api/core.ts, WebSocket URLs built from window.location in useWebSocket.ts and usePtyWebSocket.ts, cli/main.py serve host binding, api/routes/workflows.py deriving SCISTUDIO_ENGINE_API_URL from request.base_url, and no FastAPI root_path support."
owners:
  - "@jiazhenz026"
related_adrs:
  - 55
related_specs:
  - adr-055-webmcp-bridge
  - adr-055-lab-deployment
scope:
  in:
    - A single backend-configured mount prefix (root path) applied to the FastAPI app so the UI, /api routes, /ws, and asset serving all resolve correctly when the service is mounted below a non-root path such as a JupyterHub user route.
    - "A single frontend base-path source of truth: every API fetch, WebSocket URL, asset URL, and same-origin check is built from it; root-relative hardcoded call sites are migrated."
    - Runtime delivery of the prefix to the already-built SPA (no per-deployment rebuild).
    - Correct worker callback URL (SCISTUDIO_ENGINE_API_URL) when the app is mounted under a prefix.
    - CLI flags and environment variables for host binding and mount prefix on the serve and gui entry points.
    - Regression coverage proving the default empty prefix preserves current desktop and local-browser behavior exactly.
  out:
    - JupyterHub, authentication, or any Hub-specific behavior (adr-055-lab-deployment). This spec's prefix plumbing must not import or reference Hub concepts.
    - The WebMCP bridge routes (adr-055-webmcp-bridge); this spec delivers the prefix plumbing they consume.
    - Per-user routing, spawning, or any control plane (adr-055-lab-deployment).
    - Desktop launcher mode selection and background runtime (adr-055-local-background-runtime).
governs:
  modules:
    - scistudio.api.app
    - scistudio.api.spa
    - scistudio.cli.main
  contracts: []
  entry_points: []
  files:
    - docs/specs/adr-055-prefix-independence.md
    - src/scistudio/api/app.py
    - src/scistudio/api/spa.py
    - src/scistudio/api/routes/workflows.py
    - src/scistudio/cli/main.py
    - frontend/src/lib/api/core.ts
    - frontend/src/hooks/useWebSocket.ts
    - frontend/src/components/AIChat/hooks/usePtyWebSocket.ts
    - frontend/src/components/AIChat/SetupScreen.tsx
    - frontend/src/components/CodeEditor.parts/useLintMarkers.ts
    - frontend/src/components/LearningCenter.parts/ProviderIntro.tsx
    - frontend/src/lib/logger.ts
    - frontend/src/App.parts/InteractiveModals.parts/panelModuleLoader.ts
    - frontend/src/components/DataPreview.parts/dynamicPreviewer.ts
    - frontend/src/lib/api/base-path.ts
  excludes: []
planned_governs:
  modules: []
  contracts: []
  entry_points: []
  files: []
  excludes: []
tests:
  - tests/api/test_root_path.py
  - frontend/src/lib/api/base-path.test.ts
acceptance_source: adr
language_source: en
---

# ADR-055 Spec 0 — Prefix Independence For UI, API, WebSocket, And Worker Callback URLs

## 1. Change Summary

This spec comes from ADR-055 (sections 4 and 8) and umbrella issue #2263.

Today the SciStudio core assumes it is always mounted at the root path of
whatever serves it. The frontend calls root-relative URLs (`fetch("/api/...")`,
`${proto}//${window.location.host}/ws`), the backend has no `root_path`
support, the CLI binds and advertises root URLs, and the engine callback URL is
derived from `request.base_url` without any prefix awareness. ADR-055 section 8
mounts each user's SciStudio under a per-user Hub route such as
`/user/<name>/scistudio/`, and section 4 requires the WebMCP bridge URLs to
resolve under that prefix. None of that works until the core stops assuming a
root mount.

The owner directed that this work is **Spec 0 and lands before every other
ADR-055 spec**: it is not a deployment feature but the removal of a core
implicit assumption, it must stay in the core tree, and it carries zero
JupyterHub dependency so it can be implemented and accepted entirely on its
own. Every later spec (bridge, tools, local runtime, lab deployment) builds on
the URL contract defined here.

The change: one backend-configured mount prefix, one frontend base-path source
of truth delivered to the built SPA at runtime, and a worker callback URL that
respects the configured prefix. Default configuration (empty prefix) must be a
pure no-op for desktop and local use.

## 2. User Scenarios & Testing

### User Story 1 - The backend serves correctly under a non-root mount (Priority: P1)

An operator (or later, a Hub spawner) starts the SciStudio backend with a mount
prefix, for example `scistudio serve --root-path /user/alice/scistudio`. The
FastAPI app serves its UI, every `/api` route, and the `/ws` WebSocket so that
a reverse proxy forwarding `/user/alice/scistudio/` to the backend works
end-to-end without rewriting response bodies.

**Why this priority**: Every other ADR-055 surface (bridge catalogue, workspace
transfer, lab deployment) breaks in its prefixed form until the app itself is
prefix-correct. This is the foundation story.

**Independent Test**: Start the backend with a non-empty root path, put a plain
prefix-terminating proxy (or direct `TestClient` with `root_path` set) in front
of it, and verify the SPA HTML, a representative API GET, and a WebSocket
handshake all succeed through the prefix while the same requests at the root
path behave as documented (redirect or 404 per the chosen contract).

**Acceptance Scenarios**:

1. **Given** the backend started with root path `/user/alice/scistudio`,
   **When** a client requests `/user/alice/scistudio/` and
   `/user/alice/scistudio/api/version`, **Then** both return the same content
   as their root-mounted equivalents and no response contains an unprefixed
   root-relative link that breaks under the proxy.
2. **Given** the same backend, **When** a client opens a WebSocket to
   `/user/alice/scistudio/ws`, **Then** the handshake succeeds and events flow.
3. **Given** the backend started with the default empty prefix, **When** the
   existing desktop and local-browser flows run, **Then** behavior is byte-for-
   byte unchanged (no prefix segments, no redirects added).

### User Story 2 - The frontend builds every URL from one base path (Priority: P2)

A developer adding any new API call, WebSocket, or asset reference cannot
accidentally hardcode a root-relative URL: all construction goes through one
base-path module, and the built SPA learns the prefix at runtime without a
per-deployment rebuild.

**Why this priority**: The backend half of P1 is only observable end-to-end
once the frontend stops issuing root-relative requests; one missed
`fetch("/api/...")` silently breaks one feature under a prefix.

**Independent Test**: Serve the built SPA under a non-root prefix (any static
mount plus the backend from US1), load the app, and drive the surfaces that
issue API calls, open WebSockets, and load panel/previewer modules; every
request the page issues carries the prefix. A lint-level or unit-level guard
fails on new root-relative API literals.

**Acceptance Scenarios**:

1. **Given** the SPA served under `/user/alice/scistudio/`, **When** the app
   boots and calls `apiFetch`, **Then** requests go to
   `/user/alice/scistudio/api/...`.
2. **Given** the same deployment, **When** `useWebSocket` and the PTY WebSocket
   hook connect, **Then** both dial `wss://<host>/user/alice/scistudio/ws`-style
   URLs including the prefix.
3. **Given** the same-origin validators in the panel module loader and dynamic
   previewer, **When** they resolve module URLs, **Then** resolution stays
   correct under the prefix and still rejects cross-origin URLs.

### User Story 3 - Workers reach the API through the prefix (Priority: P3)

A workflow run executed by a worker subprocess can call back into the API when
the backend is mounted under a prefix, because `SCISTUDIO_ENGINE_API_URL`
reflects the configured external base instead of the unprefixed
`request.base_url` (current behavior at
`src/scistudio/api/routes/workflows.py:106`).

**Why this priority**: Workflow execution is the product's core; a prefix
deployment where runs cannot call back is broken in a non-obvious way.

**Independent Test**: With a prefixed backend, trigger a workflow run and
assert the environment the worker receives contains the prefixed API URL and
that a worker-side API call succeeds.

**Acceptance Scenarios**:

1. **Given** a backend started with root path `/prefix`, **When** a workflow
   run is launched, **Then** the worker environment's API URL ends with
   `/prefix/` and worker-to-API calls succeed.

### User Story 4 - CLI entry points expose binding and prefix explicitly (Priority: P4)

An operator can control host binding and mount prefix from the CLI and
environment in one documented way, instead of the current fixed
`serve(host="0.0.0.0")` default (`src/scistudio/cli/main.py`) and implicit root
mount.

**Why this priority**: It is the configuration surface the lab deployment spec
consumes; keeping it explicit prevents per-caller reimplementation.

**Independent Test**: `scistudio serve --help` and `scistudio gui --help` show
the new options; starting with each combination (default, custom host, custom
prefix) binds and advertises accordingly.

**Acceptance Scenarios**:

1. **Given** `scistudio serve` with no new flags, **When** the server starts,
   **Then** binding and advertised URLs match today's behavior.
2. **Given** `--root-path /p` (or its environment variable), **When** the
   server starts, **Then** the ready output and the served app use `/p`.

### Edge Cases

- Trailing-slash and double-slash normalization: `/prefix`, `/prefix/`, and
  `/prefix//api/...` must not produce divergent behavior; the spec requires one
  normalization point per side.
- Empty prefix must be a strict no-op, including no extra redirects and no
  changed `Location` headers.
- Development mode with the Vite dev server (`SCISTUDIO_DESKTOP_FRONTEND_URL`)
  keeps working; the dev proxy must forward prefixed and unprefixed forms
  consistently with the chosen contract.
- Electron `loadURL` and the remembered-port flow (`desktop/runtime-port.js`)
  are unaffected because the desktop path always uses the empty prefix.
- Requests arriving at the unprefixed root while a prefix is configured: the
  implementation must pick one behavior (redirect to the prefix or 404) and
  document it; silently serving at both is not allowed.

## 3. Requirements

### Functional Requirements

- **FR-001**: The FastAPI application MUST accept a single configured mount
  prefix (environment variable `SCISTUDIO_ROOT_PATH` and CLI `--root-path`),
  applied via FastAPI/uvicorn `root_path` so request routing, URL generation,
  and redirects are prefix-aware.
- **FR-002**: With the default empty prefix, the application MUST behave
  exactly as today: no changed routes, headers, redirects, or payload shapes.
- **FR-003**: The built SPA MUST learn the prefix at runtime from the serving
  backend (templated injection into the served `index.html` by the existing SPA
  static handler, or an equivalent bootstrap mechanism); a per-deployment
  frontend rebuild is forbidden.
- **FR-004**: All frontend API calls MUST be constructed through
  `frontend/src/lib/api/core.ts` (`apiFetch`) or the new base-path module; the
  known direct `fetch("/api/...")` call sites (SetupScreen, useLintMarkers,
  ProviderIntro, logger diagnostics/native-dialog/client-logs) MUST be
  migrated, and implementation MUST include a sweep for any remaining
  root-relative API or asset literals.
- **FR-005**: WebSocket URLs (`useWebSocket`, `usePtyWebSocket`) and the
  same-origin checks in the panel module loader and dynamic previewer MUST be
  built from the same base-path source of truth.
- **FR-006**: `SCISTUDIO_ENGINE_API_URL` for worker subprocesses MUST be
  derived from the configured external base URL (including prefix), not from
  the incoming `request.base_url` of whichever request happened to trigger the
  run.
- **FR-007**: `scistudio serve` and `scistudio gui` MUST expose host binding
  and root path through flags and environment variables; current defaults stay
  unchanged except where the lab-deployment spec later overrides them for its
  own mode.
- **FR-008**: URL normalization (joining base path and route) MUST exist
  exactly once per side (one Python helper, one TypeScript helper); string
  concatenation of prefixes at call sites is forbidden.
- **FR-009**: The spec's regression gate MUST include a prefixed-mode test
  proving no response body or header contains an unprefixed absolute path that
  the proxy did not rewrite, for the SPA shell and a representative API/WS set.

## 4. Implementation Plan

### 4.1 Technical Approach

Backend: read the configured prefix in `create_app` and pass it as FastAPI
`root_path`; keep all routers mounted exactly as today (the prefix is
structural, not a router change). The SPA static handler
(`src/scistudio/api/spa.py`) templates a single bootstrap assignment (for
example `window.__SCISTUDIO_BASE_PATH__`) into the served `index.html`; asset
files themselves stay byte-identical so caching and OTA packaging are
unaffected. Worker callback URL: compute the engine API base from configuration
(app root path + external base) instead of `request.base_url`; keep a
documented fallback for the unprefixed local case.

Frontend: add `frontend/src/lib/api/base-path.ts` exposing the runtime prefix
(read from the injected global, default `""`) plus `apiUrl(path)` and
`wsUrl(path)` helpers with single-point normalization. `apiFetch` routes
through `apiUrl`; the two WebSocket hooks and the two same-origin validators
route through the shared helper; direct `fetch("/api/...")` call sites migrate
to `apiFetch`. A unit test plus a cheap lint/e2e guard prevents new
root-relative API literals.

Dependency note: this spec deliberately knows nothing about JupyterHub,
authentication, or per-user routing; the lab-deployment spec consumes the
`--root-path` contract.

### 4.2 Affected Files

| File | Action | Rationale |
|---|---|---|
| `src/scistudio/api/app.py` | modify | Accept and apply `root_path`; expose configured prefix to SPA serving |
| `src/scistudio/api/spa.py` | modify | Template the bootstrap prefix into served `index.html` |
| `src/scistudio/api/routes/workflows.py` | modify | Engine callback URL from configuration, not `request.base_url` |
| `src/scistudio/cli/main.py` | modify | `--root-path` / host flags and env wiring for `serve` and `gui` |
| `frontend/src/lib/api/base-path.ts` | create | Single base-path source of truth and URL helpers |
| `frontend/src/lib/api/core.ts` | modify | `apiFetch` routes through the base-path helper |
| `frontend/src/hooks/useWebSocket.ts` | modify | WebSocket URL from helper |
| `frontend/src/components/AIChat/hooks/usePtyWebSocket.ts` | modify | WebSocket URL from helper |
| `frontend/src/App.parts/InteractiveModals.parts/panelModuleLoader.ts` | modify | Same-origin check prefix-aware |
| `frontend/src/components/DataPreview.parts/dynamicPreviewer.ts` | modify | Same-origin check prefix-aware |
| `frontend/src/components/AIChat/SetupScreen.tsx`, `frontend/src/components/CodeEditor.parts/useLintMarkers.ts`, `frontend/src/components/LearningCenter.parts/ProviderIntro.tsx`, `frontend/src/lib/logger.ts` | modify | Migrate direct root-relative `fetch` call sites to `apiFetch` |
| `tests/api/test_root_path.py` | create | Prefixed serving, redirects, worker callback URL |
| `frontend/src/lib/api/base-path.test.ts` | create | URL helper normalization and guard tests |

### 4.3 Implementation Sequence

1. **T-001** (foundation, US1): backend `root_path` plumbing + SPA bootstrap
   injection + `tests/api/test_root_path.py` skeleton.
2. **T-002** (US2): `base-path.ts` + `apiFetch` migration + WS hooks +
   same-origin validators + unit tests.
3. **T-003** (US2): migrate the enumerated direct `fetch` call sites; run the
   root-relative literal sweep; add the guard test.
4. **T-004** (US3): engine callback URL change + worker test.
5. **T-005** (US4): CLI flags/env wiring + help/regression tests.
6. **T-006** (cross-cutting): default-empty-prefix regression pass (desktop
   boot smoke + existing test suites) and FR-009 prefixed response scan.

### 4.4 Verification Plan

- `tests/api/test_root_path.py`: prefixed and unprefixed serving, SPA shell,
  API, WS handshake, callback URL derivation.
- `frontend/src/lib/api/base-path.test.ts`: helper normalization; a guard that
  scans `frontend/src` for new root-relative API literals outside the helper.
- Existing API and frontend suites must pass unchanged under the default empty
  prefix (the no-op proof).
- Manual: desktop install smoke (Electron `loadURL` flow) and a local
  `serve --root-path /p` behind a trivial prefix proxy.
- `gate_record check` tier-selected checks for the diff.

### 4.5 Risks And Rollback

- Risk: a missed root-relative call site breaks one feature only in prefixed
  deployments. Mitigation: the FR-004 sweep plus the FR-009 response scan and
  the guard test.
- Risk: `root_path` interacts with absolute redirects from Starlette
  (`FileResponse`, OAuth later). Mitigation: FR-001 requires prefix-aware URL
  generation; tests cover redirect `Location` headers.
- Risk: templating `index.html` complicates caching. Mitigation: only the HTML
  document is templated; hashed assets stay static.
- Rollback: the default empty prefix is a no-op, so reverting is removing the
  configuration path; no data or schema migration exists.

## 5. Success Criteria

### Measurable Outcomes

- **SC-001**: With `--root-path /user/demo/scistudio`, 100% of requests issued
  by the SPA during a scripted boot-and-browse (API, WS, panel module loads)
  carry the prefix; zero root-relative API requests observed.
- **SC-002**: All pre-existing backend and frontend test suites pass with the
  default empty prefix with no test modifications.
- **SC-003**: A workflow run under a prefixed backend completes with the worker
  receiving a callback URL that ends with the configured prefix.
- **SC-004**: `frontend/src` contains zero root-relative `"/api/` or
  `"/ws"` literals outside `base-path.ts` and its tests after the sweep.

## 6. Assumptions

- The deployment mounts SciStudio under a path prefix on the same origin, not
  under a separate subdomain (source: ADR-055 section 8, Hub user routes).
- The SPA is always served by the backend in prefixed deployments; the Vite dev
  server flow is unprefixed development only (source: existing-system,
  `desktop/main.js` dev URL override).
- The proxy in front strips nothing and rewrites nothing; it forwards the
  prefix verbatim (source: ADR-055 section 8, "service prefixes ... must be
  verified").
- Desktop and local modes always use the empty prefix; no per-mode prefix is
  required (source: owner session, 2026-09-05).
