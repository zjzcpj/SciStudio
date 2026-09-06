/**
 * ADR-034 Phase 1.3: WebSocket hook for the PTY-backed embedded terminal.
 *
 * Protocol (LOCKED — backend uses the same spec):
 *   URL: ws://host/api/ai/pty/{tab_id}
 *          ?project_dir=<urlencoded_abs_path>
 *          &provider=<agent provider key | user-terminal>
 *          &dangerous=<true|false>
 *
 *   ADR-034 FR-020a: the agent provider keys accepted here come from the
 *   backend provider registry and are validated server-side against it. The
 *   frontend does not enumerate them.
 *
 *   Client -> Server (JSON, one frame per WS message):
 *     {type: "stdin", data: "<utf-8 string>"}
 *     {type: "resize", cols: <int>, rows: <int>}
 *
 *   Server -> Client (JSON):
 *     {type: "stdout", data: "<utf-8 string>"}
 *     {type: "exit", code: <int>}
 *     {type: "error", message: "<string>"}
 *
 * No auto-reconnect. PTY exit is terminal; the frontend exposes a Reopen
 * button that re-mounts this hook with fresh params.
 */
import { useCallback, useEffect, useRef } from "react";

import { wsUrl } from "../../../lib/api/base-path";
import type { TerminalProvider } from "../../../store/types";

export type PtyClientFrame =
  | { type: "stdin"; data: string }
  | { type: "resize"; cols: number; rows: number };

export type PtyServerFrame =
  | { type: "stdout"; data: string }
  | { type: "exit"; code: number }
  | { type: "error"; message: string };

export interface UsePtyWebSocketParams {
  tabId: string;
  projectDir: string | null;
  /** ADR-034 FR-020 — imported from the single source; not redeclared here. */
  provider: TerminalProvider;
  dangerous: boolean;
  /** Delay launch until the terminal has enough layout state to spawn cleanly. */
  enabled?: boolean;
  /** Optional initial PTY dimensions, sent in the spawn handshake. */
  initialCols?: number | null;
  initialRows?: number | null;
  /** Called for each parsed server-side frame. */
  onMessage: (frame: PtyServerFrame) => void;
  /** Called once when the underlying socket opens. */
  onOpen?: () => void;
  /** Called if the WS closes unexpectedly (before an `exit` frame). */
  onClose?: (ev: CloseEvent) => void;
}

export interface UsePtyWebSocketResult {
  send: (frame: PtyClientFrame) => void;
  /** True while the WS is in OPEN state. */
  readonly readyStateRef: React.MutableRefObject<number>;
}

/** Build the PTY WS URL with all required query params, properly encoded. */
export function buildPtyUrl({
  tabId,
  projectDir,
  provider,
  dangerous,
  cols,
  rows,
  baseOrigin,
}: {
  tabId: string;
  projectDir: string;
  provider: string;
  dangerous: boolean;
  cols?: number | null;
  rows?: number | null;
  baseOrigin?: string;
}): string {
  const params = new URLSearchParams({
    project_dir: projectDir,
    provider,
    dangerous: dangerous ? "true" : "false",
  });
  if (Number.isFinite(cols) && cols && cols > 0) {
    params.set("cols", String(Math.trunc(cols)));
  }
  if (Number.isFinite(rows) && rows && rows > 0) {
    params.set("rows", String(Math.trunc(rows)));
  }
  const path = `/api/ai/pty/${encodeURIComponent(tabId)}?${params.toString()}`;
  // baseOrigin override is for unit tests; it is already a complete base, so
  // the path is appended verbatim there. Production goes through wsUrl so
  // the configured mount prefix rides along (ADR-055 Spec 0, FR-005).
  if (baseOrigin) return `${baseOrigin}${path}`;
  return wsUrl(path);
}

/**
 * Open and manage a single PTY WebSocket for the lifetime of the calling
 * component. The hook opens the socket on mount and closes it on unmount.
 *
 * If `projectDir` is null the hook does NOT open a socket (caller is in a
 * state where it cannot launch). When params change identity, the hook
 * closes the old socket and opens a new one — caller is responsible for
 * not flipping params during a live session.
 */
export function usePtyWebSocket(params: UsePtyWebSocketParams): UsePtyWebSocketResult {
  const {
    tabId,
    projectDir,
    provider,
    dangerous,
    enabled = true,
    initialCols,
    initialRows,
    onMessage,
    onOpen,
    onClose,
  } = params;

  const wsRef = useRef<WebSocket | null>(null);
  const readyStateRef = useRef<number>(WebSocket.CLOSED);
  // Keep callbacks in refs so identity changes do not force WS reconnection.
  const onMessageRef = useRef(onMessage);
  const onOpenRef = useRef(onOpen);
  const onCloseRef = useRef(onClose);
  onMessageRef.current = onMessage;
  onOpenRef.current = onOpen;
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!enabled || !projectDir) {
      // No working dir -> cannot launch a PTY. Caller is in setup or closed
      // state; do nothing.
      return undefined;
    }
    // `disposed` guards against late-firing onerror/onclose after an
    // intentional cleanup (StrictMode double-mount in dev, or any caller-
    // triggered teardown). Without this guard, the browser's close-side
    // `onerror` propagates upward as a fake "WebSocket error" frame and
    // tears the user's tab into the closed state on every mount.
    let disposed = false;
    let ws: WebSocket | null = null;
    const url = buildPtyUrl({
      tabId,
      projectDir,
      provider,
      dangerous,
      cols: initialCols,
      rows: initialRows,
    });
    const connectTimer = window.setTimeout(() => {
      if (disposed) return;
      ws = new WebSocket(url);
      wsRef.current = ws;
      readyStateRef.current = ws.readyState;

      ws.onopen = () => {
        if (disposed || !ws) return;
        readyStateRef.current = ws.readyState;
        onOpenRef.current?.();
      };
      ws.onmessage = (ev) => {
        if (disposed) return;
        // Server always sends JSON-encoded text frames. Anything else is a
        // protocol violation and surfaced as an error frame so the UI shows it.
        try {
          const data = typeof ev.data === "string" ? ev.data : "";
          if (!data) return;
          const frame = JSON.parse(data) as PtyServerFrame;
          onMessageRef.current(frame);
        } catch (err) {
          onMessageRef.current({
            type: "error",
            message: `Malformed server frame: ${(err as Error).message}`,
          });
        }
      };
      ws.onerror = () => {
        if (disposed) return;
        // Browser WebSocket errors are intentionally opaque and are normally
        // followed by close. Let the close handler surface the actionable code
        // instead of replacing the real PTY/server error with "WebSocket error".
      };
      ws.onclose = (ev) => {
        if (disposed || !ws) return;
        readyStateRef.current = ws.readyState;
        onCloseRef.current?.(ev);
      };
    }, 0);

    return () => {
      disposed = true;
      window.clearTimeout(connectTimer);
      readyStateRef.current = WebSocket.CLOSING;
      if (ws) {
        try {
          ws.close();
        } catch {
          // Already closed.
        }
      }
      wsRef.current = null;
    };
  }, [tabId, projectDir, provider, dangerous, enabled, initialCols, initialRows]);

  const send = useCallback((frame: PtyClientFrame) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      // Drop the frame silently — the upstream UI will recover on the next
      // PTY exit/error event. We never queue, to avoid replaying stale
      // keystrokes onto a reconnected PTY.
      return;
    }
    try {
      ws.send(JSON.stringify(frame));
    } catch {
      // Buffer overflow or socket closed mid-send; ignore.
    }
  }, []);

  return { send, readyStateRef };
}
