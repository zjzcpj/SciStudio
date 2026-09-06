/**
 * ADR-055 Spec 1 (FR-009 / US3 / SC-003) — the registration lifecycle matrix:
 *
 * - capability absent at load, then appearing late (retry path);
 * - a superseded attempt resolving after a newer one (aborted, no stale
 *   success, no registrations from the obsolete attempt);
 * - a host that rejects one tool of N (rest register, failure logged with
 *   the tool name, batch not fatal);
 * - reconnect (re-registration aborts and replaces the previous attempt);
 * - dual host probing (`document.modelContext` and `navigator.modelContext`);
 * - bridge calls carry the session token header and the catalogue's
 *   project snapshot.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ProjectResponse } from "../types/api";
import type { ModelContext, ToolDefinition } from "./types";

const { apiFetchMock, loggerMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  loggerMock: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("../lib/api/core", () => ({
  apiFetch: apiFetchMock,
  webmcpSessionHeaders: () => ({ "X-SciStudio-WebMCP-Token": "test-token" }),
}));

vi.mock("../lib/logger", () => ({ logger: loggerMock }));

import {
  registerSciStudioTools,
  registerSciStudioToolsWithRetry,
  subscribeToProjectChanges,
} from "./register";

interface FakeHost extends ModelContext {
  tools: Map<string, ToolDefinition>;
}

function fakeHost(options?: { reject?: string[] }): FakeHost {
  const tools = new Map<string, ToolDefinition>();
  const host = {
    tools,
    registerTool: vi.fn(async (def: ToolDefinition, opts?: { signal?: AbortSignal }) => {
      if (options?.reject?.includes(def.name)) {
        throw new Error(`host refused ${def.name}`);
      }
      if (opts?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      opts?.signal?.addEventListener("abort", () => tools.delete(def.name));
      tools.set(def.name, def);
    }),
    getTools: vi.fn(async () => [...tools.values()]),
    executeTool: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
  return host as unknown as FakeHost;
}

function catalogue(names: string[], projectId: string | null = "project-1") {
  return {
    tools: names.map((name) => ({
      name,
      description: `${name} description`,
      inputSchema: { type: "object", properties: {} },
      category: "testing",
      mutation: "read" as const,
    })),
    context: { projectId },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  apiFetchMock.mockReset();
  for (const fn of Object.values(loggerMock)) fn.mockClear();
  delete document.modelContext;
  delete (navigator as { modelContext?: ModelContext }).modelContext;
});

describe("registerSciStudioTools", () => {
  it("reports zero registrations without erroring when the capability is absent", async () => {
    const count = await registerSciStudioTools();
    expect(count).toBe(0);
    expect(apiFetchMock).not.toHaveBeenCalled();
    expect(loggerMock.error).not.toHaveBeenCalled();
    expect(
      loggerMock.info.mock.calls.some((c) => String(c[0]).includes("WebMCP unavailable")),
    ).toBe(true);
  });

  it("probes navigator.modelContext when document lacks it (Chrome 149 form)", async () => {
    const host = fakeHost();
    (navigator as { modelContext?: ModelContext }).modelContext = host;
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha"]));
    const count = await registerSciStudioTools();
    expect(count).toBe(1);
    expect(host.tools.has("alpha")).toBe(true);
  });

  it("registers the catalogue and sends the session token + project snapshot", async () => {
    const host = fakeHost();
    document.modelContext = host;
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha", "beta"]));
    const count = await registerSciStudioTools();
    expect(count).toBe(2);

    // Catalogue fetch carried the token header (FR-006).
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/webmcp/tools",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-SciStudio-WebMCP-Token": "test-token" }),
      }),
    );

    // An execute callback posts name + arguments + the catalogue's project
    // snapshot (FR-005), with the token header.
    apiFetchMock.mockResolvedValueOnce({ content: [{ type: "text", text: "ok" }] });
    const tool = host.tools.get("alpha");
    expect(tool).toBeDefined();
    await tool!.execute({ q: 1 });
    const [, init] = apiFetchMock.mock.calls[1];
    expect(apiFetchMock.mock.calls[1][0]).toBe("/api/webmcp/call");
    expect(init.headers["X-SciStudio-WebMCP-Token"]).toBe("test-token");
    expect(JSON.parse(init.body)).toEqual({
      name: "alpha",
      arguments: { q: 1 },
      projectId: "project-1",
    });
  });

  it("a superseded attempt is aborted and never reports success or registers", async () => {
    const host = fakeHost();
    document.modelContext = host;

    const slowFetch = deferred<ReturnType<typeof catalogue>>();
    const fastFetch = deferred<ReturnType<typeof catalogue>>();
    apiFetchMock.mockReturnValueOnce(slowFetch.promise).mockReturnValueOnce(fastFetch.promise);

    // Attempt A starts and stalls on the catalogue fetch.
    const attemptA = registerSciStudioTools();
    // Attempt B starts before A's fetch resolves — A is now superseded.
    const attemptB = registerSciStudioTools();

    // A's fetch resolves AFTER B started; A must not register or succeed.
    slowFetch.resolve(catalogue(["alpha", "beta"]));
    fastFetch.resolve(catalogue(["alpha", "beta"]));
    const [countA, countB] = await Promise.all([attemptA, attemptB]);

    expect(countA).toBe(0);
    expect(countB).toBe(2);
    // Only B's batch reached the host.
    expect(host.registerTool).toHaveBeenCalledTimes(2);
    expect(host.tools.size).toBe(2);
    expect(loggerMock.info.mock.calls.some((c) => String(c[0]).includes("superseded"))).toBe(true);
  });

  it("a host rejecting one tool of N leaves the rest registered and logs the name", async () => {
    const host = fakeHost({ reject: ["beta"] });
    document.modelContext = host;
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha", "beta", "gamma"]));

    const count = await registerSciStudioTools();
    expect(count).toBe(2);
    expect([...host.tools.keys()].sort()).toEqual(["alpha", "gamma"]);
    expect(loggerMock.warn.mock.calls.some((c) => String(c[0]).includes("'beta'"))).toBe(true);
  });

  it("reconnect re-registration aborts and replaces the previous attempt", async () => {
    const hostA = fakeHost();
    document.modelContext = hostA;
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha"]));
    expect(await registerSciStudioTools()).toBe(1);
    expect(hostA.tools.size).toBe(1);

    // Reconnect: a (possibly new) host object, registration runs again.
    const hostB = fakeHost();
    document.modelContext = hostB;
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha", "beta"]));
    expect(await registerSciStudioTools()).toBe(2);

    // The aborted first attempt's registrations were revoked by the host.
    expect(hostA.tools.size).toBe(0);
    expect(hostB.tools.size).toBe(2);
  });
});

describe("registerSciStudioToolsWithRetry", () => {
  it("retries until a late-appearing capability registers", async () => {
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha"]));
    const pending = registerSciStudioToolsWithRetry({ maxAttempts: 10, delayMs: 5 });
    // The capability appears after the first probe(s).
    setTimeout(() => {
      document.modelContext = fakeHost();
    }, 20);
    const count = await pending;
    expect(count).toBe(1);
    const host = document.modelContext as FakeHost | undefined;
    expect(host?.tools.has("alpha")).toBe(true);
  });

  it("resolves to 0 without erroring when the capability never appears", async () => {
    const count = await registerSciStudioToolsWithRetry({ maxAttempts: 3, delayMs: 1 });
    expect(count).toBe(0);
    expect(apiFetchMock).not.toHaveBeenCalled();
    expect(loggerMock.error).not.toHaveBeenCalled();
  });
});

function fakeProject(id: string): ProjectResponse {
  return {
    id,
    name: id,
    description: "",
    path: `/tmp/${id}`,
    workflow_count: 0,
    workflows: [],
    current_workflow_id: null,
  };
}

describe("subscribeToProjectChanges (PR #2275 review P1)", () => {
  it("re-registers with a fresh project snapshot when the active project changes", async () => {
    const host = fakeHost();
    document.modelContext = host;
    // Boot registration happens before any project is open: snapshot is null.
    apiFetchMock.mockResolvedValueOnce(catalogue(["alpha"], null));
    expect(await registerSciStudioTools()).toBe(1);

    const { useAppStore } = await import("../store");
    useAppStore.getState().setCurrentProject(null);
    const unsubscribe = subscribeToProjectChanges();
    try {
      // User opens a project: the store change triggers a re-registration
      // whose catalogue fetch carries the new snapshot.
      apiFetchMock.mockResolvedValueOnce(catalogue(["alpha"], "project-9"));
      useAppStore.getState().setCurrentProject(fakeProject("project-9"));
      await vi.waitFor(() => {
        expect(host.registerTool).toHaveBeenCalledTimes(2);
      });

      // The refreshed execute closure posts the new projectId, so mutation
      // calls are not stuck at the stale boot-time snapshot.
      apiFetchMock.mockResolvedValueOnce({ content: [{ type: "text", text: "ok" }] });
      await host.tools.get("alpha")!.execute({});
      const [, init] = apiFetchMock.mock.calls[2];
      expect(JSON.parse(init.body as string).projectId).toBe("project-9");
    } finally {
      unsubscribe();
      useAppStore.getState().setCurrentProject(null);
    }
  });

  it("ignores unchanged project ids and does nothing without a host capability", async () => {
    const { useAppStore } = await import("../store");
    useAppStore.getState().setCurrentProject(null);
    const unsubscribe = subscribeToProjectChanges();
    try {
      // No host capability (beforeEach cleared both surfaces): a project
      // change must not fetch or register anything.
      useAppStore.getState().setCurrentProject(fakeProject("project-7"));
      // Same id again: no change, no trigger.
      useAppStore.getState().setCurrentProject(fakeProject("project-7"));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(apiFetchMock).not.toHaveBeenCalled();
    } finally {
      unsubscribe();
      useAppStore.getState().setCurrentProject(null);
    }
  });
});
