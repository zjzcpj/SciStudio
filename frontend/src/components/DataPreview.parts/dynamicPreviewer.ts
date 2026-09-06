/**
 * ADR-048 SPEC 1 — same-origin dynamic previewer module loading (FR-022).
 *
 * Validates a {@link PreviewerManifest}, dynamically `import()`s its ESM
 * module from a *same-origin* URL only, reads the named export, checks it is a
 * {@link PreviewerModule} with a compatible API version, and mounts it into a
 * host-owned container.
 *
 * Any failure (remote URL, import error, missing/invalid export, version
 * mismatch, mount throw) resolves to a typed {@link LoadFailure} so the caller
 * ({@link PreviewHost}) can render the diagnostic AND fall back to the core
 * viewer for the envelope kind (FR-029, US2.3). This module NEVER throws.
 */

import { apiUrl } from "../../lib/api/base-path";
import type { PreviewerManifest } from "../../types/api";
import {
  type PreviewHostApi,
  type PreviewerInstance,
  type PreviewerModule,
  isApiVersionCompatible,
  isPreviewerModule,
} from "./previewerHostApi";

export interface LoadSuccess {
  ok: true;
  instance: PreviewerInstance;
}

export interface LoadFailure {
  ok: false;
  /** Stable reason code for diagnostics + tests. */
  reason:
    | "remote_url_rejected"
    | "invalid_module_url"
    | "import_failed"
    | "export_missing"
    | "not_a_previewer_module"
    | "api_version_mismatch"
    | "mount_failed";
  message: string;
}

export type LoadResult = LoadSuccess | LoadFailure;

/**
 * Reject any module URL that is not same-origin. The backend serves validated
 * previewer assets under `/api/previews/assets/...`, so a legitimate
 * `module_url` is a site-relative path. Absolute http(s)/protocol-relative/
 * data/blob URLs are rejected — no remote or inline code is ever imported
 * (FR-022, ADR-048 §4).
 *
 * ADR-055 Spec 0 (FR-005): prefix resolution goes through the shared
 * base-path source of truth — the validator resolves the URL the loader will
 * actually import (i.e. after `apiUrl` applies the configured mount prefix).
 * Both the backend-relative form (`/api/...`) and the already-prefixed form
 * are accepted; remote URLs are still rejected.
 */
export function isSameOriginModuleUrl(moduleUrl: string): boolean {
  if (typeof moduleUrl !== "string" || moduleUrl.trim() === "") return false;
  const url = moduleUrl.trim();
  // Reject protocol-relative (`//host/...`) and any explicit scheme
  // (http:, https:, data:, blob:, file:, javascript:, ...).
  if (url.startsWith("//")) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return false;
  // Require a site-relative absolute path so it always resolves against the
  // app origin (the backend always emits `/api/previews/assets/...`).
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
 * The dynamic import indirection is isolated here so tests can inject a fake
 * importer (jsdom cannot `import()` a runtime URL). Production passes the real
 * `import()`.
 */
export type ModuleImporter = (moduleUrl: string) => Promise<Record<string, unknown>>;

const defaultImporter: ModuleImporter = (moduleUrl) =>
  // Vite leaves a bare dynamic import with a runtime expression alone; this is
  // the same-origin ESM load. `@vite-ignore` keeps the bundler from trying to
  // statically analyse the previewer bundle (it lives in a wheel, not in src).
  import(/* @vite-ignore */ moduleUrl);

/** Inject the CSS assets a manifest declares (same-origin only). Failures are
 *  non-fatal — the module still mounts without its stylesheet. */
function injectManifestCss(manifest: PreviewerManifest): void {
  for (const href of manifest.css ?? []) {
    if (!isSameOriginModuleUrl(href)) continue;
    // ADR-055 Spec 0: resolve through the base path so the stylesheet loads
    // under a prefixed mount (apiUrl is idempotent for already-prefixed URLs).
    const resolvedHref = apiUrl(href);
    if (
      document.querySelector(
        `link[data-previewer="${manifest.previewer_id}"][href="${resolvedHref}"]`,
      )
    ) {
      continue;
    }
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = resolvedHref;
    link.dataset.previewer = manifest.previewer_id;
    document.head.appendChild(link);
  }
}

/**
 * Validate + import + mount a dynamic previewer. Resolves to a {@link LoadResult}.
 * Never throws.
 */
export async function mountDynamicPreviewer(
  manifest: PreviewerManifest,
  container: HTMLElement,
  hostApi: PreviewHostApi,
  importer: ModuleImporter = defaultImporter,
): Promise<LoadResult> {
  // FR-022 — same-origin validation BEFORE any network/import work.
  if (typeof manifest.module_url !== "string" || manifest.module_url.trim() === "") {
    return { ok: false, reason: "invalid_module_url", message: "manifest has no module_url" };
  }
  if (!isSameOriginModuleUrl(manifest.module_url)) {
    return {
      ok: false,
      reason: "remote_url_rejected",
      message: `refused to load non-same-origin previewer module: ${manifest.module_url}`,
    };
  }
  // FR-006 — API version compatibility gate.
  if (!isApiVersionCompatible(manifest.api_version)) {
    return {
      ok: false,
      reason: "api_version_mismatch",
      message: `previewer api_version ${manifest.api_version ?? "<none>"} is incompatible with host`,
    };
  }

  let mod: Record<string, unknown>;
  try {
    // ADR-055 Spec 0: the manifest emits a backend-relative URL; import the
    // prefixed form so the module resolves under a mounted prefix.
    mod = await importer(apiUrl(manifest.module_url));
  } catch (err) {
    return {
      ok: false,
      reason: "import_failed",
      message: `failed to import previewer module: ${describeError(err)}`,
    };
  }

  const exportName = manifest.export_name || "default";
  const exported = mod[exportName];
  if (exported === undefined) {
    return {
      ok: false,
      reason: "export_missing",
      message: `previewer module has no export '${exportName}'`,
    };
  }
  if (!isPreviewerModule(exported)) {
    return {
      ok: false,
      reason: "not_a_previewer_module",
      message: `export '${exportName}' is not a valid PreviewerModule`,
    };
  }
  if (!isApiVersionCompatible((exported as PreviewerModule).apiVersion)) {
    return {
      ok: false,
      reason: "api_version_mismatch",
      message: `module apiVersion ${(exported as PreviewerModule).apiVersion} is incompatible with host`,
    };
  }

  injectManifestCss(manifest);

  try {
    const instance = (exported as PreviewerModule).mount(container, hostApi);
    if (!instance || typeof instance.unmount !== "function") {
      return {
        ok: false,
        reason: "mount_failed",
        message: "previewer mount() did not return a valid instance",
      };
    }
    return { ok: true, instance };
  } catch (err) {
    return {
      ok: false,
      reason: "mount_failed",
      message: `previewer mount() threw: ${describeError(err)}`,
    };
  }
}

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}
