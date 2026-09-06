/**
 * ADR-055 Spec 0 — the single frontend base-path source of truth
 * (FR-003/FR-004/FR-005/FR-008).
 *
 * The backend can be mounted below a non-root path (e.g. a JupyterHub user
 * route such as `/user/alice/scistudio`). The serving backend templates the
 * prefix into the served `index.html` as `window.__SCISTUDIO_BASE_PATH__`
 * (see `src/scistudio/api/spa.py`); this module reads that global and builds
 * every API fetch URL, WebSocket URL, and same-origin asset URL from it.
 *
 * Rules for callers:
 *  - NEVER concatenate the prefix by hand. `apiUrl`/`wsUrl` are the only
 *    normalization points (FR-008); they are idempotent, so passing an
 *    already-prefixed path is safe.
 *  - Root-relative `"/api/..."` / `"/ws"` literals belong to this module's
 *    consumers only as *arguments* to `apiFetch`/`apiUrl`/`wsUrl` — never as
 *    direct `fetch()`/`new WebSocket()`/`new EventSource()` targets.
 *    `base-path.test.ts` guards that contract.
 *  - The default empty prefix is a strict no-op: every helper returns its
 *    input unchanged (modulo normalization), so desktop and local-browser
 *    behavior is byte-identical (FR-002).
 */

declare global {
  interface Window {
    /** Injected by the backend into the served index.html (FR-003). */
    __SCISTUDIO_BASE_PATH__?: string;
  }
}

/**
 * Normalize a configured prefix to `""` (root mount) or `"/prefix"` — exactly
 * one leading slash, no trailing slash, no doubled separators — so `/prefix`,
 * `/prefix/`, and `//prefix` cannot diverge (spec edge cases).
 */
function normalizeBasePath(raw: string): string {
  const segments = raw.trim().split("/").filter(Boolean);
  return segments.length > 0 ? `/${segments.join("/")}` : "";
}

let cachedBasePath: string | null = null;

/**
 * The configured mount prefix: `""` at the root (default), else `"/prefix"`.
 * Read once and cached; the value is fixed for the lifetime of the page
 * because it comes from the document that served the page.
 */
export function getBasePath(): string {
  if (cachedBasePath === null) {
    const raw =
      typeof window !== "undefined" && typeof window.__SCISTUDIO_BASE_PATH__ === "string"
        ? window.__SCISTUDIO_BASE_PATH__
        : "";
    cachedBasePath = normalizeBasePath(raw);
  }
  return cachedBasePath;
}

/** Test-only hook: drop the cached prefix so a test can re-inject the global. */
export function resetBasePathCacheForTests(): void {
  cachedBasePath = null;
}

/** Join base + path with exactly one separating slash; idempotent.
 *
 * The "already prefixed" check is an exact boundary match (`=== base` or
 * `base + "/"`), and it is unambiguous because the backend rejects prefixes
 * whose first segment collides with the `/api`/`/ws` route namespaces at
 * configuration time (`normalize_root_path` in `api/app.py`): with a legal
 * prefix like `/p`, no API route path (`/api/...`, `/ws`) can start with
 * `/p/`, so a match can only mean the prefix was already applied. */
function joinBasePath(base: string, path: string): string {
  if (!base) return path;
  if (!path) return base;
  if (path === base || path.startsWith(`${base}/`)) return path;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * Build a same-origin URL for an API route or backend-served asset.
 * Accepts paths with or without a leading slash and returns the input
 * unchanged when the prefix is empty or already applied.
 */
export function apiUrl(path: string): string {
  return joinBasePath(getBasePath(), path);
}

/**
 * Build a WebSocket URL for a backend route, honoring the page's scheme
 * (`http:` -> `ws:`, `https:` -> `wss:`), host, and the configured prefix.
 * `wsUrl("")` returns just `ws(s)://<host><prefix>` for callers that compose
 * their own path+query. Falls back to `ws://localhost` outside a browser
 * (unit tests).
 */
export function wsUrl(path: string): string {
  let origin = "ws://localhost";
  if (typeof window !== "undefined" && window.location) {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    origin = `${protocol}//${window.location.host}`;
  }
  return `${origin}${apiUrl(path)}`;
}
