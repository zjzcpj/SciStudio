/**
 * ADR-051 — same-origin dynamic *interactive panel* module loading.
 *
 * This mirrors the ADR-048 previewer loader
 * ({@link ../../components/DataPreview.parts/dynamicPreviewer.ts}) but for the
 * interactive-block panel host. A package-provided interactive block ships its
 * window as an ESM module served by the backend under a same-origin, backend-
 * relative `module_url` (e.g. `/api/interactive/panels/<panel_id>/<file>.js`).
 *
 * {@link mountDynamicPanel} validates the manifest, dynamically `import()`s the
 * module from a *same-origin* URL only, reads the named export, checks it is a
 * {@link PanelModule} with a compatible API version, injects any same-origin
 * CSS the manifest declares, and mounts it into a host-owned container with a
 * constrained {@link PanelHostApi}.
 *
 * Any failure (remote URL, missing/empty url, import error, missing/invalid
 * export, version mismatch, mount throw) resolves to a typed {@link LoadFailure}
 * so the caller ({@link ./DynamicPanel.tsx}) can render a visible error surface
 * with a Cancel exit instead of leaving the user stuck on a paused block. This
 * module NEVER throws.
 */

import { apiUrl } from "../../lib/api/base-path";
import type { PanelManifestDescriptor } from "../../store/types";

/**
 * Current panel host-module contract version. Bump only on a breaking change to
 * {@link PanelHostApi} / {@link PanelModule}. A loaded module whose declared
 * `apiVersion` (or whose manifest `api_version`) is incompatible is not mounted.
 */
export const PANEL_HOST_API_VERSION = "1" as const;

/**
 * The constrained API a dynamic panel module receives on mount. Unlike the core
 * panels (which call `onConfirm`/`onCancel` directly), a package panel drives
 * the exact same run-scoped flow through {@link PanelHostApi.confirm} /
 * {@link PanelHostApi.cancel}. The host owns the WebSocket frames; the module
 * only supplies its JSON-safe decision.
 */
export interface PanelHostApi {
  /** Host contract version. Modules may assert compatibility against this. */
  readonly apiVersion: string;
  /** The paused block this panel is resolving. Display/identity only. */
  readonly blockId: string;
  /** The block-built, window-sized JSON view the panel renders from. */
  readonly panelPayload: Record<string, unknown>;
  /** Submit the panel's JSON-safe decision (drives `interactive_complete`). */
  confirm(response: Record<string, unknown>): void;
  /** Cancel the interactive block (drives `cancel_block`). */
  cancel(): void;
}

/** Handle returned by {@link PanelModule.mount} so the host can tear the panel
 *  down (unmount React root, remove listeners) on cleanup. */
export interface PanelInstance {
  unmount(): void;
}

/**
 * The shape a dynamic panel module must export under
 * {@link PanelManifestDescriptor.export_name} (default `"default"`). This is the
 * contract package authors implement.
 */
export interface PanelModule {
  /** API version this module was built against; compared to
   *  {@link PANEL_HOST_API_VERSION} before mounting. */
  apiVersion: string;
  /**
   * Mount the panel into a host-owned DOM container with a constrained host
   * API. Returns a {@link PanelInstance} for later unmount. SHOULD NOT throw for
   * routine failures.
   */
  mount(container: HTMLElement, host: PanelHostApi): PanelInstance;
}

export interface LoadSuccess {
  ok: true;
  instance: PanelInstance;
}

export interface LoadFailure {
  ok: false;
  /** Stable reason code for diagnostics + tests. */
  reason:
    | "remote_url_rejected"
    | "invalid_module_url"
    | "import_failed"
    | "export_missing"
    | "not_a_panel_module"
    | "api_version_mismatch"
    | "mount_failed";
  message: string;
}

export type LoadResult = LoadSuccess | LoadFailure;

/**
 * The dynamic import indirection is isolated here so tests can inject a fake
 * importer (jsdom cannot `import()` a runtime URL). Production passes the real
 * `import()`.
 */
export type ModuleImporter = (moduleUrl: string) => Promise<Record<string, unknown>>;

const defaultImporter: ModuleImporter = (moduleUrl) =>
  // Vite leaves a bare dynamic import with a runtime expression alone; this is
  // the same-origin ESM load. `@vite-ignore` keeps the bundler from trying to
  // statically analyse the panel bundle (it lives in a wheel, not in src).
  import(/* @vite-ignore */ moduleUrl);

/**
 * Reject any module URL that is not same-origin. The backend serves validated
 * panel assets under `/api/blocks/panels/...`, so a legitimate
 * `module_url` is a site-relative path. Absolute http(s)/protocol-relative/
 * data/blob URLs are rejected — no remote or inline code is ever imported.
 *
 * ADR-055 Spec 0 (FR-005): prefix resolution goes through the shared
 * base-path source of truth — the validator resolves the URL the loader will
 * actually import (i.e. after `apiUrl` applies the configured mount prefix).
 * Both the backend-relative form (`/api/...`) and the already-prefixed form
 * are accepted; remote URLs are still rejected.
 *
 * Duplicated from the ADR-048 previewer loader so the panel host stays
 * self-contained (no cross-feature import).
 */
export function isSameOriginModuleUrl(moduleUrl: string): boolean {
  if (typeof moduleUrl !== "string" || moduleUrl.trim() === "") return false;
  const url = moduleUrl.trim();
  // Reject protocol-relative (`//host/...`) and any explicit scheme
  // (http:, https:, data:, blob:, file:, javascript:, ...).
  if (url.startsWith("//")) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return false;
  // Require a site-relative absolute path so it always resolves against the
  // app origin (the backend always emits `/api/blocks/panels/...`).
  if (!url.startsWith("/")) return false;
  // Defensive: confirm it resolves to the current origin, prefix applied.
  try {
    const resolved = new URL(apiUrl(url), window.location.origin);
    return resolved.origin === window.location.origin;
  } catch {
    return false;
  }
}

/**
 * Are the host and module API versions compatible? Major version (the part
 * before the first `.`) must match; the host advances minor/patch additively.
 */
export function isApiVersionCompatible(
  moduleApiVersion: string | undefined,
  hostApiVersion: string = PANEL_HOST_API_VERSION,
): boolean {
  if (!moduleApiVersion) return false;
  const major = (v: string) => v.split(".")[0];
  return major(moduleApiVersion) === major(hostApiVersion);
}

/** Narrow runtime guard: is `value` a usable {@link PanelModule}? */
export function isPanelModule(value: unknown): value is PanelModule {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<PanelModule>;
  return typeof candidate.mount === "function" && typeof candidate.apiVersion === "string";
}

/** Inject the CSS assets a manifest declares (same-origin only). Failures are
 *  non-fatal — the module still mounts without its stylesheet. */
function injectManifestCss(manifest: PanelManifestDescriptor): void {
  for (const href of manifest.css ?? []) {
    if (!isSameOriginModuleUrl(href)) continue;
    // ADR-055 Spec 0: resolve through the base path so the stylesheet loads
    // under a prefixed mount (apiUrl is idempotent for already-prefixed URLs).
    const resolvedHref = apiUrl(href);
    if (document.querySelector(`link[data-panel="${manifest.panel_id}"][href="${resolvedHref}"]`)) {
      continue;
    }
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = resolvedHref;
    link.dataset.panel = manifest.panel_id;
    document.head.appendChild(link);
  }
}

/**
 * The module URL with a per-mount cache buster.
 *
 * `import()` caches by URL for the life of the page, and a panel's module URL
 * is fixed by its manifest — so the *first* version of a panel a session ever
 * loaded was the version it kept using. That is wrong for the thing this
 * loader exists to serve: a panel is drop-in code the person is editing, and
 * a block written by a tutorial step is rewritten by the next run of that
 * step. The file on disk changed and the window went on running what it had.
 *
 * A timestamp rather than the manifest's `version`: the version is written by
 * hand, and an author who edits a panel without bumping it is exactly the
 * person this breaks. Re-fetching a small ES module each time a modal opens
 * costs nothing next to showing stale code.
 */
function freshModuleUrl(moduleUrl: string): string {
  const separator = moduleUrl.includes("?") ? "&" : "?";
  return `${moduleUrl}${separator}mounted=${Date.now()}`;
}

/**
 * Validate + import + mount a dynamic panel. Resolves to a {@link LoadResult}.
 * Never throws.
 */
export async function mountDynamicPanel(
  manifest: PanelManifestDescriptor,
  container: HTMLElement,
  host: PanelHostApi,
  importer: ModuleImporter = defaultImporter,
): Promise<LoadResult> {
  // Same-origin validation BEFORE any network/import work.
  if (typeof manifest.module_url !== "string" || manifest.module_url.trim() === "") {
    return { ok: false, reason: "invalid_module_url", message: "manifest has no module_url" };
  }
  if (!isSameOriginModuleUrl(manifest.module_url)) {
    return {
      ok: false,
      reason: "remote_url_rejected",
      message: `refused to load non-same-origin panel module: ${manifest.module_url}`,
    };
  }
  // API version compatibility gate (manifest-declared version).
  if (!isApiVersionCompatible(manifest.api_version)) {
    return {
      ok: false,
      reason: "api_version_mismatch",
      message: `panel api_version ${manifest.api_version ?? "<none>"} is incompatible with host`,
    };
  }

  let mod: Record<string, unknown>;
  try {
    // ADR-055 Spec 0: the manifest emits a backend-relative URL; import the
    // prefixed form so the module resolves under a mounted prefix.
    mod = await importer(freshModuleUrl(apiUrl(manifest.module_url)));
  } catch (err) {
    return {
      ok: false,
      reason: "import_failed",
      message: `failed to import panel module: ${describeError(err)}`,
    };
  }

  const exportName = manifest.export_name || "default";
  const exported = mod[exportName];
  if (exported === undefined) {
    return {
      ok: false,
      reason: "export_missing",
      message: `panel module has no export '${exportName}'`,
    };
  }
  if (!isPanelModule(exported)) {
    return {
      ok: false,
      reason: "not_a_panel_module",
      message: `export '${exportName}' is not a valid PanelModule`,
    };
  }
  if (!isApiVersionCompatible(exported.apiVersion)) {
    return {
      ok: false,
      reason: "api_version_mismatch",
      message: `module apiVersion ${exported.apiVersion} is incompatible with host`,
    };
  }

  injectManifestCss(manifest);

  try {
    const instance = exported.mount(container, host);
    if (!instance || typeof instance.unmount !== "function") {
      return {
        ok: false,
        reason: "mount_failed",
        message: "panel mount() did not return a valid instance",
      };
    }
    return { ok: true, instance };
  } catch (err) {
    return {
      ok: false,
      reason: "mount_failed",
      message: `panel mount() threw: ${describeError(err)}`,
    };
  }
}

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
