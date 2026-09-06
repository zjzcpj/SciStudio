/**
 * Frontend logging (#1741).
 *
 * A tiny logger that:
 *  - writes to the browser console (human-readable),
 *  - keeps a bounded in-memory ring buffer for export,
 *  - batches records at/above a reflux threshold to the backend
 *    (`POST /api/client-logs`) so a beta tester's frontend errors land on disk
 *    where developers can see them — no third-party telemetry.
 *
 * Use `logger.{debug,info,warn,error}` at user-action and API boundaries. The
 * global error handlers and the React ErrorBoundary route uncaught failures
 * here too, so the frontend is no longer an observability black hole.
 */

import { apiUrl } from "./api/base-path";

export type LogLevel = "debug" | "info" | "warn" | "error";

export interface ClientLogRecord {
  level: LogLevel;
  message: string;
  ts: string;
  url?: string;
  request_id?: string;
  context?: Record<string, unknown>;
}

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

const RING_LIMIT = 500;
const REFLUX_THRESHOLD: LogLevel = "warn"; // reflux warn + error to the backend
const FLUSH_INTERVAL_MS = 4000;
const MAX_BATCH = 50;

const ring: ClientLogRecord[] = [];
let pending: ClientLogRecord[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;

function nowIso(): string {
  return new Date().toISOString();
}

function pushRing(record: ClientLogRecord): void {
  ring.push(record);
  if (ring.length > RING_LIMIT) {
    ring.splice(0, ring.length - RING_LIMIT);
  }
}

function scheduleFlush(): void {
  if (flushTimer === null) {
    flushTimer = setTimeout(() => void flush(), FLUSH_INTERVAL_MS);
  }
}

async function flush(): Promise<void> {
  flushTimer = null;
  if (pending.length === 0) return;
  const batch = pending.slice(0, MAX_BATCH);
  pending = pending.slice(MAX_BATCH);
  try {
    // ADR-055 Spec 0 (FR-004): raw fetch (apiFetch would be a circular
    // import), but the URL still goes through the base-path source of truth.
    await fetch(apiUrl("/api/client-logs"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: batch }),
      keepalive: true,
    });
  } catch {
    // Backend unreachable; the records remain in the ring buffer so the
    // "Export logs" button can still ship them.
  }
  if (pending.length > 0) scheduleFlush();
}

function emit(level: LogLevel, message: string, context?: Record<string, unknown>): void {
  const record: ClientLogRecord = {
    level,
    message,
    ts: nowIso(),
    url: typeof window !== "undefined" ? window.location?.href : undefined,
    request_id: typeof context?.request_id === "string" ? context.request_id : undefined,
    context,
  };
  pushRing(record);

  /* eslint-disable no-console -- this IS the console logging boundary */
  const sink =
    level === "error"
      ? console.error
      : level === "warn"
        ? console.warn
        : level === "debug"
          ? console.debug
          : console.info;
  sink(`[scistudio] ${message}`, context ?? "");
  /* eslint-enable no-console */

  if (LEVEL_ORDER[level] >= LEVEL_ORDER[REFLUX_THRESHOLD]) {
    pending.push(record);
    scheduleFlush();
  }
}

export const logger = {
  debug: (message: string, context?: Record<string, unknown>) => emit("debug", message, context),
  info: (message: string, context?: Record<string, unknown>) => emit("info", message, context),
  warn: (message: string, context?: Record<string, unknown>) => emit("warn", message, context),
  error: (message: string, context?: Record<string, unknown>) => emit("error", message, context),
};

/** Return a copy of the in-memory ring buffer (for export / debugging). */
export function getLogBuffer(): ClientLogRecord[] {
  return [...ring];
}

/** Install global `error` / `unhandledrejection` handlers + a flush-on-unload. */
export function installGlobalErrorHandlers(): void {
  if (typeof window === "undefined") return;
  window.addEventListener("error", (event) => {
    logger.error(`uncaught error: ${event.message}`, {
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
      stack: event.error?.stack,
    });
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason as { message?: string; stack?: string } | undefined;
    logger.error(`unhandled rejection: ${reason?.message ?? String(event.reason)}`, {
      stack: reason?.stack,
    });
  });
  window.addEventListener("beforeunload", () => void flush());
}

/** Trigger a browser download of a blob under the given filename. */
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** Render frontend records as a human-readable .log (owner: no JSON files). */
function formatRecordsAsLog(records: ClientLogRecord[]): string {
  const body = records
    .map((r) => {
      const extras: string[] = [];
      if (r.request_id) extras.push(`req=${r.request_id}`);
      if (r.url) extras.push(`url=${r.url}`);
      if (r.context) extras.push(`context=${JSON.stringify(r.context)}`);
      const suffix = extras.length ? `  [${extras.join(" ")}]` : "";
      return `${r.ts} ${r.level.toUpperCase()} frontend ${r.message}${suffix}`;
    })
    .join("\n");
  return records.length ? `${body}\n` : "";
}

/**
 * Open the native OS save dialog and return the chosen path.
 *
 * Called via a direct `fetch` (not the filesystem api module) to keep this
 * low-level logger free of the `lib/api` import graph (`apiFetch` imports the
 * logger). Returns `available: false` when the platform native dialog could not
 * run (the route 500s); an empty `path` with `available: true` means the user
 * cancelled.
 */
async function openNativeSaveDialog(
  defaultFilename: string,
): Promise<{ path: string | null; available: boolean }> {
  try {
    const response = await fetch(apiUrl("/api/filesystem/native-dialog"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // prefer_home: a diagnostic bundle is a machine artifact, not a project
      // file, so it saves from home rather than the active project root (#1915).
      body: JSON.stringify({
        mode: "save_file",
        default_filename: defaultFilename,
        prefer_home: true,
      }),
    });
    if (!response.ok) return { path: null, available: false };
    const data = (await response.json()) as { paths?: string[]; available?: boolean };
    return { path: data.paths?.[0] ?? null, available: data.available !== false };
  } catch {
    return { path: null, available: false };
  }
}

/** Browser-download fallback: stream the bundle blob (or a plain `.log`). */
async function downloadBundleViaBrowser(records: ClientLogRecord[]): Promise<void> {
  try {
    const response = await fetch(apiUrl("/api/diagnostics/bundle"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records }),
    });
    if (!response.ok) throw new Error(`bundle request failed: ${response.status}`);
    downloadBlob(await response.blob(), "scistudio-diagnostics.zip");
    logger.info("diagnostic bundle exported");
  } catch (error) {
    logger.warn(`backend diagnostic bundle unavailable: ${String(error)}`);
    downloadBlob(
      new Blob([formatRecordsAsLog(records)], { type: "text/plain" }),
      "scistudio-frontend-logs.log",
    );
  }
}

/**
 * Export diagnostics for a bug report as a SINGLE file.
 *
 * Native-dialog-first (#1760 bug2): the OS save dialog is shown *immediately*
 * so it no longer waits for the (potentially large) bundle to be built. When a
 * destination is chosen the backend writes the bundle straight to that path,
 * building + DEFLATE-compressing the rotating logs off its request event loop.
 *
 * Fallbacks mirror the preview-export contract (#1716): when the native dialog
 * did run but returned no path the user cancelled — do nothing (no second
 * dialog). Only when the native dialog is unavailable (browser context) do we
 * fall back to a single browser download, which bundles the ring buffer as a
 * human-readable `frontend-logs.log` alongside the backend logs + environment +
 * run logs (a single download avoids the browser's block on consecutive
 * auto-downloads).
 */
export async function exportDiagnosticBundle(): Promise<void> {
  const records = getLogBuffer();
  const dialog = await openNativeSaveDialog("scistudio-diagnostics.zip");
  if (dialog.path) {
    try {
      const response = await fetch(apiUrl("/api/diagnostics/bundle"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records, path: dialog.path }),
      });
      if (!response.ok) throw new Error(`bundle write failed: ${response.status}`);
      logger.info("diagnostic bundle exported");
    } catch (error) {
      // The user picked a path but the server-side write failed; fall back to a
      // browser download so the bug report is not lost.
      logger.warn(`diagnostic bundle write failed: ${String(error)}`);
      await downloadBundleViaBrowser(records);
    }
    return;
  }
  if (!dialog.available) {
    await downloadBundleViaBrowser(records);
  }
}
