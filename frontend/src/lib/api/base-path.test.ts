/**
 * ADR-055 Spec 0 — base-path helper tests (FR-003/FR-004/FR-008, SC-004).
 *
 * Two concerns:
 *
 *  1. Helper normalization: `apiUrl`/`wsUrl` are the single frontend
 *     normalization point — trailing slashes, missing leading slashes, and
 *     doubled separators must not diverge, and the empty prefix is a strict
 *     identity no-op (FR-002).
 *
 *  2. The root-relative literal guard (FR-004 sweep, SC-004): no source file
 *     under `frontend/src` may feed a root-relative `"/api/..."` or `"/ws"`
 *     literal directly to a raw network primitive (`fetch`, `new WebSocket`,
 *     `new EventSource`). Literals are sanctioned ONLY as arguments to the
 *     helpers in this module's family (`apiFetch` in `core.ts`, `apiUrl`,
 *     `wsUrl`) — those helpers apply the configured mount prefix centrally,
 *     so a bare literal anywhere else is a request that silently breaks the
 *     moment the app is mounted under a prefix.
 */

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { apiUrl, getBasePath, resetBasePathCacheForTests, wsUrl } from "./base-path";
import { apiFetch } from "./core";

const SRC_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

function setBasePath(value: string | undefined): void {
  if (value === undefined) {
    delete window.__SCISTUDIO_BASE_PATH__;
  } else {
    window.__SCISTUDIO_BASE_PATH__ = value;
  }
  resetBasePathCacheForTests();
}

afterEach(() => {
  setBasePath(undefined);
  vi.unstubAllGlobals();
});

describe("getBasePath", () => {
  it("defaults to the empty prefix when nothing was injected", () => {
    setBasePath(undefined);
    expect(getBasePath()).toBe("");
  });

  it.each([
    ["", ""],
    ["/", ""],
    ["  ", ""],
    ["/user/alice/scistudio", "/user/alice/scistudio"],
    ["/user/alice/scistudio/", "/user/alice/scistudio"],
    ["user/alice/scistudio", "/user/alice/scistudio"],
    ["//a//b///", "/a/b"],
  ])("normalizes %j to %j", (raw, expected) => {
    setBasePath(raw);
    expect(getBasePath()).toBe(expected);
  });
});

describe("apiUrl", () => {
  it("is the identity function under the default empty prefix (FR-002)", () => {
    setBasePath(undefined);
    expect(apiUrl("/api/version")).toBe("/api/version");
    expect(apiUrl("/api/logs/stream?workflow_id=x")).toBe("/api/logs/stream?workflow_id=x");
  });

  it("joins the prefix with exactly one separating slash", () => {
    setBasePath("/user/alice/scistudio/");
    expect(apiUrl("/api/version")).toBe("/user/alice/scistudio/api/version");
    expect(apiUrl("api/version")).toBe("/user/alice/scistudio/api/version");
  });

  it("is idempotent for already-prefixed paths", () => {
    setBasePath("/p");
    expect(apiUrl("/p/api/version")).toBe("/p/api/version");
    expect(apiUrl(apiUrl("/api/version"))).toBe("/p/api/version");
  });

  it("does not mistake a near-colliding route for an already-prefixed path", () => {
    // The exact-boundary check (base + "/") must not misfire when the prefix
    // is a strict prefix-string of the route namespace. (A true collision
    // like base="/api" is rejected at configuration time by the backend's
    // normalize_root_path — that case can never reach this helper.)
    setBasePath("/ap");
    expect(apiUrl("/api/version")).toBe("/ap/api/version");
    expect(apiUrl("/ap/api/version")).toBe("/ap/api/version");
  });

  it("preserves query strings", () => {
    setBasePath("/p");
    expect(apiUrl("/api/logs/stream?workflow_id=w&block_id=b")).toBe(
      "/p/api/logs/stream?workflow_id=w&block_id=b",
    );
  });
});

describe("wsUrl", () => {
  it("builds a ws: URL from the page origin under the empty prefix", () => {
    setBasePath(undefined);
    expect(wsUrl("/ws")).toBe(`ws://${window.location.host}/ws`);
  });

  it("carries the configured prefix (FR-005)", () => {
    setBasePath("/user/alice/scistudio");
    expect(wsUrl("/ws")).toBe(`ws://${window.location.host}/user/alice/scistudio/ws`);
  });

  it('wsUrl("") yields origin + prefix for callers composing their own path', () => {
    setBasePath("/p");
    expect(wsUrl("")).toBe(`ws://${window.location.host}/p`);
    expect(wsUrl("")).not.toMatch(/\/$/);
  });
});

describe("apiFetch prefix integration", () => {
  function stubOkFetch(): ReturnType<typeof vi.fn> {
    const spy = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ok: true }),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  it("requests the unprefixed URL under the default root mount", async () => {
    const spy = stubOkFetch();
    await apiFetch("/api/version");
    expect(spy).toHaveBeenCalledWith("/api/version", expect.anything());
  });

  it("requests the prefixed URL when a mount prefix is configured", async () => {
    setBasePath("/user/alice/scistudio");
    const spy = stubOkFetch();
    await apiFetch("/api/version");
    expect(spy).toHaveBeenCalledWith("/user/alice/scistudio/api/version", expect.anything());
  });
});

describe("root-relative literal guard (FR-004 sweep, SC-004)", () => {
  // A root-relative "/api/..." or "/ws" literal handed straight to a raw
  // network primitive bypasses the base-path helpers and breaks under a
  // mounted prefix. \s covers a newline between the call and the literal.
  const FORBIDDEN = /(?:\bfetch|new\s+(?:WebSocket|EventSource))\s*\(\s*[`"']\/(?:api\/|ws)/;

  function* sourceFiles(dir: string): Generator<string> {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === "__tests__" || entry.name === "node_modules") continue;
        yield* sourceFiles(full);
      } else if (/\.(ts|tsx)$/.test(entry.name) && !entry.name.includes(".test.")) {
        yield full;
      }
    }
  }

  it("no raw fetch/WebSocket/EventSource call takes a root-relative API literal", () => {
    const violations: string[] = [];
    for (const file of sourceFiles(SRC_ROOT)) {
      // The helpers' own module is the one place where URL construction lives.
      if (path.basename(file) === "base-path.ts") continue;
      const text = readFileSync(file, "utf-8");
      if (FORBIDDEN.test(text)) {
        violations.push(path.relative(SRC_ROOT, file));
      }
    }
    expect(violations).toEqual([]);
  });
});
