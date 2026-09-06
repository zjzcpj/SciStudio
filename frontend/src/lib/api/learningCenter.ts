/**
 * ADR-053 Learning Center (#2057) — client for `/api/tutorials`.
 *
 * The wire shapes below are the manager-owned HTTP contract in
 * `docs/planning/learning-center-checklist.md` §6.1.6. They are transcribed
 * here rather than invented: a field this file adds on its own would be a
 * field the backend never sends.
 *
 * Nothing in this module judges a tutorial. Spec §4.1 puts every completion
 * decision on the backend, because almost every condition the designed
 * scenarios need is a backend fact — a registered type, a git branch, a
 * succeeded run, a file on disk. The frontend reports what the user did
 * (`reportTutorialUiEvent`, FR-052), asks for a re-read of state no event
 * reaches (`evaluateActiveTutorialStep`, FR-053), and renders whatever step
 * view comes back.
 *
 * Types live beside the client rather than in `types/api.ts`, following the
 * work-import module (#2001), so the contract and the calls that depend on it
 * move together.
 */

import { apiUrl } from "./base-path";
import { ApiError, apiFetch, JSON_HEADERS } from "./core";

/** §6.1.6 — the four discovery sources a tutorial can come from. */
export type TutorialSourceKind = "core" | "package" | "user" | "project";

/**
 * §6.1.6 — the state the backend reports for a catalogue entry (FR-085).
 *
 * `unavailable` always arrives with an `unavailable_reason`: a tutorial whose
 * required package is missing is still *listed*, because a user cannot decide
 * to install a package whose tutorials they were never shown.
 */
export type TutorialEntryState = "not_started" | "in_progress" | "complete" | "unavailable";

/** §6.1.6 `CatalogueEntry`. */
export interface TutorialCatalogueEntry {
  source_kind: TutorialSourceKind;
  /** `""` for core, the distribution name for a package, `user` / `project`. */
  source_id: string;
  id: string;
  title: string;
  summary: string;
  cover_url: string | null;
  order: number;
  state: TutorialEntryState;
  unavailable_reason: string | null;
  /**
   * The tutorial project directory on disk, when one exists.
   *
   * Carried on the entry so the restart confirmation can name the directory it
   * is about to delete (FR-066, FR-067, FR-087) without a second round trip.
   */
  project_directory: string | null;
  /**
   * Whether the tutorial only ever asks the reader to read on.
   *
   * Derived on the backend from the steps' own conditions rather than declared
   * in the manifest — a tutorial cannot claim to be reading while waiting on a
   * run to succeed. The Learning Center lists these under their own tab.
   */
  reading: boolean;
}

/** §6.1.6 — one source's group, with its own counts (FR-084, FR-076). */
export interface TutorialCatalogueGroup {
  source_kind: TutorialSourceKind;
  source_id: string;
  label: string;
  completed: number;
  total: number;
  tutorials: TutorialCatalogueEntry[];
}

export interface TutorialCatalogueResponse {
  groups: TutorialCatalogueGroup[];
  active: TutorialSessionResponse | null;
  /** Per-source discovery problems; one bad manifest never empties a group. */
  diagnostics: string[];
}

/**
 * §6.1.6 — the step view, rendered verbatim.
 *
 * `awaiting_continue` is the one step kind that carries a continue button
 * (FR-012, the reading case). Every other step advances on its own the moment
 * its condition holds, with no confirmation click (User Story 1, acceptance 3).
 */
/**
 * What a step points at, and which one of it.
 *
 * `args` is empty for the targets whose name is already an address, and carries
 * the selector for the ones that address an element among many of its kind:
 * `block_type` for `palette_block` and `node`, `plot_id` for `plot_card`. Kept
 * as an open record rather than a union per target so a new backend target does
 * not need a matching type change here — the closed set lives in
 * `scistudio.tutorials.manifest.HIGHLIGHT_SPECS` and is mirrored for lookup in
 * `LearningCenter.parts/targets.ts`.
 */
export interface TutorialHighlightView {
  target: string;
  args: Record<string, string>;
}

/**
 * FR-011b — a dialog this step seeds, and the values it opens holding.
 *
 * The same shape as a highlight, meaning something different: a highlight
 * points at what is already on screen, a prefill supplies the default for
 * something not yet open. The closed target set lives in
 * `scistudio.tutorials.manifest.PREFILL_SPECS`; the frontend consumer for each
 * target is what makes it do anything.
 */
export interface TutorialPrefillView {
  target: string;
  args: Record<string, string>;
}

/**
 * FR-011 / #2061 — the step's user-triggered action, as the reader sees it.
 *
 * Only the label crosses the wire. What pressing the button does is the
 * backend's to perform through the trigger route, so nothing here can address
 * a surface the manifest format cannot (FR-041).
 */
export interface TutorialTriggerView {
  label: string;
}

export interface TutorialStepView {
  id: string;
  index: number;
  total: number;
  /** FR-011c — the step's own short heading; null falls back to the tutorial's. */
  title: string | null;
  /**
   * FR-011 — what the step says, as the ordered beats it is delivered in:
   * typically a line or two introducing the material, then the line that
   * hands over the task. Empty when the step says nothing.
   */
  say: string[];
  /**
   * FR-011f (#2136) — the expression each beat is delivered with, one per beat,
   * in the same order as `say`.
   *
   * Optional so a fixture written before expressions existed reads as the
   * default rather than as a type error.
   */
  say_moods?: string[];
  /**
   * FR-011e — deliver this step as a chat line rather than as a scene. Declared
   * by the step's author; the surface does not infer it.
   */
  /**
   * FR-011e — which form each beat is delivered in, in the same order as `say`.
   *
   * Per beat because a step's lead-in and its instruction often want different
   * forms: "a block is the basic unit", said about the palette as a whole,
   * wants the character standing there; "drag Load onto the canvas", said about
   * one entry in it, wants a chat line beside that entry.
   */
  compacts: boolean[];
  /**
   * FR-054c (#2136) — this step moves on by itself once its condition holds.
   *
   * Optional so a fixture written before it existed reads as "wait", which is
   * the behaviour every step had.
   */
  auto_advance?: boolean;
  /**
   * FR-089e (#2136) — what each beat points at, in the same order as `say`.
   *
   * Per beat because a step is usually a lead-in and an instruction: ringing
   * the control the instruction names while the lead-in is still on screen
   * points at something she has not mentioned yet, which reads as an
   * instruction and sends the reader off early.
   */
  highlights: (TutorialHighlightView | null)[];
  route_to: string | null;
  prefill: TutorialPrefillView[];
  /**
   * FR-011 — the reading pages this step presents, in order. Names the pages
   * route serves; the reading surface fetches content as the reader turns.
   * Optional in the type so older fixtures and cached responses stay valid;
   * the backend always sends it.
   */
  pages?: string[];
  /** FR-011 / #2061 — the step's user-triggered action, when it declares one. */
  trigger?: TutorialTriggerView | null;
  awaiting_continue: boolean;
  /**
   * FR-054a — whether this step's condition holds right now.
   *
   * What Continue reads to decide whether it is live. Reported by the backend
   * rather than worked out here: judging is spec §4.1's backend concern, and a
   * second copy of the rule in the frontend is the thing FR-002 removed.
   */
  satisfied: boolean;
}

/** §6.1.7 — one open scripted terminal: the surface, and the tab carrying it. */
export interface TutorialReplayView {
  surface: string;
  tab_id: string;
}

/**
 * One row of the session's read-only step outline: the inert subset of a step
 * — index, id, title, say, pages — so the reading window can show every card
 * name up front. For a sequential tutorial, a row is behind the reader exactly
 * when its index is smaller than the current step's.
 */
export interface TutorialStepOutline {
  index: number;
  id: string;
  title: string | null;
  say: string[];
  pages: string[];
}

export interface TutorialSessionResponse {
  source_kind: TutorialSourceKind;
  source_id: string;
  tutorial_id: string;
  title: string;
  project_id: string | null;
  project_path: string | null;
  step: TutorialStepView | null;
  satisfied_step_ids: string[];
  /**
   * #2138 — whether an earlier step is reachable.
   *
   * Optional so older fixtures and a backend that predates the trail both read
   * as "nowhere to go back to" rather than as a type error.
   */
  can_go_back?: boolean;
  /**
   * #2138 — whether the reader has walked back and is behind their furthest step.
   *
   * A revisited step reports `satisfied` whatever its condition now says, so a
   * surface that reads `satisfied` as "you just did this" needs this to tell
   * the two apart.
   */
  revisiting?: boolean;
  status: "active" | "complete" | "error";
  error: string | null;
  /**
   * Every scripted terminal that is open, one per surface (#2083).
   *
   * A list rather than a single value: an AI Block runs in a terminal of its
   * own while the chat that asked for it stays open, so the frontend adopts a
   * set of tabs rather than swapping one for another.
   */
  replays: TutorialReplayView[];
  /** The whole tutorial's read-only step outline; optional for older fixtures. */
  steps?: TutorialStepOutline[];
}

export interface TutorialStartRequest {
  source_kind: TutorialSourceKind;
  source_id: string;
  tutorial_id: string;
  /** FR-066 — deletes the previous tutorial project and starts fresh. */
  restart: boolean;
}

export interface TutorialProgressGroup {
  source_kind: TutorialSourceKind;
  source_id: string;
  label: string;
  completed: number;
  total: number;
}

/** FR-076 — grouped counts only; there is deliberately no aggregate. */
export interface TutorialProgressResponse {
  groups: TutorialProgressGroup[];
}

/** FR-088 — the directories a clear would delete, named before it happens. */
export interface TutorialClearPreviewResponse {
  directories: string[];
}

export interface TutorialClearResponse {
  deleted_directories: string[];
}

/** FR-079 — whether the product should volunteer the work-import offer. */
export interface TutorialUnlockResponse {
  work_import_offer_pending: boolean;
}

/**
 * HTTP 409 from `POST /sessions` means another tutorial is already running.
 *
 * Named here so the Learning Center can answer it the way the spec's edge case
 * requires — state that one tutorial runs at a time and offer to leave the
 * current one — rather than showing a raw error string.
 */
export const TUTORIAL_SESSION_CONFLICT_STATUS = 409;

export const learningCenterApi = {
  getTutorialCatalogue: () => apiFetch<TutorialCatalogueResponse>("/api/tutorials/catalogue"),

  /** Resolves to `null` when no tutorial is running. */
  getActiveTutorialSession: () =>
    apiFetch<TutorialSessionResponse | null>("/api/tutorials/sessions/active"),

  /** 409 when another session is active — see the conflict constant above. */
  startTutorialSession: (body: TutorialStartRequest) =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),

  /**
   * FR-053 — ask the backend to re-read state no mapped event reaches.
   *
   * `file.changed` is filtered to `ADR036_FILE_ALLOWLIST`, so a condition on a
   * TIFF or a Zarr store is never event-driven; this is the path that covers it.
   */
  evaluateActiveTutorialStep: () =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions/active/evaluate", {
      method: "POST",
    }),

  /**
   * FR-052 — report a named user-interface event.
   *
   * The only completion path that originates in the frontend, and it exists
   * because some product actions (enlarging a panel, opening a tab) leave no
   * backend state for a condition to read.
   *
   * `target` is what the event acted on, for the events that declare a target
   * argument (#2063): the block type behind `node_selected` and
   * `block_source_viewed`, the plot id behind `plot_rendered`. The pairing is
   * core-owned (`scistudio.tutorials.conditions.UI_EVENT_SPECS`); a bare name
   * stays a complete report for every event.
   */
  reportTutorialUiEvent: (name: string, target?: string) =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions/active/ui-event", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(target === undefined ? { name } : { name, target }),
    }),

  /**
   * #2061 — run the current step's user-triggered action.
   *
   * The backend performs the trigger's actions and settles the registries
   * before answering, so whatever the button claimed to do has happened by
   * the time the response renders. A failure leaves the session on the same
   * step and the press can be retried.
   */
  triggerActiveTutorialStep: () =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions/active/trigger", {
      method: "POST",
    }),

  /**
   * #2083 — report that a scripted reply has finished playing.
   *
   * The scripted agent window reveals a transcript at a speaking pace, so the
   * files its segments bind are held back at press time and land on this call
   * instead: the block appears when the agent finishes saying it wrote one,
   * not ten seconds before it got there. The response is the re-judged
   * session.
   *
   * Safe to post when nothing is pending, which is what lets a surface fire it
   * without knowing whether this particular reply bound anything.
   */
  settleActiveTutorialReplay: () =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions/active/replay-settled", {
      method: "POST",
    }),

  /** FR-012 — advance a reading step the user has finished reading. */
  continueActiveTutorialStep: () =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions/active/continue", {
      method: "POST",
    }),

  /**
   * #2138 — go back to the step before this one.
   *
   * A cursor move over steps already entered, so nothing is re-run. A press
   * with nowhere to go returns the current step unchanged.
   */
  backActiveTutorialStep: () =>
    apiFetch<TutorialSessionResponse>("/api/tutorials/sessions/active/back", {
      method: "POST",
    }),

  /** FR-090 — leave at any step; the session is preserved for later. */
  leaveActiveTutorialSession: () =>
    apiFetch<void>("/api/tutorials/sessions/active/leave", { method: "POST" }),

  getTutorialProgress: () => apiFetch<TutorialProgressResponse>("/api/tutorials/progress"),

  previewTutorialDataClear: () =>
    apiFetch<TutorialClearPreviewResponse>("/api/tutorials/data/clear-preview"),

  clearTutorialData: () =>
    apiFetch<TutorialClearResponse>("/api/tutorials/data/clear", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ confirm: true }),
    }),

  getTutorialUnlock: () => apiFetch<TutorialUnlockResponse>("/api/tutorials/unlock"),

  dismissTutorialUnlock: () => apiFetch<void>("/api/tutorials/unlock/dismiss", { method: "POST" }),
};

// ---------------------------------------------------------------------------
// Reading pages (#2084)
//
// A reading tutorial's step content lives in markdown pages under the
// tutorial's `assets/pages/`, served by
// `GET /api/tutorials/{source_kind}/{source_id}/{tutorial_id}/pages/{name}`.
// Serving IS progress: the backend records `page_reached` for the served page
// (see `_page` in `scistudio/api/routes/tutorials.py`), and that record is the
// only thing that satisfies a reading step's condition. The frontend never
// reports "the user read this" separately — asking for the page is the report.
//
// These live outside `learningCenterApi` deliberately: pages are text, not
// JSON, so they cannot go through `apiFetch`, and the reading surface passes
// `fetchTutorialPage` around as a plain function.
// ---------------------------------------------------------------------------

/** The triple addressing one tutorial's assets (§6.1.6 catalogue entry key). */
export interface TutorialAssetKey {
  source_kind: string;
  /** `""` for core — the URL keeps its empty middle segment, which the
   * backend routes explicitly (`/{source_kind}//{tutorial_id}/...`). */
  source_id: string;
  id: string;
}

/** The URL of one reading page, named without its extension.
 *  ADR-055 Spec 0: carries the configured mount prefix via apiUrl (a no-op
 *  under the default root mount). */
export function tutorialPageUrl(key: TutorialAssetKey, page: string): string {
  const kind = encodeURIComponent(key.source_kind);
  const source = encodeURIComponent(key.source_id);
  const tutorial = encodeURIComponent(key.id);
  return apiUrl(`/api/tutorials/${kind}/${source}/${tutorial}/pages/${encodeURIComponent(page)}`);
}

/**
 * Fetch one reading page's markdown.
 *
 * Callers that care about progress follow a successful fetch with
 * `evaluateActiveTutorialStep`, because the backend records the page on serve
 * but only re-judges the step when asked.
 */
export async function fetchTutorialPage(key: TutorialAssetKey, page: string): Promise<string> {
  const response = await fetch(tutorialPageUrl(key, page));
  if (!response.ok) {
    throw new ApiError(`Could not load page "${page}" (HTTP ${response.status})`, response.status);
  }
  return await response.text();
}
