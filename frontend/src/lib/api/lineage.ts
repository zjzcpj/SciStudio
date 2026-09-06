/**
 * ADR-038 §3.8 — Lineage REST surface + row adapters.
 *
 * Extracted from `frontend/src/lib/api.ts` (#1422).
 *
 * The `lineageApi.lineage` namespace (`api.lineage.getRuns(...)`) groups
 * Lineage calls so callers do not collide with `api.list*` / `api.get*`
 * from the existing surface. Mirrors how a future `api.git.*` namespace
 * (already implemented since ADR-039) keeps its concerns isolated.
 *
 * URL shapes match the backend routes declared by D38-2.4a in
 * `src/scistudio/api/routes/runs.py`.
 */

import type {
  LineageBlockExecution,
  LineageDataObjectRef,
  LineageGetRunsParams,
  LineageGetRunsResponse,
  LineageMethodsResponse,
  LineageRunDetail,
  LineageRunSummary,
  RestorePreflight,
} from "../../types/lineage";
import { apiFetch, ApiError } from "./core";
import { apiUrl } from "./base-path";

// ---------------------------------------------------------------------------
// Lineage response adapters
// ---------------------------------------------------------------------------

/**
 * Backend ``runs`` rows are raw SQLite values: ``workflow_dirty`` is 0/1,
 * ``environment_snapshot`` is a JSON string, ``finished_at`` may be null.
 * Convert to the frontend wire type and compute derived fields.
 */
function adaptRunSummary(row: Record<string, unknown>): LineageRunSummary {
  const startedAt = String(row.started_at ?? "");
  const finishedAt = (row.finished_at as string | null | undefined) ?? null;
  const durationMs = computeDurationMs(startedAt, finishedAt);
  return {
    run_id: String(row.run_id ?? ""),
    workflow_id: String(row.workflow_id ?? ""),
    workflow_git_commit: (row.workflow_git_commit as string | null) ?? null,
    workflow_dirty: Boolean(row.workflow_dirty),
    started_at: startedAt,
    finished_at: finishedAt,
    status: (row.status as LineageRunSummary["status"]) ?? "completed",
    triggered_by: (row.triggered_by as LineageRunSummary["triggered_by"]) ?? "user",
    parent_run_id: (row.parent_run_id as string | null) ?? null,
    execute_from_block_id: (row.execute_from_block_id as string | null) ?? null,
    // Codex P2 (PR #944): backend's GET /api/runs returns raw runs-table
    // rows and does NOT include a block_count column, so a static `0`
    // default would silently misreport "0 block(s)" for every list row.
    // Surface `null` ("unknown from this endpoint") instead; the detail
    // endpoint backfills the true count when the user selects the run.
    block_count: typeof row.block_count === "number" ? row.block_count : null,
    duration_ms: durationMs,
  };
}

function adaptBlockExecution(row: Record<string, unknown>): LineageBlockExecution {
  // Hotfix #1015: wire backend-supplied inputs/outputs through. Pre-fix
  // this adapter unconditionally returned `inputs: []` / `outputs: []`
  // with a "future enhancement" comment, but #996 already inlined the
  // join server-side. The Lineage block cards therefore rendered "0
  // inputs / 0 outputs" for every expanded block even though the
  // /api/runs/{id} response carried the data and Methods export
  // surfaced it correctly.
  //
  // Backend entry shape (see `src/scistudio/api/routes/runs.py::get_run`):
  //   { direction, port_name, position, object_id, type_name, backend,
  //     storage_path, produced_by_execution }
  //
  // The frontend type (`LineageDataObjectRef`) only carries the fields
  // the UI consumes; `direction`, `backend`, `produced_by_execution`
  // are dropped at this adapter boundary. The `direction` separator is
  // already applied server-side (inputs vs outputs bucket), so the
  // adapter just trusts the bucket assignment.
  const rawInputs = (row.inputs as Record<string, unknown>[] | undefined) ?? [];
  const rawOutputs = (row.outputs as Record<string, unknown>[] | undefined) ?? [];
  return {
    block_execution_id: String(row.block_execution_id ?? ""),
    block_id: String(row.block_id ?? ""),
    block_type: String(row.block_type ?? ""),
    block_version: String(row.block_version ?? "unknown"),
    block_config_resolved: parseJsonRawObject(row.block_config_resolved),
    started_at: String(row.started_at ?? ""),
    finished_at: (row.finished_at as string | null | undefined) ?? null,
    duration_ms:
      typeof row.duration_ms === "number"
        ? row.duration_ms
        : computeDurationMs(
            String(row.started_at ?? ""),
            (row.finished_at as string | null | undefined) ?? null,
          ),
    termination: (row.termination as LineageBlockExecution["termination"]) ?? "completed",
    termination_detail: (row.termination_detail as string | null | undefined) ?? null,
    inputs: rawInputs.map(adaptDataObjectRef),
    outputs: rawOutputs.map(adaptDataObjectRef),
  };
}

function adaptDataObjectRef(row: Record<string, unknown>): LineageDataObjectRef {
  return {
    object_id: String(row.object_id ?? ""),
    type_name: String(row.type_name ?? ""),
    port_name: String(row.port_name ?? ""),
    position: typeof row.position === "number" ? row.position : 0,
    storage_path: (row.storage_path as string | null | undefined) ?? null,
  };
}

function parseJsonRawObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "string" || value === "") {
    return {};
  }
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // fall through
  }
  return {};
}

function parseJsonObject(value: unknown): Record<string, string> {
  if (typeof value !== "string" || value === "") {
    return {};
  }
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const result: Record<string, string> = {};
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        result[k] = typeof v === "string" ? v : JSON.stringify(v);
      }
      return result;
    }
  } catch {
    // fall through
  }
  return {};
}

function computeDurationMs(startedAt: string, finishedAt: string | null): number | null {
  if (!finishedAt || !startedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return end - start;
}

// ---------------------------------------------------------------------------
// Lineage REST surface
// ---------------------------------------------------------------------------

export const lineageApi = {
  lineage: {
    /**
     * GET /api/runs?workflow_id=...&offset=...&limit=...
     *
     * Backend wraps the list in {runs, offset, limit, has_more}; we only
     * surface the runs array to the slice. Pagination is a future
     * polish issue (ADR-038 §7.3 row counts are KB-MB).
     *
     * Backend rows include extra columns the slice/UI does not use
     * (workflow_yaml_snapshot, environment_snapshot, user_notes, etc.).
     * We also compute the convenience fields the frontend type needs
     * (block_count, duration_ms, workflow_dirty as boolean).
     */
    getRuns: async (params?: LineageGetRunsParams): Promise<LineageGetRunsResponse> => {
      const qs = new URLSearchParams();
      if (params?.workflowId !== undefined) {
        qs.set("workflow_id", params.workflowId);
      }
      if (params?.limit !== undefined) {
        qs.set("limit", String(params.limit));
      }
      const path = qs.toString() ? `/api/runs?${qs.toString()}` : "/api/runs";
      const raw = await apiFetch<{ runs: Record<string, unknown>[] }>(path);
      const runs = (raw.runs ?? []).map(adaptRunSummary);
      return { runs };
    },

    /**
     * GET /api/runs/{run_id}
     *
     * Backend returns {run, block_executions} with raw SQLite rows.
     * We parse JSON columns, compute duration_ms / block_count, and
     * surface the frontend's richer LineageRunDetail shape. Block I/O
     * is not joined server-side yet (ADR-038 follow-up) — inputs and
     * outputs default to [] for v1.
     */
    getRun: async (runId: string): Promise<LineageRunDetail> => {
      const raw = await apiFetch<{
        run: Record<string, unknown>;
        block_executions: Record<string, unknown>[];
      }>(`/api/runs/${encodeURIComponent(runId)}`);
      const runSummary = adaptRunSummary(raw.run);
      const blocks = (raw.block_executions ?? []).map(adaptBlockExecution);
      const summaryWithCount: LineageRunSummary = {
        ...runSummary,
        block_count: blocks.length,
      };
      return {
        run: summaryWithCount,
        blocks,
        environment_snapshot: parseJsonObject(raw.run.environment_snapshot),
        workflow_yaml_snapshot:
          (raw.run.workflow_yaml_snapshot as string | null | undefined) ?? null,
      };
    },

    /**
     * GET /api/runs/{run_id}/methods
     *
     * Backend serves the markdown as text/markdown (PlainTextResponse),
     * so we fetch it as text rather than going through apiFetch's JSON
     * decoder.
     */
    getRunMethods: async (runId: string): Promise<LineageMethodsResponse> => {
      // Raw fetch (text/markdown, not apiFetch's JSON decoder), but the URL
      // still goes through the base-path source of truth (ADR-055 Spec 0).
      const response = await fetch(apiUrl(`/api/runs/${encodeURIComponent(runId)}/methods`));
      if (!response.ok) {
        const payload = (await response.json().catch(() => ({ detail: response.statusText }))) as {
          detail?: string;
        };
        throw new ApiError(
          payload.detail ?? `Request failed with ${response.status}`,
          response.status,
        );
      }
      const markdown = await response.text();
      return { markdown };
    },

    /**
     * GET /api/runs/validate-restore?commit_sha=…[&run_id=…]
     *
     * ADR-038 §3.6 checks, relocated onto Restore by Addendum 1 (#2033).
     *
     * This replaces a stub that returned a hardcoded empty-warnings object
     * without contacting the backend — the route it was waiting for never
     * shipped, so the dialog reported "No drift detected" unconditionally.
     * Errors are deliberately NOT swallowed into an empty result here: a
     * failed check is not a clean check, and the caller renders the difference.
     *
     * Pass `runId` whenever the restore was launched from a specific run.
     * Several runs can share one commit — the pre-run auto-commit is skipped
     * on an already-clean tree — so without it the backend compares against
     * the newest run at that SHA, which may not be the one the user chose.
     */
    validateRestore: async (commitSha: string, runId?: string): Promise<RestorePreflight> => {
      const params = new URLSearchParams({ commit_sha: commitSha });
      if (runId) params.set("run_id", runId);
      return apiFetch<RestorePreflight>(`/api/runs/validate-restore?${params.toString()}`);
    },
  },
};
