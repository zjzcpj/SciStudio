/**
 * ADR-048 SPEC 1 — PreviewHost (FR-020 .. FR-023).
 *
 * The routed preview container. For a selected {@link PreviewTarget} it:
 *   1. Creates a session via `POST /api/previews/sessions` and reads the
 *      {@link PreviewEnvelope}.
 *   2. If the resolved previewer surfaces a `frontend_manifest` (first-class on
 *      `envelope.frontend_manifest`, with a legacy `envelope.metadata.frontend_manifest`
 *      fallback) → validates it (same-origin only,
 *      FR-022), dynamically `import()`s it, and mounts the named export with a
 *      constrained {@link PreviewHostApi} (FR-023). On ANY validation / import /
 *      mount failure → renders diagnostics AND the core fallback viewer for
 *      `envelope.kind` (FR-029, US2.3).
 *   3. Else → renders the core fallback viewer for `envelope.kind`.
 *
 * Child routing (composite slots, collection items, US4.3) re-uses the same
 * precedence by fetching the resource — which the backend renders as a freshly
 * routed child envelope — and pushing it onto a small drill-down stack.
 *
 * The host keeps only UI-level state (active envelope, drill-down stack); the
 * backend remains authoritative for routing, sessions, and data. Cache keying
 * lives in the Zustand preview slice (FR-021); this component reads/writes it
 * through the injected callbacks.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../lib/api";
import { apiUrl } from "../../lib/api/base-path";
import type {
  PreviewEnvelope,
  PreviewResource,
  PreviewResourceResponse,
  PreviewTarget,
  PreviewerManifest,
} from "../../types/api";

import { CoreFallbackRenderer, DiagnosticsBanner } from "./coreViewers";
import { type LoadResult, mountDynamicPreviewer } from "./dynamicPreviewer";
import {
  PREVIEWER_HOST_API_VERSION,
  type PreviewHostApi,
  type PreviewProviderIdentity,
  type PreviewerInstance,
} from "./previewerHostApi";

export interface PreviewHostProps {
  /** The target to preview. A `null` target renders the empty state. */
  target: PreviewTarget | null;
  /** Optional initial query state (slice/page/sort). */
  initialQuery?: Record<string, unknown>;
  /**
   * #2113 — routing epoch. Bumped by the store whenever a per-type previewer
   * choice changes (#2049); the session-creation effect re-runs, so an open
   * preview re-creates its session and the backend routes it through the new
   * choice instead of sitting on the envelope the old choice produced.
   * Omitting it keeps the host's pre-#2113 behaviour exactly.
   */
  routingEpoch?: number;
  /**
   * Optional session-keyed cache hooks (FR-021). The host writes rendered
   * envelopes only after the backend has resolved preview identity, so cache
   * keys include target + previewer + session + query + data version.
   */
  getCachedEnvelope?: (key: string) => PreviewEnvelope | undefined;
  cacheEnvelope?: (key: string, envelope: PreviewEnvelope) => void;
  buildCacheKey?: (
    target: PreviewTarget,
    query: Record<string, unknown>,
    opts?: PreviewCacheKeyOptions,
  ) => string;
  /** Test seam: inject a fake dynamic-module importer. */
  importer?: Parameters<typeof mountDynamicPreviewer>[3];
}

type Status = "idle" | "loading" | "ready" | "error";
type PreviewCacheKeyOptions = {
  previewerId?: string | null;
  sessionId?: string | null;
  dataVersion?: string | number | null;
};
const RESOURCE_PARAMS_MAX_CHARS = 8192;

function readManifest(envelope: PreviewEnvelope | null): PreviewerManifest | undefined {
  if (!envelope) return undefined;
  // Prefer the first-class field the session manager stamps (#1579); fall back
  // to the legacy flattened `metadata.frontend_manifest` for un-migrated providers.
  const manifest = envelope.frontend_manifest ?? envelope.metadata?.frontend_manifest;
  if (manifest && typeof manifest === "object" && typeof manifest.module_url === "string") {
    return manifest;
  }
  return undefined;
}

function manifestIdentityKey(manifest: PreviewerManifest | undefined): string | null {
  if (!manifest) return null;
  return [
    manifest.previewer_id,
    manifest.module_url,
    manifest.export_name,
    manifest.api_version ?? "",
  ].join("|");
}

function cacheDataVersionFromEnvelope(envelope: PreviewEnvelope): string | number | null {
  const candidates = [
    envelope.metadata?.data_version,
    envelope.metadata?.dataVersion,
    envelope.payload?.data_version,
    envelope.payload?.dataVersion,
  ];
  const value = candidates.find(
    (candidate) => typeof candidate === "string" || typeof candidate === "number",
  );
  return typeof value === "string" || typeof value === "number" ? value : null;
}

function cacheIdentityFromEnvelope(envelope: PreviewEnvelope): PreviewCacheKeyOptions {
  return {
    previewerId: envelope.previewer_id,
    sessionId: envelope.session_id,
    dataVersion: cacheDataVersionFromEnvelope(envelope),
  };
}

function cacheEnvelopeForQuery(
  cacheEnvelope: PreviewHostProps["cacheEnvelope"],
  buildCacheKey: PreviewHostProps["buildCacheKey"],
  target: PreviewTarget,
  query: Record<string, unknown>,
  envelope: PreviewEnvelope,
) {
  if (!cacheEnvelope || !buildCacheKey) return;
  cacheEnvelope(buildCacheKey(target, query, cacheIdentityFromEnvelope(envelope)), envelope);
}

export function PreviewHost({
  target,
  initialQuery,
  routingEpoch,
  cacheEnvelope,
  buildCacheKey,
  importer,
}: PreviewHostProps) {
  const [status, setStatus] = useState<Status>("idle");
  const [envelope, setEnvelope] = useState<PreviewEnvelope | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  // Diagnostics raised by the host itself (dynamic-load failures etc).
  const [hostDiagnostics, setHostDiagnostics] = useState<string[]>([]);
  // Drill-down stack of child envelopes (composite slot / collection item).
  const [childStack, setChildStack] = useState<PreviewEnvelope[]>([]);

  const queryRef = useRef<Record<string, unknown>>(initialQuery ?? {});
  const initialQueryKey = useMemo(() => JSON.stringify(initialQuery ?? {}), [initialQuery]);

  // -- session creation ----------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    setChildStack([]);
    setHostDiagnostics([]);
    if (!target) {
      setStatus("idle");
      setEnvelope(null);
      return;
    }
    const query = { ...(initialQuery ?? {}) };
    queryRef.current = query;

    setStatus("loading");
    setRequestError(null);
    api
      .createPreviewSession(target, query)
      .then((env) => {
        if (cancelled) return;
        setEnvelope(env);
        setStatus("ready");
        cacheEnvelopeForQuery(cacheEnvelope, buildCacheKey, target, query, env);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setRequestError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
    // initialQuery is captured intentionally on target change only; later
    // query updates go through patchQuery below. routingEpoch (#2113) is a
    // deliberate dep: a choice change must re-create the session so the new
    // routing applies to the preview already open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.ref, target?.kind, initialQueryKey, routingEpoch]);

  // -- patch query (slice/page/sort/slot) ----------------------------------
  const patchQuery = useCallback(
    async (patch: Record<string, unknown>): Promise<PreviewEnvelope> => {
      const current = childStack[childStack.length - 1] ?? envelope;
      if (!current?.session_id) {
        return current ?? Promise.reject(new Error("no session"));
      }
      queryRef.current = { ...queryRef.current, ...patch };
      const next = await api.patchPreviewSession(current.session_id, patch);
      if (current.session_id === envelope?.session_id) {
        setEnvelope(next);
      } else {
        setChildStack((stack) => (stack.length > 0 ? [...stack.slice(0, -1), next] : stack));
      }
      if (target && current.session_id === envelope?.session_id)
        cacheEnvelopeForQuery(cacheEnvelope, buildCacheKey, target, queryRef.current, next);
      return next;
    },
    [childStack, envelope, buildCacheKey, cacheEnvelope, target],
  );

  // -- child routing (composite slot / collection item) --------------------
  const openResource = useCallback(
    async (resource: PreviewResource) => {
      const active = childStack[childStack.length - 1] ?? envelope;
      if (!active?.session_id) return;
      try {
        const result = await fetchPreviewResource(
          active.session_id,
          resource.resource_id,
          resource.params,
        );
        // A child resource resolves to a full child envelope.
        const childEnvelope = result.data as unknown as PreviewEnvelope;
        if (childEnvelope && typeof childEnvelope.kind === "string") {
          setChildStack((stack) => [...stack, childEnvelope]);
        }
      } catch (err) {
        setHostDiagnostics((d) => [
          ...d,
          `failed to open ${resource.resource_id}: ${err instanceof Error ? err.message : String(err)}`,
        ]);
      }
    },
    [childStack, envelope],
  );

  const popChild = useCallback(() => setChildStack((stack) => stack.slice(0, -1)), []);

  // -- export / save -------------------------------------------------------
  const saveEnvelopeResource = useCallback(
    async (
      active: PreviewEnvelope,
      resourceId: string,
      params?: Record<string, unknown>,
      filename?: string,
    ) => {
      if (!active.session_id) {
        throw new Error("no preview session");
      }
      const defaultFilename = filename ?? defaultResourceFilename(active, params);
      const dialog = await api
        .openNativeSaveDialog({
          defaultFilename,
          fileFilter: fileFilterForFilename(defaultFilename),
        })
        .catch(() => ({ paths: [] as string[], available: false }));
      const destinationPath = dialog.paths[0];
      if (destinationPath) {
        await api.savePreviewResource(active.session_id, resourceId, {
          destination_path: destinationPath,
          params: params ?? {},
        });
        return;
      }
      // No path returned. Only fall back to a browser download when the native
      // save dialog was unavailable (the route raised → `available === false`).
      // When the native dialog DID run, an empty result means the user
      // cancelled — respect that and do NOT open a second (browser) save dialog.
      // Fixes the double save-dialog bug for every previewer, since they all
      // export through this shared path.
      if (dialog.available === false) {
        const result = await fetchPreviewResource(active.session_id, resourceId, params);
        downloadDataUri(result.data, defaultFilename);
      }
    },
    [],
  );

  const exportResource = useCallback(
    async (resource: PreviewResource) => {
      const active = childStack[childStack.length - 1] ?? envelope;
      if (!active) return;
      try {
        await saveEnvelopeResource(
          active,
          resource.resource_id,
          resource.params,
          defaultResourceFilename(active, resource.params),
        );
        /*
         * ADR-053 FR-052 — `plot_exported`, in the closed `UI_EVENT_NAMES`
         * set. Reported only after the save resolved, so a step that asks the
         * reader to keep the figure waits for the file rather than for the
         * dialog. A step cannot judge that on the file itself: the reader
         * names it in a native dialog, and image extensions are outside the
         * watcher's allowlist, so nothing would re-evaluate the condition even
         * once the file existed. Scoped to a plot's own artifact — other
         * previewers export too, and this event is about the figure.
         */
        if (active.target.kind === "plot_artifact") {
          // Imported here rather than at the top of the file: the store pulls
          // in every slice, and this module is mounted by tests that mock a
          // narrow slice of the API surface. The report is a side effect of a
          // rare action, so paying for the module then costs nothing.
          const { useAppStore } = await import("../../store");
          void useAppStore.getState().reportTutorialUiEvent("plot_exported");
        }
      } catch (err) {
        setHostDiagnostics((d) => [
          ...d,
          `export failed: ${err instanceof Error ? err.message : String(err)}`,
        ]);
      }
    },
    [childStack, envelope, saveEnvelopeResource],
  );

  // The envelope currently in focus (top of the drill-down stack, else root).
  const activeEnvelope = childStack[childStack.length - 1] ?? envelope;
  const manifest = useMemo(() => readManifest(activeEnvelope), [activeEnvelope]);
  const manifestKey = useMemo(() => manifestIdentityKey(manifest), [manifest]);
  const dynamicMountKey =
    manifestKey && activeEnvelope
      ? `${manifestKey}|${activeEnvelope.session_id ?? activeEnvelope.target.ref}`
      : null;
  const activeEnvelopeRef = useRef<PreviewEnvelope | null>(activeEnvelope);
  const manifestRef = useRef<PreviewerManifest | undefined>(manifest);
  const patchQueryRef = useRef(patchQuery);
  activeEnvelopeRef.current = activeEnvelope;
  manifestRef.current = manifest;
  patchQueryRef.current = patchQuery;

  // -- dynamic previewer mount ---------------------------------------------
  const mountRef = useRef<HTMLDivElement | null>(null);
  const instanceRef = useRef<PreviewerInstance | null>(null);
  const [dynamicFailed, setDynamicFailed] = useState(false);

  const hostApi: PreviewHostApi | null = useMemo(() => {
    const initialEnvelope = activeEnvelopeRef.current;
    if (!initialEnvelope || !dynamicMountKey) return null;
    const currentEnvelope = () => activeEnvelopeRef.current ?? initialEnvelope;
    const provider: PreviewProviderIdentity = {
      previewerId: initialEnvelope.previewer_id,
      capabilities: [],
      source: initialEnvelope.target.source ?? null,
    };
    return {
      apiVersion: PREVIEWER_HOST_API_VERSION,
      get previewSessionId() {
        return currentEnvelope().session_id;
      },
      get envelope() {
        return currentEnvelope();
      },
      get kind() {
        return currentEnvelope().kind;
      },
      get provider() {
        const current = currentEnvelope();
        return {
          ...provider,
          previewerId: current.previewer_id,
          source: current.target.source ?? null,
        };
      },
      session: {
        refresh: async () => {
          const sessionId = currentEnvelope().session_id;
          return sessionId ? api.getPreviewSession(sessionId) : null;
        },
        patchQuery: async (q) => {
          const current = currentEnvelope();
          if (!current.session_id) return current;
          return patchQueryRef.current(q);
        },
        getResource: async (resourceId, params) => {
          const current = currentEnvelope();
          if (!current.session_id) return null;
          const resource = current.resources.find((r) => r.resource_id === resourceId);
          return (
            await fetchPreviewResource(
              current.session_id,
              resourceId,
              mergeResourceParams(resource, params),
            )
          ).data;
        },
        get resources() {
          return currentEnvelope().resources;
        },
      },
      // ADR-055 Spec 0: asset URLs handed to previewer modules carry the
      // configured mount prefix via the shared base-path helper.
      assetUrl: (assetPath: string) =>
        apiUrl(
          `/api/previews/assets/${encodeURIComponent(currentEnvelope().previewer_id)}/${assetPath.replace(/^\/+/, "")}`,
        ),
      exportArtifact: async (request) => {
        const current = currentEnvelope();
        const resourceId = request?.resourceId ?? "export";
        const resource = current.resources.find((r) => r.resource_id === resourceId);
        const params = mergeResourceParams(resource, {
          ...(request?.params ?? {}),
          ...(request?.format ? { format: request.format } : {}),
        });
        await saveEnvelopeResource(
          current,
          resourceId,
          params,
          request?.filename ?? defaultResourceFilename(current, params),
        );
      },
      saveArtifact: async (request) => {
        const current = currentEnvelope();
        const resourceId = request?.resourceId ?? "export";
        const resource = current.resources.find((r) => r.resource_id === resourceId);
        const params = mergeResourceParams(resource, {
          ...(request?.params ?? {}),
          ...(request?.format ? { format: request.format } : {}),
        });
        await saveEnvelopeResource(
          current,
          resourceId,
          params,
          request?.filename ?? defaultResourceFilename(current, params),
        );
      },
      reportError: (message: string) => {
        setHostDiagnostics((d) => [...d, message]);
      },
    };
  }, [dynamicMountKey, saveEnvelopeResource]);

  useEffect(() => {
    // Tear down any prior instance whenever the focus changes.
    if (instanceRef.current) {
      try {
        instanceRef.current.unmount();
      } catch {
        /* ignore unmount errors */
      }
      instanceRef.current = null;
    }
    setDynamicFailed(false);

    const currentManifest = manifestRef.current;
    if (!currentManifest || !hostApi || !mountRef.current) return;
    let disposed = false;
    const container = mountRef.current;
    void mountDynamicPreviewer(currentManifest, container, hostApi, importer).then(
      (result: LoadResult) => {
        if (disposed) return;
        if (result.ok) {
          instanceRef.current = result.instance;
        } else {
          // FR-029 / US2.3 — diagnostics + clean fallback to the core viewer.
          setDynamicFailed(true);
          setHostDiagnostics((d) => [...d, result.message]);
        }
      },
    );
    return () => {
      disposed = true;
      if (instanceRef.current) {
        try {
          instanceRef.current.unmount();
        } catch {
          /* ignore */
        }
        instanceRef.current = null;
      }
    };
  }, [dynamicMountKey, hostApi, importer]);

  // Push new envelopes into a live dynamic instance.
  useEffect(() => {
    if (instanceRef.current?.update && activeEnvelope) {
      instanceRef.current.update(activeEnvelope);
    }
  }, [activeEnvelope]);

  // -- render --------------------------------------------------------------
  if (!target || status === "idle") {
    return (
      <div className="rounded-[1.6rem] border border-dashed border-stone-300 px-4 py-6 text-sm text-stone-500">
        Nothing to preview yet
      </div>
    );
  }
  if (status === "loading") {
    return (
      <div
        className="rounded-[1.6rem] border border-stone-200 bg-white p-4 text-sm text-stone-500"
        data-testid="preview-host-loading"
      >
        Loading preview…
      </div>
    );
  }
  if (status === "error") {
    return (
      <div
        className="rounded-[1.6rem] border border-red-300 bg-red-50 p-4 text-sm text-red-800"
        data-testid="preview-host-request-error"
        role="alert"
      >
        Could not create a preview session: {requestError}
      </div>
    );
  }
  if (!activeEnvelope) return null;

  const useDynamic = !!manifest && !dynamicFailed;

  return (
    <div data-testid="preview-host">
      {childStack.length > 0 ? (
        <button
          type="button"
          data-testid="preview-host-back"
          onClick={popChild}
          className="mb-2 rounded-full border border-stone-300 bg-white px-3 py-0.5 text-xs text-stone-600 hover:bg-stone-50"
        >
          ← Back
        </button>
      ) : null}

      <DiagnosticsBanner diagnostics={hostDiagnostics} />

      {/* Mount point for the dynamic previewer. Always present in the DOM so the
          mount effect has a stable container; hidden when we fall back. */}
      <div
        ref={mountRef}
        data-testid="preview-host-dynamic-mount"
        style={{ display: useDynamic ? "block" : "none" }}
      />

      {/* Core fallback path: no manifest, or the dynamic previewer failed. */}
      {!useDynamic ? (
        <CoreFallbackRenderer
          envelope={activeEnvelope}
          onPatchQuery={(q) => void patchQuery(q)}
          onOpenResource={(r) => void openResource(r)}
          onExport={(r) => void exportResource(r)}
        />
      ) : null}
    </div>
  );
}

async function fetchPreviewResource(
  sessionId: string,
  resourceId: string,
  params?: Record<string, unknown>,
): Promise<PreviewResourceResponse> {
  const response = await fetch(buildPreviewResourceUrl(sessionId, resourceId, params));
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({ detail: response.statusText }))) as {
      detail?: string | { message?: string };
    };
    const detail = payload.detail;
    const message =
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : `Request failed with ${response.status}`;
    throw new Error(message);
  }
  return (await response.json()) as PreviewResourceResponse;
}

function buildPreviewResourceUrl(
  sessionId: string,
  resourceId: string,
  params?: Record<string, unknown>,
): string {
  // ADR-055 Spec 0: the URL carries the configured mount prefix via apiUrl
  // (idempotent under the default root mount).
  const base = apiUrl(
    `/api/previews/sessions/${encodeURIComponent(sessionId)}/resources/${encodeURIComponent(
      resourceId,
    )}`,
  );
  if (!params || Object.keys(params).length === 0) return base;
  const encodedParams = JSON.stringify(params);
  if (encodedParams.length > RESOURCE_PARAMS_MAX_CHARS) {
    throw new Error("resource params exceed the 8 KiB limit");
  }
  const search = new URLSearchParams({ params: encodedParams });
  return `${base}?${search.toString()}`;
}

function mergeResourceParams(
  resource: PreviewResource | undefined,
  params?: Record<string, unknown>,
): Record<string, unknown> | undefined {
  const merged = { ...(resource?.params ?? {}), ...(params ?? {}) };
  return Object.keys(merged).length > 0 ? merged : undefined;
}

// Map an export format to the file extension the default save filename uses.
// jpeg is conventionally saved as .jpg to match the on-disk artifact (#1918).
function extensionForFormat(format: string): string {
  return format === "jpeg" ? "jpg" : format;
}

function defaultResourceFilename(
  envelope: PreviewEnvelope,
  params?: Record<string, unknown>,
): string {
  // When the caller requested a specific export format (the plot Save-as menu,
  // #1918), the default filename must carry that format's extension so the
  // native dialog defaults to the right type instead of the preview's format.
  const requested =
    typeof params?.format === "string" && params.format ? extensionForFormat(params.format) : "";
  const payloadPath = envelope.payload?.path;
  if (typeof payloadPath === "string" && payloadPath) {
    const normalized = payloadPath.replace(/[\\/]+$/, "");
    const parts = normalized.split(/[\\/]/);
    const name = parts[parts.length - 1];
    if (name) {
      if (requested) {
        const base = name.replace(/\.[A-Za-z0-9]+$/, "");
        return `${base}.${requested}`;
      }
      return name;
    }
  }
  return `${envelope.previewer_id}.${requested || "bin"}`;
}

function fileFilterForFilename(filename: string): string {
  const match = /\.([A-Za-z0-9]+)$/.exec(filename);
  if (!match) return "All files (*.*)|*.*";
  const extension = match[1].toLowerCase();
  return `${extension.toUpperCase()} (*.${extension})|*.${extension}|All files (*.*)|*.*`;
}

function downloadDataUri(data: Record<string, unknown>, filename: string): void {
  const dataUri = data.data_uri;
  if (typeof dataUri !== "string" || !dataUri.startsWith("data:")) {
    throw new Error("resource did not provide downloadable data");
  }
  const link = document.createElement("a");
  link.href = dataUri;
  link.download = filename;
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
