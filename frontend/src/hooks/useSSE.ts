import { useEffect, useState } from "react";

import { apiUrl } from "../lib/api/base-path";
import type { LogEntry } from "../types/api";
import { useAppStore } from "../store";
import {
  RECONNECT_INITIAL_DELAY_MS,
  nextBackoffDelay,
  type ConnectionStatus,
} from "./connectionState";

export interface LogStreamState {
  connected: boolean;
  /** #177: richer lifecycle so the UI can show reconnecting. */
  status: ConnectionStatus;
}

/**
 * Subscribe to the server-sent-event log stream for a workflow / block.
 *
 * #177: the EventSource is recreated with exponential backoff after any
 * ``onerror`` so a dropped connection (Wi-Fi handoff, server restart,
 * laptop sleep/wake) does not silently stop log streaming. The backoff
 * resets to :data:`RECONNECT_INITIAL_DELAY_MS` after a successful open.
 *
 * Note: the browser's native ``EventSource`` already auto-reconnects on
 * transient errors, but only while the connection object lives and only
 * for some error classes; it does not surface a status the UI can show
 * and it can get stuck after the page is backgrounded. We manage the
 * lifecycle explicitly so the behaviour is observable and testable.
 */
export function useLogStream(workflowId: string | null, blockId: string | null): LogStreamState {
  const appendLog = useAppStore((state) => state.appendLog);
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");

  useEffect(() => {
    if (!workflowId) {
      setStatus("disconnected");
      return undefined;
    }

    let cancelled = false;
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let delay = RECONNECT_INITIAL_DELAY_MS;

    const params = new URLSearchParams({ workflow_id: workflowId });
    if (blockId) {
      params.set("block_id", blockId);
    }

    const scheduleReconnect = () => {
      if (cancelled) return;
      setStatus("reconnecting");
      retryTimer = setTimeout(() => {
        delay = nextBackoffDelay(delay);
        connect();
      }, delay);
    };

    function connect() {
      if (cancelled) return;
      // ADR-055 Spec 0 (FR-004/FR-005): EventSource has no apiFetch
      // equivalent, so the URL goes through the base-path source of truth.
      source = new EventSource(apiUrl(`/api/logs/stream?${params.toString()}`));
      source.onopen = () => {
        if (cancelled) return;
        delay = RECONNECT_INITIAL_DELAY_MS;
        setStatus("connected");
      };
      source.onerror = () => {
        if (cancelled) return;
        // EventSource fires onerror for both transient blips and hard
        // failures. Close our handle and schedule a managed retry so the
        // status is observable and the backoff is honoured.
        source?.close();
        source = null;
        scheduleReconnect();
      };
      source.addEventListener("log", (event) => {
        const message = JSON.parse((event as MessageEvent<string>).data) as LogEntry;
        appendLog(message);
      });
    }

    setStatus("connecting");
    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      source?.close();
      setStatus("disconnected");
    };
  }, [appendLog, blockId, workflowId]);

  return { connected: status === "connected", status };
}
