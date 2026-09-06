/**
 * ADR-034 Phase 1.3: SetupScreen — provider + permission-mode picker.
 *
 * Renders before a terminal tab is launched. Fetches `/api/ai/status` on
 * mount to disable providers that aren't installed and surface a
 * "(not logged in)" hint for providers that are installed but unauthenticated.
 */
import { useEffect, useMemo, useState } from "react";

import { apiFetch } from "../../lib/api/core";
import { useAppStore } from "../../store";
import { NoProvidersNotice } from "./SetupScreen.parts/NoProvidersNotice";
import { PermissionModePicker } from "./SetupScreen.parts/PermissionModePicker";
import { ProviderPicker } from "./SetupScreen.parts/ProviderPicker";
import type {
  AiStatusResponse,
  PermissionMode,
  ProviderStatus,
  TerminalProvider,
} from "./SetupScreen.parts/types";

export interface SetupLaunchConfig {
  provider: TerminalProvider;
  dangerous: boolean;
}

export interface SetupScreenProps {
  /** ID of the parent terminal tab. Used only for accessibility / labelling. */
  tabId: string;
  onLaunch: (config: SetupLaunchConfig) => void;
  onCancel: () => void;
}

// Module-level cache (30s TTL). Multiple SetupScreens mounted within 30s share
// the same in-flight / cached payload.
let _statusCache: { at: number; data: AiStatusResponse } | null = null;
let _statusInflight: Promise<AiStatusResponse> | null = null;

async function fetchStatus(force = false): Promise<AiStatusResponse> {
  const now = Date.now();
  if (!force && _statusCache && now - _statusCache.at < 30_000) {
    return _statusCache.data;
  }
  if (_statusInflight) return _statusInflight;
  _statusInflight = (async () => {
    try {
      // apiFetch routes through the base-path helper (ADR-055 Spec 0 FR-004)
      // and throws ApiError for non-2xx, so no manual r.ok check is needed.
      const data = await apiFetch<AiStatusResponse>("/api/ai/status");
      _statusCache = { at: Date.now(), data };
      return data;
    } finally {
      _statusInflight = null;
    }
  })();
  return _statusInflight;
}

/** Test-only: reset module cache. Not exported via index. */
export function _resetSetupStatusCache(): void {
  _statusCache = null;
  _statusInflight = null;
}

interface UseSetupStatusResult {
  status: AiStatusResponse | null;
  statusError: string | null;
  statusLoading: boolean;
}

function useSetupStatus(): UseSetupStatusResult {
  const [status, setStatus] = useState<AiStatusResponse | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await fetchStatus();
        if (!cancelled) {
          setStatus(data);
          setStatusError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setStatusError(err instanceof Error ? err.message : "status unavailable");
        }
      } finally {
        if (!cancelled) setStatusLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return { status, statusError, statusLoading };
}

export function SetupScreen({ tabId, onLaunch, onCancel }: SetupScreenProps) {
  const currentProject = useAppStore((s) => s.currentProject);
  const projectPath = currentProject?.path ?? null;

  const { status, statusError, statusLoading } = useSetupStatus();
  const [provider, setProvider] = useState<TerminalProvider | null>(null);
  const [permissionMode, setPermissionMode] = useState<PermissionMode | null>(null);

  const providers: ProviderStatus[] = useMemo(() => status?.providers ?? [], [status]);

  /**
   * ADR-034 FR-021d — the three status branches SetupScreen already
   * distinguishes. `statusLoaded` is true only once the payload actually
   * arrived, so "unknown availability" (in flight or failed) can never be
   * mistaken for "confirmed absence" and no new state is needed.
   */
  const statusLoaded = !statusLoading && statusError === null && status !== null;
  /** FR-021c — the loaded-and-all-unavailable branch, and only that branch. */
  const noProvidersAvailable = statusLoaded && providers.every((p) => !p.available);

  const selectedProviderStatus = providers.find((p) => p.name === provider);
  const launchDisabled =
    !provider ||
    !permissionMode ||
    !projectPath ||
    selectedProviderStatus === undefined ||
    !selectedProviderStatus.available;

  return (
    <div
      className="flex h-full min-h-0 flex-col overflow-hidden px-4 py-3"
      data-testid={`setup-screen-${tabId}`}
    >
      <div
        className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1"
        data-testid="setup-scroll-body"
      >
        {statusError ? (
          <div
            className="rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
            data-testid="setup-status-error"
          >
            Could not check provider status ({statusError}). Launch will be disabled until
            /api/ai/status is reachable.
          </div>
        ) : null}

        {noProvidersAvailable ? (
          <NoProvidersNotice providers={providers} />
        ) : (
          <ProviderPicker
            tabId={tabId}
            providers={providers}
            statusLoading={statusLoading}
            provider={provider}
            onChange={setProvider}
          />
        )}

        {/* #1859: Codex asks the user to trust this project's hooks on first
            launch. Non-technical users may not know what hooks are; if they
            decline, SciStudio's safety hooks (e.g. the data/ guard) never run.
            We surface a short note rather than silently editing global config. */}
        {provider === "codex" ? (
          <div
            className="rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
            data-testid="setup-codex-trust-note"
          >
            <strong className="font-medium">Heads up:</strong> the first time Codex launches in this
            project it will ask whether to trust its hooks. Please choose{" "}
            <strong className="font-medium">trust / yes</strong> — SciStudio installs safety hooks
            (such as protecting your <span className="font-mono">data/</span> folder from accidental
            edits) that only take effect if you accept.
          </div>
        ) : null}

        {/* #2045: Kimi Code does have a hook system, and its contract is the
            one SciStudio's scripts already speak — JSON on stdin, exit code 2
            blocks, regex matchers on the tool name. What it has no equivalent
            of is a *project-scope* place to declare hooks: the only location it
            reads is the user-level config file, and `.kimi-code/local.toml`
            accepts nothing but `[workspace] additional_dir`. So there is no
            file SciStudio can drop into the project the way it does for the
            other four CLIs, and this tab runs unguarded. Writing the user's
            global config on their behalf was rejected — SciStudio has never
            written a user-scope CLI config — which leaves saying so plainly. */}
        {provider === "kimi-code" ? (
          <div
            className="rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
            data-testid="setup-kimi-hooks-note"
          >
            <strong className="font-medium">Heads up:</strong> Kimi Code reads hooks only from its
            own user-level <span className="font-mono">config.toml</span>, so SciStudio&rsquo;s
            safety hooks — the ones that keep your <span className="font-mono">data/</span> folder
            and workflow files from being edited by accident — do not apply to this tab. You can add
            them by hand, or simply ask Kimi in this tab to set them up for you. They would then
            apply to <strong className="font-medium">every</strong> Kimi Code session on this
            machine, not just this project.
          </div>
        ) : null}

        <PermissionModePicker
          tabId={tabId}
          permissionMode={permissionMode}
          onChange={setPermissionMode}
        />

        <div
          className="rounded-2xl border border-stone-200 bg-stone-50 px-3 py-2 text-xs text-stone-600"
          data-testid="setup-working-dir"
        >
          Working dir:{" "}
          <span className="font-mono">
            {projectPath ?? <em className="text-stone-400">(no project open)</em>}
          </span>
        </div>
      </div>

      <div
        className="flex shrink-0 items-center justify-end gap-2 border-t border-stone-200 pt-3"
        data-testid="setup-actions"
      >
        <button
          type="button"
          className="rounded-full border border-stone-300 px-4 py-2 text-sm text-stone-600 hover:bg-stone-50"
          onClick={onCancel}
          data-testid="setup-cancel"
        >
          Cancel
        </button>
        <button
          type="button"
          className={`rounded-full px-4 py-2 text-sm text-white ${
            launchDisabled ? "bg-stone-300" : "bg-ink hover:bg-stone-800"
          }`}
          disabled={launchDisabled}
          data-testid="setup-launch"
          onClick={() => {
            if (provider && permissionMode) {
              onLaunch({ provider, dangerous: permissionMode === "dangerous" });
            }
          }}
        >
          Launch ▸
        </button>
      </div>
    </div>
  );
}
