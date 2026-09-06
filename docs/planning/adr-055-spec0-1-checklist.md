---
title: "ADR-055 Spec 0-1 Agent Dispatch Checklist"
status: Approved
owners:
  - "@jiazhenz026"
related_adrs:
  - 55
related_specs:
  - adr-055-prefix-independence
  - adr-055-webmcp-bridge
language_source: en
---

# ADR-055 Spec 0-1 Agent Dispatch Checklist

> Mandatory tracking file. Every agent edits only rows it owns.
> Drift is a protocol violation.
> Source template:
> `docs/ai-developer/templates/agent-dispatch-checklist-template.md`

## 1. Change Summary

- Owner request: "Act as manager; implement ADR-055 spec0-1 (prefix
  independence + WebMCP bridge, referencing the read-only scistudio-web-demo);
  deliver 2 PRs with CI passing, no deferrals."
- Task kind: `feature` (implementation tracks); `manager` (this checklist)
- Manager persona: `manager`
- Issue: `#2270` (Spec 0), `#2271` (Spec 1); umbrella context `#2263` (closed),
  ADR-055 tracking `#2239` (closed)
- Gate record: `.workflow/records/track-adr-055-spec0-1-track-adr-055-spec0-1.json`
- Branch/worktree plan: manager umbrella `track/adr-055-spec0-1` at
  `.worktrees/track-adr-055-spec0-1`; agent branches
  `feat/2270-prefix-independence` (base `origin/main`) and
  `feat/2271-webmcp-bridge` (stacked on `feat/2270-prefix-independence`,
  `--base-ref` recorded per #2143)
- Protected branch: `main`
- Umbrella branch: `track/adr-055-spec0-1`
- Umbrella PR: `#2273`
- Umbrella PR title: `[DO NOT MERGE] ADR-055 Spec 0-1 dispatch`
- Final PR target: `main` (manager explicitly assigns both spec PRs as final
  PRs to the protected branch per owner directive "推上去2个PR")
- Dispatch prompt templates:
  - Work: `docs/ai-developer/templates/agent-dispatch-prompt-template.md`
  - Audit with context:
    `docs/ai-developer/templates/agent-dispatch-audit-with-context-prompt-template.md`
  - Audit no context:
    `docs/ai-developer/templates/agent-dispatch-audit-no-context-prompt-template.md`

## 2. Scope

- In scope:
  - Spec 0 (`docs/specs/adr-055-prefix-independence.md`): backend `root_path`
    plumbing, SPA bootstrap injection, CLI `--root-path`/host flags, frontend
    `base-path.ts` + migration of root-relative call sites, worker callback URL
  - Spec 1 (`docs/specs/adr-055-webmcp-bridge.md`): `src/scistudio/api/routes/webmcp.py`,
    adapter contract in `ai/agent/mcp/server.py`, audience-tag filtering,
    session middleware (loopback token), `frontend/src/webmcp/`, tests
- Out of scope:
  - Domain tools (`get_agent_context`, workspace, execution):
    adr-055-agent-context-workspace
  - Hub OAuth, per-user routing: adr-055-lab-deployment
  - Local background runtime: adr-055-local-background-runtime
  - AI-host presentation (deferred by owner)
  - ANY write/commit/push to `scistudio-web-demo` (read-only reference at
    `.scratch-design/webmcp-recovery/scistudio-web-demo`; local blocking hooks
    installed, push remote disabled, upstream branch protection on)
- Protected paths: none touched beyond the spec-declared core files; core-path
  changes are spec-governed (ADR-055 governs lists them)
- Deferred work: N/A (owner directive: no deferrals)

## 3. Conventions

- `[ ]` not started
- `[~]` in progress
- `[x]` done
- `[!]` blocked
- Every completed row MUST include an artifact:
  PR link, commit, test command, report path, or gate-record entry.
- Chat messages are not checklist evidence.
- Agents edit only their own rows.
- Scope changes require gate-record amendment before work continues.

## 4. Manager Preflight

- [x] Dedicated manager branch and worktree created. ->
      `track/adr-055-spec0-1` at `.worktrees/track-adr-055-spec0-1`
- [x] Existing issue linked, or new issue created only if none exists. ->
      #2270, #2271 (no open issue tracked the work; #2239/#2263 closed)
- [x] Gate record started. -> `.workflow/records/track-adr-055-spec0-1-track-adr-055-spec0-1.json`
- [x] Scope include/exclude recorded in the gate record.
- [x] Umbrella branch created. -> `track/adr-055-spec0-1`
- [x] Umbrella PR opened. -> #2273
- [x] Umbrella PR title includes `[DO NOT MERGE]`.
- [x] Protected branch and umbrella PR number recorded in this checklist. -> main / #2273
- [x] No `pip install -e .` environment pollution found. -> gate CLI runs via
      `PYTHONPATH=src`, no editable install
- [x] Dispatch checklist copied from the template and committed.
- [ ] Dispatch prompts created from the correct prompt template and linked
      below.
- [x] Sentrux baseline recorded, or N/A reason recorded. -> N/A: Sentrux MCP
      not available in this runtime; guard evidence is recorded by
      `gate_record check` where applicable.

## 5. Local Gate Hook Bypass Evidence

- Authorized bypass label: `N/A`
- Owner authorization source: `N/A`
- Reason: `N/A`

| Hook | Command | Bypass label | Status | Evidence |
|---|---|---|---|---|
| Pre-commit | `python -m scistudio.qa.governance.gate_record check --mode pre-commit` | `N/A` | `[ ]` | `<pending>` |
| Commit message | `python -m scistudio.qa.governance.gate_record check --mode commit-msg` | `N/A` | `[ ]` | `<pending>` |
| Pre-push | `python -m scistudio.qa.governance.gate_record check --mode pre-push` | `N/A` | `[ ]` | `<pending>` |
| Pre-PR reconcile | `python -m scistudio.qa.governance.gate_record check --mode pre-pr --pr-body-file <body-file>` | `N/A` | `[ ]` | `<pending>` |

## 5.1 Docs Impact Check

- Wrapper/hook/gate-record/receipt/CI/runtime behavior changed: `no`
- AI docs checked:
  `docs/ai-developer/rules.md`,
  `docs/ai-developer/specific_rules/gated-workflow.md`,
  `docs/ai-developer/specific_rules/agent-dispatch.md`,
  `docs/ai-developer/templates/*dispatch*.md`
- Updated docs or N/A rationale: `N/A — no AI-workflow behavior changes`

## 6. Dispatch Matrix

| Agent | Persona | Audit mode | Prompt | Task | Branch | Worktree | Write set | Out of scope | Issue/PR | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| `A1` | `implementer` | `N/A` | `docs/planning/adr-055-spec0-1-dispatch-prompts.md` (A1) | Spec 0 prefix independence | `feat/2270-prefix-independence` | `.worktrees/feat-2270-prefix-independence` | spec §4.2 files | `docs/ai-developer/**`, demo repo, other ADR-055 specs | `#2270` / PR #2274 | `[x]` |
| `A2` | `implementer` | `N/A` | `docs/planning/adr-055-spec0-1-dispatch-prompts.md` (A2) | Spec 1 WebMCP bridge | `feat/2271-webmcp-bridge` | `.worktrees/feat-2271-webmcp-bridge` | spec §4.2 files | `docs/ai-developer/**`, demo repo, other ADR-055 specs | `#2271` / PR #2275 | `[x]` |

## 7. Track: Spec 0 — Prefix Independence

### 7.1 Track Scope

- Owner: `A1 (implementer)`
- In scope: spec `adr-055-prefix-independence` §3 FR-001..FR-009, §4.2 files
- Out of scope: JupyterHub/auth, WebMCP bridge routes, lab deployment
- Required docs: spec acceptance; user docs N/A unless behavior surface
  changes warrant it (record rationale in ledger)
- Required tests: `tests/api/test_root_path.py`,
  `frontend/src/lib/api/base-path.test.ts`

### 7.2 Dispatch

- [x] Prompt file created or dispatch prompt recorded. -> `docs/planning/adr-055-spec0-1-dispatch-prompts.md`
- [x] Correct prompt template selected. -> work template (non-audit)
- [x] Audit mode recorded when persona is `audit_reviewer`. -> N/A
- [x] Agent branch/worktree assigned.
- [x] Write set and out-of-scope paths included in prompt.
- [x] TODO rule included in prompt.
- [x] Required checks included in prompt.

### 7.3 Implementation

- [x] Backend root_path + SPA injection + CLI flags -> PR #2274 (`feat/2270-prefix-independence` @ dc6efef13)
- [x] Frontend base-path module + call-site migration -> PR #2274 (19 frontend files incl. sweep extras, gate-amended)
- [x] Tests: `tests/api/test_root_path.py`, `base-path.test.ts` -> 35 + 18 new tests green; full suites 1344 py / 2189 vitest passed (A1 report)

### 7.4 Audit

- [x] Audit agent assigned, or manager audit completed. -> AU1, with-context
- [x] Audit report file path assigned. -> `docs/audit/2026-09-06-adr-055-spec0-with-context.md`
- [x] Audit report committed. -> `dedb71c3` on `audit/2270-spec0-with-context`
- [x] Audit report merged into final PR evidence path. -> cherry-picked as `666588dbb` on `feat/2270-prefix-independence` (PR #2274)
- [x] Findings recorded. -> report §findings: P1-1 (CI red at audited head), P3 x3
- [x] P1 findings fixed before integration. -> P1-1 root cause (typer OptionHighlighter ANSI span splitting, width-independent) confirmed by AU1; resolved by A1's introspection-based fix `12e58baf1` — CI green at `dd1affc79`
- [x] P2/P3 findings fixed or tracked with owner-approved rationale. -> P3-1 (WS close-1008 assertion) fixed in `ea69abcf0`; P3-2/P3-3 documented in the committed audit report itself (tracked evidence)

### 7.5 Integration

- [x] Agent output reviewed by manager. -> AU1 audit + manager diff review; claims match code
- [x] Scope compliance verified. -> gate ledger `observed_diff` consistent; sweep extras gate-amended
- [x] Conflicts resolved intentionally. -> N/A (no conflicts; sequential dispatch)
- [x] Track merged or integrated. -> PR #2274 open to main, CI green at `47925cde9` (audit report + P3-1 fix included)

## 8. Track: Spec 1 — WebMCP Bridge

### 8.1 Track Scope

- Owner: `A2 (implementer)`
- In scope: spec `adr-055-webmcp-bridge` §3 FR-001..FR-011, §4.2 files;
  transplant from demo commit `952f697b` (read-only)
- Out of scope: domain tools, Hub OAuth backend, AI-host presentation,
  local socket wire protocol changes
- Required docs: spec acceptance; N/A rationale recorded otherwise
- Required tests: `tests/api/test_webmcp.py`, `tests/ai/test_mcp_fastmcp.py`
  (audience filtering), `frontend/src/webmcp/register.test.ts`

### 8.2 Dispatch

- [x] Prompt file created or dispatch prompt recorded. -> `docs/planning/adr-055-spec0-1-dispatch-prompts.md`
- [x] Correct prompt template selected. -> work template (non-audit)
- [x] Audit mode recorded when persona is `audit_reviewer`. -> N/A
- [x] Agent branch/worktree assigned (stacked on Spec 0 branch, `--base-ref`
      recorded).
- [x] Write set and out-of-scope paths included in prompt.
- [x] TODO rule included in prompt.
- [x] Required checks included in prompt.

### 8.3 Implementation

- [x] Bridge router + adapter contract + audience filtering -> PR #2275 (`feat/2271-webmcp-bridge` @ 12096c0ab)
- [x] Session middleware (loopback token) + project binding -> PR #2275 (409 stale_project_context, X-SciStudio-WebMCP-Token)
- [x] Frontend registration module + tests -> PR #2275 (`frontend/src/webmcp/`, 8 lifecycle tests)

### 8.4 Audit

- [x] Audit agent assigned, or manager audit completed. -> AU2, with-context
- [x] Audit report file path assigned. -> `docs/audit/2026-09-06-adr-055-spec1-with-context.md`
- [x] Audit report committed. -> `7f9cb078f` on `audit/2271-spec1-with-context`
- [x] Audit report merged into final PR evidence path. -> cherry-picked as `0dfcdd217` on `feat/2271-webmcp-bridge` (PR #2275)
- [x] Findings recorded. -> report §findings: P1-1 (stale-head CI), P2-1 (manual host evidence), P3-1 (spec0 FR-002 annotation)
- [x] P1 findings fixed before integration. -> P1-1 was the pre-fix head; CI green at `ecb183a6a` after the introspection fix merge
- [x] P2/P3 findings fixed or tracked with owner-approved rationale. -> P2-1: manager-run host capability evidence collected and posted as PR #2275 comment (e2e probe; recorded host behavior per ADR-055 §11); P3-1 recorded here: spec0 FR-002 text predates spec1 FR-006 token bootstrap; the rescoped no-op test documents intent — noted for the spec-authoring track, no code impact

### 8.5 Integration

- [x] Agent output reviewed by manager. -> AU2 audit + manager diff review; no drift found
- [x] Scope compliance verified. -> ledger observed_diff == 16 files; two manager-authorized cross-spec additions recorded
- [x] Conflicts resolved intentionally. -> base merges `737438424`/`970ac21b4`/`395505594` conflict-free; three-way test file state verified by grep + runs
- [x] Track merged or integrated. -> PR #2275 open to main (stacked on #2274), CI green at `ecb183a6a`; final head `6d42b54d2` pending CI

## 9. Verification Evidence

| Check | Command or tool | Status | Evidence |
|---|---|---|---|
| Gate ledger check (local) | per-branch `gate_record check` | `[x]` | PR #2274: tier-1 all green at `dd1affc79`+; PR #2275: tier-1 all green at `6d42b54d2` (`--record` used on finalized ledgers) |
| Targeted tests | `pytest tests/api/test_root_path.py` (37), `tests/api/test_webmcp.py` (17), `tests/ai/test_mcp_fastmcp.py` audience tests, `vitest base-path.test.ts` (18) + `webmcp/register.test.ts` (8) | `[x]` | all green; full suites: 1344 py (api+cli) / 2197 vitest on #2275 head |
| Pre-push gate check | n/a — pre-push hook is the allow shim; pre-pr used as the hard local gate per ADR-042 Addendum 6 | `[x]` | N/A rationale |
| Gate ledger check (pre-PR) | `gate_record check --mode pre-pr --pr-body-file .workflow/local/pr-body.md` | `[x]` | reconciliation passed on all three branches (umbrella, feat/2270, feat/2271) |
| Gate finalize (pre-PR) | `gate_record finalize --closes "#2270"` / `"#2271"` / `"#2272"` | `[x]` | recorded in each ledger; post-PR finalize recorded PR provenance (#2273/#2274/#2275) |
| Wrapper preflight | `python scripts/scistudio_pr_create.py` | `[x]` | pre-flight clean on all three PRs; PRs opened via the wrapper |
| CI | GitHub Actions | `[x]` | PR #2274 @ `774e35620` (post-Codex-fixes): 16/16 pass. PR #2275 @ `e817f9b82` (post-Codex-fixes + final base merge): 16/16 pass |
| Live host evidence | CDP probe + HTTP probes on `scistudio serve` | `[x]` | PR #2275 comment: Chrome 152 stable, modelContext absent (trial-gated), token injection + graceful degradation + 36-tool catalogue + 409 binding verified live |

## 10. Drift Log

Append only.

| Date | Agent | Drift | Action | Follow-up |
|---|---|---|---|---|
| 2026-09-06 | codex-review | PR #2274: 4 findings (2xP1: prefixed deep-SPA asset resolution, worker callback host vs bind host; 2xP2: /api prefix collision, doubled-separator routing). PR #2275: 3 findings (P1: re-register on project change; P2: audience filter also in local-agent prompt; CodeQL: exception info exposure) | A1/A2 re-dispatched to fix all seven with tests; no deferrals per owner | all 7 fixed with tests; CI green again at final heads (#2274 `774e35620`, #2275 `e817f9b82`); review replies posted on both PRs |
| 2026-09-06 | manager | Hook-install command resolved a relative gitdir and briefly wrote blocking hooks into the main repo `.git/hooks`; a local-only test commit landed in the demo clone | Restored main hooks to documented state (pre-push allow shim; no commit hooks per #2150); `reset --hard` the demo clone back to `cf0fe769` (no push, remote disabled); reinstalled blocking hooks with absolute paths and verified they fire | N/A |

## 11. Final Readiness

- [x] All dispatched agents have final outputs. -> A1 (PR #2274), A2 (PR #2275), AU1/AU2 (audit reports), e2e probe
- [x] Manager reviewed every changed file. -> via AU1/AU2 with-context audits + manager diff review; claims match code, no drift
- [x] Gate record includes issue, scope, plan, docs, tests, checks, Sentrux
      evidence when needed, commit, and PR evidence. -> Sentrux MCP unavailable in this runtime (recorded N/A in ledgers/PR bodies)
- [x] PR closes every issue fixed by the dispatch. -> #2274 closes #2270; #2275 closes #2271; #2273 closes #2272
- [x] CI passed. -> 16/16 on both final PR heads
- [x] Checklist final state matches PR and gate record.
