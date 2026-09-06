/**
 * Lint pipeline for CodeEditor (#871 + ADR-036 §3.7).
 *
 * Extracted in #1413 so the main CodeEditor function stays under 150 lines.
 *
 * The hook exposes a stable `scheduleLint(content, language)` callback that:
 *   - Debounces by 600 ms.
 *   - Skips the request unless `language === "python"`.
 *   - Posts to `/api/lint/python` and translates the response into Monaco
 *     markers via `setModelMarkers(model, "ruff", ...)`.
 *   - Drops stale responses (monotonic request id).
 */
import { useCallback, useEffect, useRef } from "react";

import { apiFetch, JSON_HEADERS } from "../../lib/api/core";

interface LintDiagnostic {
  line: number;
  column: number;
  end_line: number;
  end_column: number;
  code: string;
  severity: "error" | "warning" | "info" | string;
  message: string;
}

interface LintResponse {
  diagnostics: LintDiagnostic[];
  note?: string;
}

const LINT_DEBOUNCE_MS = 600;

/**
 * Map a ruff severity to Monaco's MarkerSeverity numeric enum:
 *   Hint = 1, Info = 2, Warning = 4, Error = 8.
 * We avoid importing monaco at module scope so the editor stays lazy.
 */
function severityToMarkerSeverity(severity: string): number {
  switch (severity) {
    case "error":
      return 8;
    case "warning":
      return 4;
    case "info":
      return 2;
    default:
      return 1;
  }
}

export interface UseLintMarkersOpts {
  filePath: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  editorRef: React.MutableRefObject<any>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  monacoRef: React.MutableRefObject<any>;
}

export function useLintMarkers({ filePath, editorRef, monacoRef }: UseLintMarkersOpts) {
  const lintTimerRef = useRef<number | null>(null);
  // #871: monotonic request id; responses whose id is below the latest
  // in-flight value are stale (a newer lint kicked off while we awaited)
  // and must be discarded so out-of-order arrivals don't repaint stale
  // markers.
  const lintRequestIdRef = useRef(0);

  const applyMarkers = useCallback(
    (diagnostics: LintDiagnostic[]) => {
      const editor = editorRef.current;
      const monaco = monacoRef.current;
      if (!editor || !monaco) return;
      const model = editor.getModel();
      if (!model) return;
      const markers = diagnostics.map((d) => ({
        severity: severityToMarkerSeverity(d.severity),
        startLineNumber: d.line,
        startColumn: d.column,
        endLineNumber: d.end_line,
        endColumn: d.end_column,
        message: d.message,
        code: d.code,
        source: "ruff",
      }));
      try {
        monaco.editor.setModelMarkers(model, "ruff", markers);
      } catch {
        /* ignore */
      }
    },
    [editorRef, monacoRef],
  );

  const runLint = useCallback(
    async (content: string) => {
      const requestId = ++lintRequestIdRef.current;
      try {
        // apiFetch routes through the base-path helper (ADR-055 Spec 0 FR-004)
        // and throws on non-2xx; lint failures are silently ignored below.
        const payload = await apiFetch<LintResponse>("/api/lint/python", {
          method: "POST",
          headers: JSON_HEADERS,
          body: JSON.stringify({ content, filename: filePath }),
        });
        // #871: drop the response if a newer lint request has fired while we
        // awaited. Without this guard, a slow response can repaint stale
        // diagnostics over the latest ones.
        if (requestId !== lintRequestIdRef.current) return;
        applyMarkers(payload.diagnostics ?? []);
      } catch {
        // Silent: lint is a best-effort UX affordance, not load-bearing.
      }
    },
    [applyMarkers, filePath],
  );

  const scheduleLint = useCallback(
    (content: string, language: string) => {
      if (language !== "python") return;
      if (lintTimerRef.current !== null) {
        window.clearTimeout(lintTimerRef.current);
      }
      lintTimerRef.current = window.setTimeout(() => {
        lintTimerRef.current = null;
        void runLint(content);
      }, LINT_DEBOUNCE_MS);
    },
    [runLint],
  );

  // Cleanup: cancel any pending lint timer on unmount.
  useEffect(() => {
    return () => {
      if (lintTimerRef.current !== null) {
        window.clearTimeout(lintTimerRef.current);
        lintTimerRef.current = null;
      }
    };
  }, []);

  return { scheduleLint };
}
