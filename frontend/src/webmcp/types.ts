/**
 * Ambient types for the WebMCP browser API (`document.modelContext` /
 * `navigator.modelContext`).
 *
 * WebMCP is a trial-era API: Chrome 149 exposed it on `navigator`, Chrome
 * 150 moved it to `document`, and ChatGPT's desktop host surfaced the
 * `document` form (ADR-055 §9.2) — so both declarations exist and callers
 * MUST probe both (`register.ts` does). The API is absent from `lib.dom`;
 * these declarations follow the explainer at
 * https://github.com/webmachinelearning/webmcp and cover only what this app
 * calls. `modelContext` is optional on purpose: on any browser without the
 * capability it is simply undefined, and the registration path treats that
 * as the normal case rather than an error.
 */

/** One block of an MCP-shaped tool result. */
export interface ToolContentBlock {
  type: "text";
  text: string;
  /** Set by the bridge adapter when this text block substitutes a non-text
   * content block the host cannot consume (ADR-055 Spec 1 FR-003). */
  substitutedFrom?: string;
}

/** What a tool's `execute` callback hands back to the agent. */
export interface ToolResult {
  content: ToolContentBlock[];
  isError?: boolean;
  /** Structured tool output preserved by the bridge adapter (FR-003). */
  structuredContent?: Record<string, unknown>;
}

export interface ToolDefinition {
  name: string;
  description: string;
  /** JSON Schema for the tool's arguments. */
  inputSchema?: Record<string, unknown>;
  execute: (
    args: Record<string, unknown>,
    options?: { signal?: AbortSignal },
  ) => Promise<ToolResult>;
}

export interface RegisterToolOptions {
  /** Aborting this signal unregisters the tool. */
  signal?: AbortSignal;
  /** Secure origins allowed to discover and run this tool. */
  exposedTo?: string[];
}

export interface ModelContext extends EventTarget {
  registerTool(tool: ToolDefinition, options?: RegisterToolOptions): Promise<void>;
  getTools(options?: { fromOrigins?: string[] }): Promise<ToolDefinition[]>;
  executeTool(
    tool: ToolDefinition,
    args: Record<string, unknown>,
    options?: { signal?: AbortSignal },
  ): Promise<ToolResult>;
}

declare global {
  interface Document {
    modelContext?: ModelContext;
  }
  interface Navigator {
    modelContext?: ModelContext;
  }
}
