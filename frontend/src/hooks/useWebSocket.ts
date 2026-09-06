import { useEffect, useState } from "react";

import { wsUrl } from "../lib/api/base-path";
import { useAppStore } from "../store";
import type { WorkflowEventMessage } from "../types/api";
import {
  RECONNECT_INITIAL_DELAY_MS,
  nextBackoffDelay,
  type ConnectionStatus,
} from "./connectionState";
import { dispatchWorkflowEvent } from "./useWebSocket.parts/dispatchEvent";

/** Heartbeat interval (#177): ping the server to detect stale sockets. */
const HEARTBEAT_INTERVAL_MS = 30000;
const HEARTBEAT_TIMEOUT_MS = 10000;

/** Ref-holder for the active WebSocket so components can send messages. */
let _activeSocket: WebSocket | null = null;

/** Send a JSON message over the active WebSocket connection.
 *  Returns false if the socket is not connected. */
export function sendWebSocketMessage(message: Record<string, unknown>): boolean {
  if (_activeSocket && _activeSocket.readyState === WebSocket.OPEN) {
    _activeSocket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

export interface WorkflowWebSocketState {
  connected: boolean;
  /** #177: richer lifecycle so the UI can show reconnecting. */
  status: ConnectionStatus;
}

/**
 * Maintain the workflow WebSocket connection.
 *
 * #177: the socket is recreated with exponential backoff after any
 * ``onclose`` / ``onerror`` so a dropped connection does not silently
 * stop block-state updates and log streaming. A 30s ping heartbeat
 * detects half-open sockets (e.g. laptop sleep/wake) that never fire a
 * close event; if no pong is observed before the next interval the
 * socket is force-closed, which triggers the reconnect path. The
 * backoff resets to :data:`RECONNECT_INITIAL_DELAY_MS` after a
 * successful open.
 *
 * The returned ``status`` is component state for the UI indicator only;
 * it is not runtime truth and is never persisted to the store.
 */
export function useWorkflowWebSocket(enabled: boolean): WorkflowWebSocketState {
  const consumeEvent = useAppStore((state) => state.consumeEvent);
  const appendLog = useAppStore((state) => state.appendLog);
  const setInteractivePrompt = useAppStore((state) => state.setInteractivePrompt);
  const setWorkflow = useAppStore((state) => state.setWorkflow);
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");

  useEffect(() => {
    if (!enabled) {
      setStatus("disconnected");
      return undefined;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    let heartbeatTimeoutTimer: ReturnType<typeof setTimeout> | null = null;
    let delay = RECONNECT_INITIAL_DELAY_MS;
    let lastInboundAt = 0;

    // ADR-055 Spec 0 (FR-005): the WS URL comes from the single base-path
    // source of truth so a prefixed mount dials `<prefix>/ws`.
    const url = wsUrl("/ws");

    const clearHeartbeat = () => {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
      if (heartbeatTimeoutTimer) {
        clearTimeout(heartbeatTimeoutTimer);
        heartbeatTimeoutTimer = null;
      }
    };

    // #177 heartbeat. Browsers cannot send WebSocket protocol pings, so
    // SciStudio uses an application-level ping/pong frame. Any inbound frame
    // after a ping proves the socket is live. If no frame arrives before the
    // timeout, the hook treats the still-OPEN socket as stale and reconnects.
    const startHeartbeat = () => {
      clearHeartbeat();
      heartbeatTimer = setInterval(() => {
        if (cancelled || !socket) return;
        if (socket.readyState !== WebSocket.OPEN) {
          // Stale or half-open socket: drive the reconnect now.
          handleClosed();
          return;
        }
        if (heartbeatTimeoutTimer) {
          clearTimeout(heartbeatTimeoutTimer);
          heartbeatTimeoutTimer = null;
        }
        const pingSentAt = Date.now();
        try {
          socket.send(JSON.stringify({ type: "ping" }));
        } catch {
          handleClosed();
          return;
        }
        heartbeatTimeoutTimer = setTimeout(() => {
          if (cancelled || !socket) return;
          if (lastInboundAt >= pingSentAt) return;
          socket.close();
          handleClosed();
        }, HEARTBEAT_TIMEOUT_MS);
      }, HEARTBEAT_INTERVAL_MS);
    };

    const scheduleReconnect = () => {
      if (cancelled || retryTimer) return;
      setStatus("reconnecting");
      retryTimer = setTimeout(() => {
        retryTimer = null;
        delay = nextBackoffDelay(delay);
        connect();
      }, delay);
    };

    const handleClosed = () => {
      clearHeartbeat();
      if (_activeSocket === socket) _activeSocket = null;
      socket = null;
      if (cancelled) return;
      scheduleReconnect();
    };

    function connect() {
      if (cancelled) return;
      socket = new WebSocket(url);
      _activeSocket = socket;

      socket.onopen = () => {
        if (cancelled) return;
        delay = RECONNECT_INITIAL_DELAY_MS;
        lastInboundAt = Date.now();
        setStatus("connected");
        startHeartbeat();
      };
      socket.onclose = handleClosed;
      socket.onerror = () => {
        // Defer to onclose for reconnect; browsers fire error then close.
        // If error fires without a subsequent close, the heartbeat will
        // eventually force one.
        if (socket && socket.readyState === WebSocket.CLOSED) {
          handleClosed();
        }
      };
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data) as WorkflowEventMessage;
        lastInboundAt = Date.now();
        if (payload.type === "pong") return;
        const consumed = dispatchWorkflowEvent(payload, {
          appendLog,
          setInteractivePrompt,
          setWorkflow,
        });
        if (consumed) return;

        consumeEvent(payload);

        // The Logs unread badge is coupled to ``appendLog`` / ``consumeEvent``
        // itself (executionSlice) so it tracks actual rendered rows. The
        // Problems tab was removed in the same change set; block_error rows
        // surface in the Logs panel (filterable via the level selector) and
        // as the inline error badge on the BlockNode itself.
      };
    }

    setStatus("connecting");
    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      clearHeartbeat();
      if (socket) {
        // Detach handlers so teardown does not schedule a reconnect.
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
      if (_activeSocket === socket) _activeSocket = null;
      socket = null;
      setStatus("disconnected");
    };
  }, [appendLog, consumeEvent, enabled, setInteractivePrompt, setWorkflow]);

  return { connected: status === "connected", status };
}
