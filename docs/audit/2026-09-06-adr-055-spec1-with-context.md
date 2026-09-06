---
title: "Audit — ADR-055 Spec 1 WebMCP bridge (with-context)"
status: Approved
owners:
  - "@jiazhenz026"
related_adrs:
  - 55
  - 42
related_specs:
  - adr-055-webmcp-bridge
  - adr-055-prefix-independence
language_source: en
---

# Audit — ADR-055 Spec 1 WebMCP Bridge (with-context)

Audit mode: **with-context** (agent AU2, `audit_reviewer` persona, task kind
`maintenance`).
Subject: PR #2275, `feat/2271-webmcp-bridge` @ `12096c0ab`, stacked on PR
#2274 (`feat/2270-prefix-independence`). Audited delta:
`git diff origin/feat/2270-prefix-independence...HEAD` (merge-base
`a707e138b`, 16 files).
Issue: #2271. Umbrella PR: #2273 `[DO NOT MERGE]`.
Gate ledger under audit: `.workflow/records/2271-feat-2271-webmcp-bridge.json`.
Audit gate ledger: `.workflow/records/2271-audit-2271-spec1-with-context.json`.

**Verdict: pass-with-fixes.** Every claimed Spec 1 behavior in the delta
verifies against the spec, the code, the tests, and the gate ledger. The one
P1 is not in the Spec 1 delta: CI on the PR head fails two Spec 0 CLI-help
tests whose first fix attempt did not survive the Linux renderer; a second fix
exists on the Spec 0 branch and must flow into this branch before merge.

## 1. Findings

### P1 — blocks merge

#### P1-1. CI fails on the PR head: two Spec 0 CLI-help tests still fail on the Linux runner

`gh pr view 2275 --json statusCheckRollup` at head `12096c0ab` shows
**Test (Python 3.11)** and **Test (Python 3.13)** FAILED (CI run
`34029787779`, `head_sha` confirmed `12096c0abdebc0bcf62185282516a54fd375c45a`).
Both failures are the same assertion:

```
FAILED tests/api/test_root_path.py::TestServeRootPath::test_serve_help_documents_host_and_root_path
FAILED tests/api/test_root_path.py::TestGuiRootPath::test_gui_help_documents_host_and_root_path
AssertionError: assert '--host' in '\x1b[1m ...╯\x1b[0m\n\n'
```

Facts established during the audit:

- `tests/api/test_root_path.py` is **Spec 0** (#2270 / PR #2274) surface, not
  part of the Spec 1 implementation claims; the Spec 1 delta touches it only
  for the manager-authorized `test_empty_prefix_is_a_noop` rescope (verified
  below).
- The head already contains the first fix attempt (`720ab9ec1`, pinning
  `COLUMNS=200` via `CliRunner.invoke(env=...)`), merged up as `737438424`.
  The pin makes the tests pass locally on Windows (reproduced by this audit:
  both tests pass at `12096c0ab` with `--no-cov`; the rendered help contains
  `--host` at widths 80 and 200) but does **not** fix the Linux CI renderer,
  which still omits the literal option strings.
- The Spec 0 branch has since replaced the approach: `12e58baf1`
  ("introspect CLI options instead of parsing rendered Rich help output",
  head `dd1affc79`) asserts against resolved click parameters, which is
  environment-proof by construction. Its CI was still pending at audit time.

Repair path: land green CI for #2274 with `12e58baf1`, merge
`feat/2270-prefix-independence` into `feat/2271-webmcp-bridge` again, and let
CI re-run on #2275. No Spec 1 code change is implicated.

### P2 — fix or explicitly track before completion

#### P2-1. The spec's manual host-verification row has no recorded evidence

Spec §4.4 (verification plan) lists: "Manual: load the app in a
WebMCP-capable host and record host/version/capability evidence per ADR-055
section 11." ADR-055 §11 marks Bridge parity and Registration as "required
evidence before implementation completion". The automated halves exist and
pass (catalogue parity, adapter contract, registration matrix — see §2), but
no manual host evidence is recorded in the PR body, PR comments, or the gate
ledger, and no tracked TODO/issue cites the deferral. If the manager intends
the umbrella e2e phase to cover this, that deferral needs a tracked reference
per AGENTS.md §3.6; otherwise the evidence must be produced.

### P3 — follow-up

#### P3-1. Spec 0 FR-002 text is now partially superseded and not annotated

`docs/specs/adr-055-prefix-independence.md` FR-002: "With the default empty
prefix, the application MUST behave exactly as today: no changed routes,
headers, redirects, or payload shapes." Spec 1 FR-006 deliberately changes
the served `index.html` payload on every mount (the
`window.__SCISTUDIO_WEBMCP_TOKEN__` bootstrap), and the Spec 1 delta rescoped
`test_empty_prefix_is_a_noop` accordingly. The code docstring
(`src/scistudio/api/spa.py`), the test docstring, and the ledger's
manager-authorized scope amendment all document the interaction, but the
Spec 0 spec text itself carries no pointer. A one-line cross-reference in the
Spec 0 spec (or an addendum note in ADR-055) would close the drift.

## 2. What was verified and found sound

### 2.1 Claims verified against code, tests, and spec

- **Shared adapter (FR-002/FR-003).**
  `src/scistudio/ai/agent/mcp/server.py`: `_serialise_result` promoted to
  `serialise_result`; `adapt_tool_result` preserves `structured_content`
  verbatim in `structuredContent` (no text round-trip), substitutes non-text
  blocks with an explicitly marked text block carrying `substitutedFrom`,
  propagates `isError`/`is_error` to the top level, and wraps primitive
  results in a single text block. Thrown exceptions never reach the adapter:
  the router maps them to `isError` content with a 200, not a 5xx
  (`webmcp.py:262-277`). Tests:
  `tests/api/test_webmcp.py::test_adapter_preserves_structured_content`,
  `test_adapter_substitutes_non_text_blocks_with_marker`,
  `test_adapter_propagates_top_level_error_flag`,
  `test_adapter_wraps_primitive_result`,
  `test_thrown_exception_maps_to_iserror_not_5xx`. No stale references to the
  old private name outside the demo clone and the promotion docstring.
- **Router (FR-001/FR-011).** `GET /api/webmcp/tools` enumerates
  `mcp.list_tools()` with `name`/`description`/`inputSchema` plus
  tag-derived `category`/`mutation` from the single shared derivation point
  `tool_category_and_mutation`, and carries the `context.projectId` snapshot.
  Unknown tool → 404 with an unknown-tool detail. Mounted via the standard
  `app.include_router(router, prefix=ROUTE_PREFIX)` sequence in
  `src/scistudio/api/app.py`. No business logic beyond dispatch, adaptation,
  binding checks, and logging; **no router-internal tool synthesis** — the
  demo's synthesized `get_started` and if-chain dispatch are absent.
- **Audience tags (FR-004).** `AUDIENCE_EXTERNAL_TAG` defined once in
  `server.py`, re-exported from `scistudio.ai.agent.mcp`; the socket
  transport's `tools/list` skips external-tagged tools
  (`server.py` `MCPServer` dispatch), the webmcp catalogue includes them.
  `tests/ai/test_mcp_fastmcp.py` adds three tests pinning both directions
  plus the single-definition invariant. Verified locally: 18 passed.
- **Session middleware (FR-006).** One `WebMCPSessionMiddleware` (pure ASGI,
  no body buffering) scoped to `/api/webmcp/*` with root-path stripping;
  pluggable `BridgeIdentityBackend` Protocol; `LoopbackTokenBackend` compares
  the `X-SciStudio-WebMCP-Token` header with `secrets.compare_digest` against
  a per-launch `secrets.token_urlsafe(32)` token stored on `app.state` and
  injected into the served page bootstrap by `SPAStaticFiles`
  (`src/scistudio/api/spa.py`, emitted even at the default root mount). The
  middleware is added **before** `CORSMiddleware`, so CORS is outermost and
  preflight never reaches the session check — pinned by
  `test_cors_preflight_still_handled_by_cors_layer`. Non-bridge routes
  untouched; missing/wrong token → 401.
- **Project binding (FR-005).** Mutation-tagged (`write`) calls whose
  presented `projectId` differs from the active project are rejected with
  409 `stale_project_context` carrying `presentedProjectId`/
  `activeProjectId`; the read policy is declared explicitly in the module
  docstring (reads dispatch without the staleness check) and pinned by
  `test_read_calls_follow_declared_policy`.
  `test_stale_project_mutation_rejected_then_refetch_succeeds` proves the
  stale call does not execute and that re-fetch + retry succeeds (SC-004).
- **Bounded logging (FR-007/SC-005).** Log lines carry tool name, outcome,
  and bounded identifiers; the exception path logs the exception **type**
  only. `test_logs_never_contain_argument_bodies` drives a secret argument
  body through both the success and stale-rejection paths and asserts it
  never appears in rendered logs.
- **Frontend (FR-008/009/010).** `frontend/src/webmcp/register.ts` probes
  both `document.modelContext` and `navigator.modelContext`; a new attempt
  aborts the previous controller; superseded attempts are detected both
  after the catalogue await and mid-batch and never report success; per-tool
  registration refusal is logged with the tool name and is not fatal;
  `registerSciStudioToolsWithRetry` covers late capability with a bounded
  budget; `main.tsx` wiring is fire-and-forget (`void
  registerSciStudioToolsWithRetry()`); all bridge URLs go through `apiFetch`
  (the Spec 0 base-path helper) — no raw `fetch`/`WebSocket`/`EventSource`
  in `frontend/src/webmcp/`. The execute callback sends the token header and
  the catalogue's project snapshot.
- **Tests exist and pass.** `tests/api/test_webmcp.py` — 17 tests, all pass
  locally (`--no-cov`). `tests/ai/test_mcp_fastmcp.py` — 18 pass locally.
  `frontend/src/webmcp/register.test.ts` — 8 tests; the CI Frontend job at
  the PR head ran them green ("src/webmcp/register.test.ts (8 tests)", full
  suite 2197 passed). Local vitest was not re-run (no `node_modules` in the
  audit worktree); CI evidence is authoritative and covers it.
- **Governs moves are minimal.** The delta moves exactly the shipped files
  (`src/scistudio/api/routes/webmcp.py`, `frontend/src/webmcp/**`, and the
  `scistudio.api.routes.webmcp` module) from `planned_governs` to `governs`
  in `docs/adr/ADR-055.md` and the spec frontmatter — nothing else.
- **`test_empty_prefix_is_a_noop` rescope preserves Spec 0 FR-002 intent.**
  The test still asserts no `__SCISTUDIO_BASE_PATH__` injection and
  unprefixed API answers at the root with the default empty prefix; only the
  byte-identity assertion gave way to the FR-006 token bootstrap, with the
  manager-authorized rescope recorded in the ledger (scope amendment
  2026-09-06T10:40:01Z) and in the test docstring.
- **Demo transplant fidelity (read-only comparison against
  `.scratch-design/webmcp-recovery/scistudio-web-demo @ 952f697b`).** The
  two-route shape, unknown-tool 404, and exception→`isError` mapping are
  preserved; the demo's lossy JSON-text serialization, synthesized tools,
  logged exception messages, missing session/auth, and missing superseded-
  attempt check are exactly the hardened deltas. The demo clone was only
  read (`git show`); nothing was modified, committed, or pushed there.

### 2.2 ADR-055 §9.2 robustness findings — disposition

| §9.2 finding | Treatment in this PR | Verified |
|---|---|---|
| Result adaptation is text-oriented | FR-003 adapter contract + fixtures | yes (tests) |
| Shell execution blocks the async route | Synthesized shell tool not transplanted; real execution tools are Spec 2 scope | yes (router has no shell path) |
| Output caps applied after collection | `_read_file` not transplanted; workspace/transfer contracts are Spec 2 scope | yes (absent) |
| Shell calls can lack a project | No shell in the bridge; FR-005 binding rejects stale/no-context mutations | yes |
| Cancellation partly represented | Browser abort forwarded via `AbortSignal` to `registerTool` and `apiFetch`; job cancellation is Spec 2 scope | yes |
| Demo-specific session/deployment assumptions | FR-006 middleware + loopback token + FR-008 prefix-aware URLs | yes (tests) |
| Registration lifecycle needs tests | `register.test.ts` 8-test matrix (missing/late capability, superseded, partial failure, reconnect, dual probe) | yes (CI) |
| Narrow context/data assumptions | Orientation/import tools deferred to `adr-055-agent-context-workspace` per spec §1 decision 1 | yes (out of scope) |
| Logging includes complete tool arguments | FR-007 bounded logging + log-scanning test | yes (tests) |

### 2.3 Gate ledger consistency (ADR-042 Addendum 6)

- `observed_diff` lists exactly the 16 files of the real delta.
- Declared scope covers the implementation surface; the three cross-spec
  additions (ADR-055.md, spec frontmatter, `tests/api/test_root_path.py`)
  are backed by recorded `add-include` scope events citing manager
  authorization, and the directive event records the plan.
- Final reconcile event (2026-09-06T11:16:38Z, mode `pre-pr`, tier 1):
  `result: pass`, `unsatisfied: []`; latest check events for
  `python_tests`, `frontend`, `full_audit`, `lint_format`, `type_check`,
  `architecture_tests`, `import_contracts`, `commit_hygiene`,
  `deferral_discipline`, `format_check` are all `pass`. Earlier failed
  check events in the ledger show the honest fix-and-rerun history, not a
  bypass.
- `verified_in_diff: null` on docs/test events matches the current CLI's
  behavior on the Spec 0 ledger too — not a per-PR gap.
- PR #2275 body names the gate record path and closes #2271 with a closing
  keyword; the ledger records PR provenance (`pull_request.number: 2275`,
  `closes: [2271]`). Every commit on the branch carries the required
  trailers (Gate-Record, Task-Kind, Issue, Assisted-by).
- Ledger events contain no absolute local paths or raw transcripts
  (`raw_log_ref` points to ignored `.workflow/local/**`).
- Local gate evidence and the CI failure do not contradict: the local
  `python_tests` check ran diff-scoped on Windows where the width-dependent
  assertion passes; the failure is Linux-renderer-specific (P1-1).
- Sentrux MCP: unavailable in this runtime (recorded N/A per dispatch); the
  ledger's `sentrux_gate` guard events are the free-tier advisory.

## 3. CI status

PR #2275 @ `12096c0ab`: all checks SUCCESS except **Test (Python 3.11)** and
**Test (Python 3.13)** — FAILURE (P1-1). `Verify Workflow Compliance`, Full
Audit, Lint & Format, Type Check, Architecture Tests, Import Contracts,
Frontend, Desktop, Wheel Release Smoke, Deferral Discipline, and CodeQL all
pass.

## 4. Recommendation

**pass-with-fixes.** The Spec 1 implementation is sound and fully verified;
do not merge #2275 until P1-1 is resolved by flowing the Spec 0 introspection
fix (`12e58baf1`) into this branch and CI is green, and P2-1 is either
evidenced or tracked with an owner-visible reference. P3-1 is a one-line doc
follow-up.
