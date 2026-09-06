/**
 * Registers SciStudio's MCP tools with the browser agent via WebMCP
 * (ADR-055 Spec 1, FR-009).
 *
 * The catalogue is the backend's, not a second hand-written list: the page
 * fetches `/api/webmcp/tools`, which is the same FastMCP instance the local
 * socket transport serves. A tool added to the Python server appears here
 * without a frontend change, and the two surfaces cannot drift.
 *
 * Each `execute` posts back to `/api/webmcp/call`, so a tool invoked by an
 * external host runs exactly the code path a local agent's tool call runs.
 * The visible consequence is that the SciStudio UI updates as the agent
 * works — the user watches the graph change rather than reading a
 * transcript about it.
 *
 * Hardened per ADR-055 §9.2 beyond the hackathon demo:
 *
 * - probes BOTH `document.modelContext` (Chrome 150+, ChatGPT desktop) and
 *   `navigator.modelContext` (Chrome 149);
 * - a superseded registration attempt is aborted and MUST NOT report
 *   success — the demo never re-checked after the catalogue await;
 * - a per-tool registration refusal is logged with the tool name and is not
 *   fatal to the batch;
 * - `registerSciStudioToolsWithRetry` covers a capability that appears
 *   after page load;
 * - bridge calls carry the session token header (FR-006) and the catalogue's
 *   project snapshot (FR-005), and all URLs go through the base-path-aware
 *   `apiFetch` (FR-008).
 */

import { apiFetch, webmcpSessionHeaders } from "../lib/api/core";
import { logger } from "../lib/logger";
import { useAppStore } from "../store";

import type { ModelContext, ToolResult } from "./types";

interface CatalogueEntry {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  category: string;
  mutation: "read" | "write";
}

interface Catalogue {
  tools: CatalogueEntry[];
  /** Active-project snapshot at fetch time (FR-005). */
  context: { projectId: string | null };
}

/**
 * The in-flight (or most recent) registration attempt's controller. A new
 * attempt aborts the previous one first, so a re-registration replaces
 * rather than duplicates — and the aborted attempt can tell it was
 * superseded.
 */
let activeRegistration: AbortController | null = null;

/**
 * Probe both host surfaces. The `document` form is current (Chrome 150+,
 * ChatGPT desktop); the `navigator` form is the Chrome 149 trial surface.
 */
export function probeModelContext(): ModelContext | undefined {
  if (typeof document !== "undefined" && document.modelContext) {
    return document.modelContext;
  }
  if (typeof navigator !== "undefined" && navigator.modelContext) {
    return navigator.modelContext;
  }
  return undefined;
}

async function fetchCatalogue(): Promise<Catalogue> {
  return apiFetch<Catalogue>("/api/webmcp/tools", {
    headers: { ...webmcpSessionHeaders() },
  });
}

async function callTool(
  name: string,
  args: Record<string, unknown>,
  projectId: string | null,
  signal?: AbortSignal,
): Promise<ToolResult> {
  return apiFetch<ToolResult>("/api/webmcp/call", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...webmcpSessionHeaders() },
    body: JSON.stringify({ name, arguments: args, projectId }),
    signal,
  });
}

/** True when this attempt was superseded by a newer one (or aborted). */
function isSuperseded(controller: AbortController): boolean {
  return controller.signal.aborted || activeRegistration !== controller;
}

/**
 * Register every backend tool with the host's WebMCP API.
 *
 * Safe to call when WebMCP is unavailable: the function reports why and
 * returns 0, because a browser without the capability is the ordinary case,
 * not a failure. Calling it again aborts the previous attempt first, so a
 * reconnect or project switch re-registers cleanly instead of accumulating
 * duplicates. An attempt superseded while in flight returns 0 and never
 * reports success.
 */
export async function registerSciStudioTools(): Promise<number> {
  const modelContext = probeModelContext();
  if (!modelContext) {
    logger.info(
      "WebMCP unavailable (neither document.modelContext nor navigator.modelContext) — " +
        "the app runs normally, but no tools are exposed to a browser agent.",
    );
    return 0;
  }

  activeRegistration?.abort();
  const controller = new AbortController();
  activeRegistration = controller;

  let catalogue: Catalogue;
  try {
    catalogue = await fetchCatalogue();
  } catch (error) {
    if (activeRegistration === controller) {
      activeRegistration = null;
    }
    logger.error("WebMCP: could not fetch the tool catalogue", { error: String(error) });
    return 0;
  }

  // ADR-055 §9.2: a newer attempt may have started while the catalogue
  // fetch was in flight. This attempt is obsolete and MUST NOT report
  // success or keep registering.
  if (isSuperseded(controller)) {
    logger.info("WebMCP: registration superseded before the catalogue arrived; abandoned");
    return 0;
  }

  const projectId = catalogue.context.projectId;
  let registered = 0;
  for (const entry of catalogue.tools) {
    if (controller.signal.aborted) {
      break;
    }
    try {
      await modelContext.registerTool(
        {
          name: entry.name,
          description: entry.description,
          inputSchema: entry.inputSchema,
          execute: async (args, options) => {
            logger.debug(`WebMCP → ${entry.name}`);
            try {
              return await callTool(entry.name, args ?? {}, projectId, options?.signal);
            } catch (error) {
              // Handed back as tool content rather than thrown: a failed call
              // (including a stale-project 409, whose message tells the agent
              // to re-fetch the catalogue) is something the agent can act on,
              // and an exception would reach it as an opaque dead end.
              return {
                content: [{ type: "text", text: `Tool call failed: ${String(error)}` }],
                isError: true,
              };
            }
          },
        },
        { signal: controller.signal },
      );
      registered += 1;
    } catch (error) {
      if (controller.signal.aborted) {
        // Superseded mid-batch; this is the abort, not a per-tool refusal.
        break;
      }
      logger.warn(`WebMCP: refused to register '${entry.name}'`, { error: String(error) });
    }
  }

  if (isSuperseded(controller)) {
    logger.info("WebMCP: registration superseded mid-batch; result discarded");
    return 0;
  }

  logger.info(`WebMCP: registered ${registered}/${catalogue.tools.length} tools`);
  return registered;
}

/**
 * Re-register when the active project changes (PR #2275 review P1).
 *
 * The `execute` closures capture the catalogue's project snapshot, so a
 * boot-time registration made before any project is open would post
 * `projectId: null` forever — and every mutation call would be rejected as
 * `stale_project_context` once a project is open. Subscribing to the
 * project store re-runs `registerSciStudioTools` on open/create/switch/
 * close; the FR-009 lifecycle guarantees hold because re-registration
 * aborts the superseded attempt (which then never reports success) and
 * fetches a fresh catalogue snapshot.
 *
 * Returns the unsubscribe function. Fire-and-forget by design: the
 * subscription itself never blocks app boot (FR-010).
 */
export function subscribeToProjectChanges(): () => void {
  let lastProjectId = useAppStore.getState().currentProject?.id ?? null;
  return useAppStore.subscribe((state) => {
    const projectId = state.currentProject?.id ?? null;
    if (projectId === lastProjectId) {
      return;
    }
    lastProjectId = projectId;
    if (!probeModelContext()) {
      // No host capability: nothing is registered, so nothing to refresh.
      return;
    }
    void registerSciStudioTools();
  });
}

export interface RegisterRetryOptions {
  /** Total probing attempts before giving up (default 4). */
  maxAttempts?: number;
  /** Delay between attempts in ms (default 2000). */
  delayMs?: number;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Retry path for a WebMCP capability that appears after page load (FR-009):
 * probe, and while the capability is absent wait and probe again, up to a
 * bounded budget. The first attempt that finds a capability delegates to
 * `registerSciStudioTools`; exhausting the budget resolves to 0 without
 * erroring.
 */
export async function registerSciStudioToolsWithRetry(
  options?: RegisterRetryOptions,
): Promise<number> {
  const maxAttempts = options?.maxAttempts ?? 4;
  const delayMs = options?.delayMs ?? 2000;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    if (probeModelContext()) {
      return registerSciStudioTools();
    }
    if (attempt < maxAttempts) {
      await delay(delayMs);
    }
  }
  logger.info(
    `WebMCP unavailable after ${maxAttempts} probe(s) — the app runs normally, ` +
      "but no tools are exposed to a browser agent.",
  );
  return 0;
}
