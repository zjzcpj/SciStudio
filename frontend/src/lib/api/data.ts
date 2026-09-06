/**
 * Data-artifact REST endpoints (uploads, metadata) and the routed previewer
 * session API. The legacy one-shot `getDataPreview` was removed under ADR-048
 * no-compat (#1604); previews flow through the session helpers below.
 *
 * Extracted from `frontend/src/lib/api.ts` (#1422).
 */

import type {
  DataMetadataResponse,
  DataOpenAsCandidatesResponse,
  DataOpenAsListResponse,
  DataRegisterPathResponse,
  DataUploadResponse,
  PlotCreateRequest,
  PlotCreateResponse,
  PlotListResponse,
  PlotRelinkRequest,
  PlotRelinkResponse,
  PlotRunRequest,
  PlotRunResponse,
  PlotTargetListResponse,
  PreviewEnvelope,
  PreviewResourceResponse,
  PreviewResourceSaveRequest,
  PreviewResourceSaveResponse,
  PreviewTarget,
  PreviewerChoiceListResponse,
  PreviewerChoiceScope,
  PreviewerListResponse,
  PreviewerReloadResponse,
} from "../../types/api";
import { apiUrl } from "./base-path";
import { JSON_HEADERS, apiFetch } from "./core";

/**
 * ADR-048 SPEC 2 / #1606 — build the routed `plot_artifact` {@link PreviewTarget}
 * for a successful {@link PlotRunResponse}.
 *
 * This is the frontend production trigger that closes the runtime dead-wire:
 * after {@link dataApi.runPlotJob} registers the produced artifact and returns
 * its catalog `data_ref`, a caller passes the target this helper builds to
 * {@link PreviewHost}, which opens a routed preview session that resolves the
 * core PlotPreviewer (`core.plot.basic`) and renders the figure. The end-to-end
 * runtime chain (run route -> catalog registration -> routed preview session ->
 * PlotPreviewer) is proven by `tests/api/test_plot_preview_wiring.py`.
 *
 * Returns `null` when the run did not produce a previewable artifact (failed /
 * cancelled / timed-out, or no `data_ref`) so callers render the failure state
 * instead of an empty preview.
 */
export function plotTargetFromRunResponse(result: PlotRunResponse): PreviewTarget | null {
  if (result.status !== "succeeded" || !result.data_ref) return null;
  return {
    kind: "plot_artifact",
    ref: result.data_ref,
    recorded_type: result.recorded_type || "PlotArtifact",
    type_chain: result.type_chain?.length ? result.type_chain : ["DataObject", "PlotArtifact"],
    source: result.source ?? null,
  };
}

/** Build the same-origin URL for a validated previewer asset
 *  (`GET /api/previews/assets/{previewer_id}/{asset_path}`). This is the ONLY
 *  origin a dynamic previewer module is permitted to load from (FR-022).
 *  ADR-055 Spec 0: the URL carries the configured mount prefix (apiUrl is
 *  idempotent under the default root mount). */
export function buildPreviewAssetUrl(previewerId: string, assetPath: string): string {
  const cleaned = assetPath.replace(/^\/+/, "");
  return apiUrl(`/api/previews/assets/${encodeURIComponent(previewerId)}/${cleaned}`);
}

export const dataApi = {
  uploadData: async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return apiFetch<DataUploadResponse>("/api/data/upload", {
      method: "POST",
      body: formData,
    });
  },
  getDataMetadata: (dataRef: string) =>
    apiFetch<DataMetadataResponse>(`/api/data/${encodeURIComponent(dataRef)}`),

  /** Register a project-relative (or project-local absolute) file path with
   *  the data catalog (`POST /api/data/register-path`). #2112: the Data tree
   *  double-click feeds the response into a `data_ref` preview target.
   *
   *  `typeName` opens the file as a specific type and `remember` records that
   *  choice for the extension in the open project; omit both to let the
   *  backend apply the remembered or inferred type. */
  registerDataPath: (request: {
    projectId?: string;
    path: string;
    typeName?: string;
    remember?: boolean;
  }) =>
    apiFetch<DataRegisterPathResponse>("/api/data/register-path", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        project_id: request.projectId,
        path: request.path,
        type_name: request.typeName,
        remember: request.remember ?? false,
      }),
    }),

  /** The types a file could be opened as, plus any remembered choice
   *  (`GET /api/data/open-as/candidates`, #2112). More than one candidate with
   *  nothing remembered is what raises the picker. */
  getOpenAsCandidates: (request: { projectId?: string; path: string }) => {
    const params = new URLSearchParams({ path: request.path });
    if (request.projectId) params.set("project_id", request.projectId);
    return apiFetch<DataOpenAsCandidatesResponse>(`/api/data/open-as/candidates?${params}`);
  },

  /** The open project's remembered extension -> type choices (#2112). */
  listOpenAsTypes: (projectId?: string) =>
    apiFetch<DataOpenAsListResponse>(
      `/api/data/open-as${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),

  /** Forget the remembered type for one extension (#2112). */
  clearOpenAsType: (request: { projectId?: string; extension: string }) => {
    const query = request.projectId ? `?project_id=${encodeURIComponent(request.projectId)}` : "";
    return apiFetch<DataOpenAsListResponse>(
      `/api/data/open-as/${encodeURIComponent(request.extension)}${query}`,
      { method: "DELETE" },
    );
  },

  // -- ADR-048 SPEC 1: routed previewer session API (additive, FR-007) ------

  /** Create a routed preview session for a target and return the first
   *  envelope (`POST /api/previews/sessions`). */
  createPreviewSession: (target: PreviewTarget, query: Record<string, unknown> = {}) =>
    apiFetch<PreviewEnvelope>("/api/previews/sessions", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ target, query }),
    }),

  /** Read the current envelope for a session
   *  (`GET /api/previews/sessions/{session_id}`). */
  getPreviewSession: (sessionId: string) =>
    apiFetch<PreviewEnvelope>(`/api/previews/sessions/${encodeURIComponent(sessionId)}`),

  /** Update query state (slice/page/sort/slot/item) and re-render the envelope
   *  (`PATCH /api/previews/sessions/{session_id}`). */
  patchPreviewSession: (sessionId: string, query: Record<string, unknown>) =>
    apiFetch<PreviewEnvelope>(`/api/previews/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify({ query }),
    }),

  /** Fetch a bounded provider resource — an array tile or a child preview
   *  envelope (`GET /api/previews/sessions/{id}/resources/{resource_id}`). */
  getPreviewResource: (sessionId: string, resourceId: string) =>
    apiFetch<PreviewResourceResponse>(
      `/api/previews/sessions/${encodeURIComponent(sessionId)}/resources/${encodeURIComponent(
        resourceId,
      )}`,
    ),

  // -- #2095: previewer discovery + reload; #2049: per-type choice -----------

  /** List registered previewers with the tier each was discovered from,
   *  ordered in FR-003 routing precedence (`GET /api/previews/previewers`).
   *  `targetType` is an exact-match filter, not the router's specificity
   *  walk. */
  listPreviewers: (targetType?: string) =>
    apiFetch<PreviewerListResponse>(
      `/api/previews/previewers${targetType ? `?target_type=${encodeURIComponent(targetType)}` : ""}`,
    ),

  /** Re-scan the drop-in previewer directories and rebuild the registries
   *  (`POST /api/previews/reload`). */
  reloadPreviewers: () =>
    apiFetch<PreviewerReloadResponse>("/api/previews/reload", { method: "POST" }),

  /** List the effective per-type previewer choices, each with the layer it
   *  came from and whether its previewer is still registered
   *  (`GET /api/previews/choices`). */
  listPreviewerChoices: () => apiFetch<PreviewerChoiceListResponse>("/api/previews/choices"),

  /** Record `targetType -> previewerId` at `scope` — `project` (this project
   *  only) or `user` (every project). Returns the resulting effective choices
   *  (`PUT /api/previews/choices/{target_type}`). */
  setPreviewerChoice: (targetType: string, previewerId: string, scope: PreviewerChoiceScope) =>
    apiFetch<PreviewerChoiceListResponse>(
      `/api/previews/choices/${encodeURIComponent(targetType)}`,
      {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({ previewer_id: previewerId, scope }),
      },
    ),

  /** Clear the choice for `targetType` at `scope`; clearing a type that was
   *  never chosen succeeds (`DELETE /api/previews/choices/{target_type}`). */
  clearPreviewerChoice: (targetType: string, scope: PreviewerChoiceScope) =>
    apiFetch<PreviewerChoiceListResponse>(
      `/api/previews/choices/${encodeURIComponent(targetType)}?scope=${encodeURIComponent(scope)}`,
      { method: "DELETE" },
    ),

  /** Save a bounded provider resource to a user-selected absolute file path
   *  (`POST /api/previews/sessions/{id}/resources/{resource_id}/save`). */
  savePreviewResource: (
    sessionId: string,
    resourceId: string,
    request: PreviewResourceSaveRequest,
  ) =>
    apiFetch<PreviewResourceSaveResponse>(
      `/api/previews/sessions/${encodeURIComponent(sessionId)}/resources/${encodeURIComponent(
        resourceId,
      )}/save`,
      {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(request),
      },
    ),

  // -- ADR-048 SPEC 2 / #1606: plot-job run + preview wiring ----------------

  /** List workflow output targets available for a new plot scaffold. */
  listPlotTargets: (params?: {
    workflowId?: string | null;
    workflowPath?: string | null;
    nodeId?: string | null;
    outputPort?: string | null;
    includeUnavailable?: boolean;
  }) => {
    const search = new URLSearchParams();
    if (params?.workflowId) search.set("workflow_id", params.workflowId);
    if (params?.workflowPath) search.set("workflow_path", params.workflowPath);
    if (params?.nodeId) search.set("node_id", params.nodeId);
    if (params?.outputPort) search.set("output_port", params.outputPort);
    if (params?.includeUnavailable === false) search.set("include_unavailable", "false");
    const suffix = search.toString();
    return apiFetch<PlotTargetListResponse>(`/api/plots/targets${suffix ? `?${suffix}` : ""}`);
  },

  /** Create plots/<id>/plot.yaml plus a render script from the plot template. */
  createPlot: (request: PlotCreateRequest) =>
    apiFetch<PlotCreateResponse>("/api/plots", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(request),
    }),

  /** Delete a plot's manifest and render script directory. */
  deletePlot: (plotId: string) =>
    apiFetch<void>(`/api/plots/${encodeURIComponent(plotId)}`, {
      method: "DELETE",
    }),

  /** Re-point an existing plot at a new workflow output target (bug#7).
   *  `POST /api/plots/{plot_id}/relink` rewrites only the manifest target block
   *  (strict 1:1) and re-validates, so a previously broken target becomes valid
   *  without recreating the plot or its render script. */
  relinkPlot: (plotId: string, request: PlotRelinkRequest) =>
    apiFetch<PlotRelinkResponse>(`/api/plots/${encodeURIComponent(plotId)}/relink`, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(request),
    }),

  /** Run a plot job and register its artifact for routed preview
   *  (`POST /api/plots/run`). On success the response's `data_ref` opens a
   *  `plot_artifact` preview session via {@link plotTargetFromRunResponse} +
   *  {@link createPreviewSession}; the produced figure then renders through the
   *  core PlotPreviewer. */
  runPlotJob: (request: PlotRunRequest) =>
    apiFetch<PlotRunResponse>("/api/plots/run", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(request),
    }),

  /** List project-local plot manifests, optionally scoped to a workflow block. */
  listPlots: (params?: {
    workflowId?: string | null;
    nodeId?: string | null;
    outputPort?: string | null;
  }) => {
    const search = new URLSearchParams();
    if (params?.workflowId) search.set("workflow_id", params.workflowId);
    if (params?.nodeId) search.set("node_id", params.nodeId);
    if (params?.outputPort) search.set("output_port", params.outputPort);
    const suffix = search.toString();
    return apiFetch<PlotListResponse>(`/api/plots${suffix ? `?${suffix}` : ""}`);
  },
};
