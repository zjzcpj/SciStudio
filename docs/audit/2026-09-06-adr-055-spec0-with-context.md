---
title: "Audit — ADR-055 Spec 0 prefix independence (with-context)"
status: Approved
owners:
  - "@jiazhenz026"
related_adrs:
  - 55
  - 42
related_specs:
  - adr-055-prefix-independence
language_source: en
---

# Audit — ADR-055 Spec 0 prefix independence (with-context)

Audit mode: **with-context** (agent AU1, `audit_reviewer` persona).
Subject: PR #2274, `feat/2270-prefix-independence` @ `a707e138b`, closing
#2270 against spec `docs/specs/adr-055-prefix-independence.md`
(FR-001..FR-009, SC-001..SC-004) and ADR-055 §4/§8/§11.
Audit branch: `audit/2270-spec0-with-context` @ worktree
`.worktrees/audit-2270-spec0`.
Gate ledger (this audit):
`.workflow/records/2270-audit-2270-spec0-with-context.json`.
Implementer ledger audited:
`.workflow/records/2270-feat-2270-prefix-independence.json`.
Sentrux MCP: unavailable in this runtime — N/A per dispatch.

**Verdict: block.** One P1: CI is red at the PR head — the claimed Rich
help-width fix (`COLUMNS=200` pin, `720ab9ec1`) does not fix the failing
tests, because the failure was misdiagnosed. The implementation itself is
sound; every other claim verified, including a live uvicorn smoke of the
prefixed contract. The fix is small and test-only (see P1-1).

## 1. Findings

### P1 — blocks merge

#### P1-1. CI red at `a707e138b`: both Python test jobs fail the two CLI `--help` tests; the `COLUMNS=200` "fix" targets the wrong root cause

CI run 34029442091 (head `a707e138bc1d6e8118790eaaa4c3d2fd536d3456`, i.e.
*after* the claimed fix landed) fails identically on `Test (Python 3.11)` and
`Test (Python 3.13)`:

```
FAILED tests/api/test_root_path.py::TestServeRootPath::test_serve_help_documents_host_and_root_path
FAILED tests/api/test_root_path.py::TestGuiRootPath::test_gui_help_documents_host_and_root_path
AssertionError: assert '--host' in '\x1b[1m        ...'
2 failed, 7406 passed, 84 skipped, 8 xfailed
```

The implementer's diagnosis ("Rich truncates option names, e.g. `--ho…`, at
narrow CI width") is wrong, and the `COLUMNS=200` pin therefore changes
nothing in CI. The actual failure mode is ANSI styling, not width:

- CI installs fresh deps (`uv pip install --system -e ".[dev]"`), resolving
  **click 8.5.0 / typer 0.27.2**, while the local venv used for verification
  has click 8.4.1 / typer 0.25.1.
- typer 0.27.2's `rich_utils` computes
  `FORCE_TERMINAL = True if getenv("GITHUB_ACTIONS") or getenv("FORCE_COLOR") or getenv("PY_COLORS") else None`.
  On GitHub Actions (`GITHUB_ACTIONS=true`) the Rich console renders with
  color enabled; locally (non-tty, no those vars) color is off.
- With color on, typer's `OptionHighlighter` emits the option name as
  *adjacent, separately styled spans*: the captured output contains
  `\x1b[1m-\x1b[0m\x1b[1m-host\x1b[0m`, so the literal substring `--host`
  never appears and the bare-substring assertion fails at any width.

Reproduced deterministically on this machine (Windows, click 8.4.1 /
typer 0.25.1 — versions don't matter; the `GITHUB_ACTIONS` gate exists in
both typer lines):

```
GITHUB_ACTIONS=true python - <<'EOF'
from typer.testing import CliRunner
from scistudio.cli.main import app
res = CliRunner().invoke(app, ["serve", "--help"], env={"COLUMNS": "200"})
"--host" in res.output   # -> False (reproduces CI exactly)
EOF
```

Control without `GITHUB_ACTIONS` passes at COLUMNS 200/120/80/40 alike —
width was never the variable.

Verified candidate fixes (both pass under `GITHUB_ACTIONS=true`):

- add `"NO_COLOR": "1"` to the `env=` override in the two tests, or
- strip ANSI before asserting:
  `re.sub(r"\x1b\[[0-9;]*m", "", result.output)`.

Note the misleading evidence trail: the pre-PR gate check recorded in the
ledger (commit `a707e138b`) ran the suites locally, where the tests pass —
a local/CI parity gap, not a gate bypass.

### P2 — should fix before completion

None. Every other claim in the implementer's report verified clean (§2).

### P3 — improvements / follow-ups (non-blocking)

- **P3-1. The WS close-1008 contract is not pinned by a test.**
  `test_unprefixed_websocket_is_rejected_while_prefix_configured`
  (`tests/api/test_root_path.py:200`) asserts only that `WebSocketDisconnect`
  is raised; it never asserts `exc.code == 1008`. The middleware does send
  code 1008 (`src/scistudio/api/app.py`), but the spec's chosen edge-case
  behavior deserves a pinned assertion.
- **P3-2. SC-004's letter vs. the implemented guard.** SC-004 reads "zero
  root-relative `"/api/` or `"/ws"` literals outside `base-path.ts` and its
  tests". The implementation sanctions `apiFetch("/api/...")` literals in
  `frontend/src/lib/api/*.ts` (central normalization) and the guard in
  `base-path.test.ts` only matches literals passed *directly* to raw
  `fetch(`/`new WebSocket(`/`new EventSource(` — a literal first assigned to
  a variable would evade it. The interpretation is reasonable (it matches
  FR-004's "direct `fetch` call sites" enumeration and spec §4.2's scope),
  is explicitly documented in the PR body, and my independent sweep
  (below) found zero violations of the guard's target. Recorded as a
  documented deviation, not a defect.
- **P3-3. uvicorn-version sensitivity of the root-path design is undocumented
  outside code comments.** The verbatim-proxy contract depends on FastAPI's
  `__call__` injecting `scope["root_path"]` (fastapi 0.136.3) and on *not*
  passing uvicorn's own `root_path`. A fastapi/starlette/uvicorn upgrade
  could silently change this; the live probe in this report (§2) is the
  right regression shape if a pinning test is ever wanted.

## 2. Verified claims (all pass)

- **Backend root_path plumbing (FR-001/FR-002/FR-008).**
  `create_app` reads `SCISTUDIO_ROOT_PATH`, normalizes via the single
  `normalize_root_path` helper (9-case parametrized test, incl. `//a//b///`),
  applies it as the FastAPI app-level `root_path`, and exposes it via
  `app.state.root_path`. uvicorn's own `root_path` is deliberately unused;
  the deviation from FR-001's literal "via FastAPI/uvicorn `root_path`"
  wording is sound and documented in code comments and the PR body.
  **Independently verified live**: real uvicorn 0.48.0 server with
  `SCISTUDIO_ROOT_PATH=/p` → `GET /p/api/version` 200, `GET /api/version`
  404, `GET /` 404. (Caution for Windows repro: Git Bash mangles env values
  like `/p` into `P:/` — `MSYS_NO_PATHCONV=1` is required, or the guard
  404s everything. First probe fell into this trap; it is a shell artifact,
  not a code bug.)
- **Unprefixed-root contract (spec §2 edge case).**
  `_RootPathGuardMiddleware` is pure ASGI, covers http+websocket, sends 404
  / close-1008, and is installed only when a prefix is configured (zero
  overhead at the default mount). Verified in tests and live.
- **SPA bootstrap injection (FR-003).** Only `index.html` is templated
  (`window.__SCISTUDIO_BASE_PATH__ = "<prefix>";` right after `<head>`,
  JSON-quoted), hashed assets byte-identical, `Cache-Control: no-cache` on
  templated responses, deep SPA routes injected, empty prefix serves the
  shell byte-identical. Tests: `test_prefixed_spa_shell_carries_injected_base_path`,
  `test_prefixed_spa_deep_route_also_carries_base_path`,
  `test_hashed_assets_stay_byte_identical_under_prefix`,
  `test_empty_prefix_is_a_noop`, plus the FR-009 shell scan and the
  prefix-aware redirect `Location` test.
- **CLI (FR-007).** `serve` and `gui` expose `--host` (`SCISTUDIO_HOST`)
  and `--root-path` (`SCISTUDIO_ROOT_PATH`); defaults unchanged
  (`test_serve_default_invocation_unchanged`,
  `test_gui_default_invocation_unchanged`); advertised URLs and the bundled
  ready JSON carry the prefix; the prefix reaches the app via the env var,
  never via uvicorn kwargs (`"root_path" not in calls` asserted).
- **Frontend base-path single source of truth (FR-004/FR-005/FR-008).**
  `frontend/src/lib/api/base-path.ts` (`getBasePath`/`apiUrl`/`wsUrl`,
  idempotent, cached-once). `apiFetch` routes through `apiUrl`;
  `useWebSocket`/`usePtyWebSocket` dial through `wsUrl`; both same-origin
  validators (`panelModuleLoader.ts`, `dynamicPreviewer.ts`) resolve through
  `apiUrl`; all enumerated direct-`fetch` call sites (SetupScreen,
  useLintMarkers, ProviderIntro, logger ×4) plus sweep extras (useSSE,
  lineage, data.buildPreviewAssetUrl, learningCenter.tutorialPageUrl,
  PreviewHost asset/resource URLs) migrated. My independent sweep:
  zero raw `fetch(`/`new WebSocket(`/`new EventSource(` calls fed
  root-relative literals anywhere in `frontend/src` outside the helper.
- **Worker callback URL (FR-006/SC-003).** `_bind_engine_api_url` derives
  from the configured `SCISTUDIO_ENGINE_API_URL` plus `app.state.root_path`,
  appended exactly once (endswith guard), documented `request.base_url`
  fallback; end-to-end execute test through a prefixed app asserts the
  worker-visible env ends with the prefix.
- **Spec frontmatter move.** `0562ee52e` moves `base-path.ts` from
  `planned_governs` to `governs`; `planned_governs` is now empty — matches
  the delivered file set.
- **Ledger hygiene.** Implementer ledger
  `.workflow/records/2270-feat-2270-prefix-independence.json`: declared scope
  matches spec §4.2; the five sweep-extra frontend files and the spec edit
  were scope-amended with reasons before commit; test paths and docs-na
  rationales recorded; all recorded check events exit 0. Docs N/A rationale
  (`docs/user/reference` is generated; flags self-document via `--help`) is
  acceptable.
- **Targeted re-runs (this audit).**
  `pytest tests/api/test_root_path.py --no-cov` → **35 passed** (claimed 35).
  `vitest run src/lib/api/base-path.test.ts` → **18 passed** (claimed 18).

## 3. CI status at `a707e138b` (run 34029442091)

| Check | Result |
|---|---|
| Lint & Format, Type Check, Architecture Tests, Full Audit, Import Contracts | SUCCESS |
| Frontend, Desktop, Wheel Release Smoke | SUCCESS |
| CodeQL (actions, python, javascript-typescript) | SUCCESS |
| Workflow Gate Check, Deferral Discipline Scan | SUCCESS |
| **Test (Python 3.11)** | **FAILURE** — the two `--help` tests (P1-1) |
| **Test (Python 3.13)** | **FAILURE** — the two `--help` tests (P1-1) |

Only the two P1-1 tests fail (7406 passed otherwise). Per AGENTS.md
("CI must pass before work is complete"), the PR cannot merge as-is.

## 4. Checklist / scope drift

- Checklist (`docs/planning/adr-055-spec0-1-checklist.md`, umbrella branch):
  row 7.3 records "35 + 18 new tests green; full suites 1344 py / 2189
  vitest passed (A1 report)". The new-test counts are accurate; the
  full-suite green claim was true only of the implementer's local runs —
  CI is red (P1-1). Rows 7.4 audit rows remain open for this report; the
  manager owns updating them.
- Scope drift: none. The five sweep-extra frontend files and the spec
  frontmatter edit were gate-amended before commit; the spec's governs list
  matches the diff.
- Diff surface matches the declared scope exactly (23 files: 4 backend
  source, 13 frontend source, 2 new test files, spec frontmatter, ledger).

## 5. Recommendation

**Block** until P1-1 lands: fix the two `--help` assertions with ANSI
stripping or `NO_COLOR=1` (both verified working under `GITHUB_ACTIONS=true`),
push, and let CI go green. No implementation changes are needed; the feature
code, tests, docs rationale, and gate evidence otherwise satisfy the spec.
P3-1 is worth folding into the same push (one assertion); P3-2/P3-3 are
follow-up material.

## 6. Audit commands run

```
gh pr view 2274 / gh pr diff 2274 / gh run view 34029442091 --log(-failed)
PYTHONPATH=src python -m pytest tests/api/test_root_path.py --no-cov     # 35 passed
frontend: vitest run src/lib/api/base-path.test.ts                       # 18 passed
# CI failure reproduction (deterministic, local):
GITHUB_ACTIONS=true python -c "... runner.invoke(cli_app, ['serve','--help'], env={'COLUMNS':'200'}) ..."
#   -> '--host' not in output (ANSI-split spans); passes with NO_COLOR=1 or ANSI strip
# Live uvicorn smoke (uvicorn 0.48.0, verbatim forwarding):
MSYS_NO_PATHCONV=1 SCISTUDIO_ROOT_PATH=/p python -m uvicorn scistudio.api.app:create_app --factory
#   -> /p/api/version 200, /api/version 404, / 404
# Independent frontend sweep:
grep -rnE '(fetch|new WebSocket|new EventSource)\s*\(\s*[`"'"'"']/(api/|ws)' frontend/src  # 0 hits
python -m scistudio.qa.governance.gate_record check --mode local \
  --base origin/feat/2270-prefix-independence --head HEAD               # see ledger
```
