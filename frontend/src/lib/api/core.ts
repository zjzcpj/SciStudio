/**
 * Shared low-level HTTP helpers for the `api.*` surface.
 *
 * Extracted from `frontend/src/lib/api.ts` (#1422) so each domain module
 * (projects, workflows, git, lineage, ...) can import the same fetcher
 * without bloating the file past the 500-LOC ceiling.
 *
 * The public `ApiError` symbol is re-exported by `../api.ts` so existing
 * `import { ApiError } from "../lib/api"` callers keep working.
 */

import { logger } from "../logger";
import { apiUrl } from "./base-path";

declare global {
  interface Window {
    /**
     * Per-launch WebMCP bridge session token, injected into the served
     * `index.html` bootstrap by the backend (`src/scistudio/api/spa.py`,
     * ADR-055 Spec 1 FR-006). Absent when the page was not served by the
     * backend (e.g. the vite dev server) — bridge calls then fail closed.
     */
    __SCISTUDIO_WEBMCP_TOKEN__?: string;
  }
}

/** Header carrying the loopback session token on every WebMCP bridge call (FR-006). */
export const WEBMCP_SESSION_HEADER = "X-SciStudio-WebMCP-Token";

/**
 * Auth headers for WebMCP bridge fetches: the per-launch session token the
 * backend injected into the served page bootstrap. Returns an empty object
 * when no token was injected, so callers can spread it unconditionally.
 */
export function webmcpSessionHeaders(): Record<string, string> {
  const token =
    typeof window !== "undefined" && typeof window.__SCISTUDIO_WEBMCP_TOKEN__ === "string"
      ? window.__SCISTUDIO_WEBMCP_TOKEN__
      : "";
  return token ? { [WEBMCP_SESSION_HEADER]: token } : {};
}

export const JSON_HEADERS = {
  "Content-Type": "application/json",
};

/** Generate a short correlation id matching the backend's X-Request-ID format. */
function newRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID().replace(/-/g, "").slice(0, 16);
  }
  return Math.random().toString(16).slice(2, 18);
}

/**
 * Error thrown by `apiFetch` for non-2xx responses. Exposes the HTTP status
 * code so callers can branch on it (e.g. fall back on 500 but not on 504).
 * See issue #678.
 */
export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Thrown when a request carrying `timeoutMs` outlives its deadline (#2019).
 *
 * Distinct from `ApiError`: no HTTP response ever arrived, so there is no
 * status code to branch on. Callers that need to tell "the server said no"
 * from "the server never answered" check `instanceof ApiTimeoutError`.
 */
export class ApiTimeoutError extends Error {
  timeoutMs: number;

  constructor(path: string, timeoutMs: number) {
    super(`Request to ${path} timed out after ${Math.round(timeoutMs / 1000)}s`);
    this.name = "ApiTimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

export interface ApiFetchOptions extends RequestInit {
  /**
   * #2019: abort the request after this many milliseconds and reject with
   * `ApiTimeoutError`. Omit for no client-side deadline (the default — most
   * calls are quick, and a spurious abort is worse than waiting).
   *
   * Set it on calls whose failure mode is "the UI is stuck until this
   * settles". A hung backend used to wedge the app permanently: the `finally`
   * that clears the busy flag and closes the modal never ran, because the
   * promise it hung off never settled.
   */
  timeoutMs?: number;
}

export async function apiFetch<T>(path: string, init?: ApiFetchOptions): Promise<T> {
  // ADR-055 Spec 0 (FR-004): every API call goes through the single
  // base-path source of truth so a prefixed mount (e.g. a Hub user route)
  // needs no per-call-site handling. Idempotent under the default root mount.
  const url = apiUrl(path);
  // #1741: attach a correlation id (X-Request-ID) and emit DEBUG at the API
  // boundary so every call is traceable across frontend -> backend logs.
  const requestId = newRequestId();
  const headers = new Headers(init?.headers);
  headers.set("X-Request-ID", requestId);
  const method = init?.method ?? "GET";
  const started = typeof performance !== "undefined" ? performance.now() : 0;
  logger.debug(`→ ${method} ${url}`, { request_id: requestId });

  // #2019: an AbortController rather than a bare Promise.race, so a timed-out
  // request actually releases the connection instead of running on unobserved.
  const { timeoutMs, ...requestInit } = init ?? {};
  const controller = timeoutMs !== undefined ? new AbortController() : null;
  const timer =
    controller !== null && timeoutMs !== undefined
      ? setTimeout(() => controller.abort(), timeoutMs)
      : null;
  // Honour a caller-supplied signal too: whichever fires first wins.
  if (controller !== null && requestInit.signal) {
    const callerSignal = requestInit.signal;
    if (callerSignal.aborted) {
      controller.abort();
    } else {
      callerSignal.addEventListener("abort", () => controller.abort(), { once: true });
    }
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...requestInit,
      headers,
      ...(controller !== null ? { signal: controller.signal } : {}),
    });
  } catch (error) {
    // Our own deadline tripped — report it as such rather than as a generic
    // network fault, so the banner says "timed out" instead of "aborted".
    if (timeoutMs !== undefined && controller?.signal.aborted && !requestInit.signal?.aborted) {
      logger.error(`timeout: ${method} ${url} after ${timeoutMs}ms`, { request_id: requestId });
      throw new ApiTimeoutError(path, timeoutMs);
    }
    logger.error(`network error: ${method} ${url}`, {
      request_id: requestId,
      error: String(error),
    });
    throw error;
  } finally {
    if (timer !== null) {
      clearTimeout(timer);
    }
  }
  const elapsedMs = Math.round(
    (typeof performance !== "undefined" ? performance.now() : 0) - started,
  );

  if (!response.ok) {
    const payload = (await response.json().catch(() => ({ detail: response.statusText }))) as {
      detail?: string | { message?: string; errors?: unknown };
    };
    // ``detail`` can be a plain string (legacy + FastAPI default) OR a
    // structured object like ``{message, errors}`` (used by the workflow
    // GET route when a YAML fails pydantic validation — surfaces the
    // exact field/reason list for the agent / GUI to display).
    let message: string;
    if (typeof payload.detail === "string") {
      message = payload.detail;
    } else if (payload.detail && typeof payload.detail.message === "string") {
      message = payload.detail.message;
    } else {
      message = `Request failed with ${response.status}`;
    }
    // Opaque server errors (a ``{"detail": "Internal Server Error"}`` body or a
    // non-JSON body that fell back to ``statusText``) carry no status code, so
    // append it: "Internal Server Error" -> "Internal Server Error (HTTP 500)".
    if (response.status >= 500 && !message.includes(String(response.status))) {
      message = `${message} (HTTP ${response.status})`;
    }
    logger.warn(`${method} ${url} ${response.status} ${elapsedMs}ms`, { request_id: requestId });
    throw new ApiError(message, response.status);
  }

  logger.debug(`← ${method} ${url} ${response.status} ${elapsedMs}ms`, { request_id: requestId });
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
