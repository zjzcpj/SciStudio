"""SciStudio MCP server (ADR-040 FastMCP migration).

This sub-package owns the Model Context Protocol (MCP) server that
SciStudio exposes to a locally installed Claude Code (or Codex) CLI. The
server is the *only* surface through which the embedded coding agent
reads workflow state, mutates configuration, or queries data — the
agent never imports SciStudio directly.

ADR-040 status:

* **S40a (skeleton)** — class signatures, ``@mcp.tool()``-decorated
  function stubs with Pydantic result models, and a CLI entry-point
  stub.
* **I40a (Phase 2a — this PR)** — 26 tool function bodies, FastMCP
  ``inputSchema`` generation, ``warnings`` soft-validation per
  §3.2a, standalone-bridge runtime wiring, and ``_render_project_context``
  per §3.3 (closes #825).

The 26 tools are split across four modules by responsibility:

* :mod:`scistudio.ai.agent.mcp.tools_workflow` — category (a):
  workflow inspection and execution (10 tools, includes
  ``finish_ai_block`` from ADR-035 §3.5).
* :mod:`scistudio.ai.agent.mcp.tools_authoring` — category (b):
  block authoring helpers (5 tools).
* :mod:`scistudio.ai.agent.mcp.tools_inspection` — category (c):
  run and data inspection (7 tools).
* :mod:`scistudio.ai.agent.mcp.tools_qa` — category (d): documentation
  and project Q&A (4 tools).
* :mod:`scistudio.ai.agent.mcp.tools_library` — category (f): promotion
  into the personal tool library (1 tool, ADR-053 FR-011).

FastMCP discovers tools by ``@mcp.tool()`` decorator on the module-scope
:data:`scistudio.ai.agent.mcp.server.mcp` instance — there is no longer a
separate ``TOOL_REGISTRY`` tuple. Consumers that need the tool
catalogue call ``await mcp.list_tools()`` directly (see
:mod:`scistudio.ai.agent.system_prompt` for the rendering side).

See ``docs/adr/ADR-040.md`` §3.1-§3.3 for the full migration design.

ADR-055 Spec 1 (FR-004) adds the audience-tag surface:
:data:`AUDIENCE_EXTERNAL_TAG` marks a tool as external-audience only — the
WebMCP HTTP bridge catalogue includes it, the local socket transport's
``tools/list`` filters it out, and untagged tools stay visible on both.
The constant is defined in :mod:`scistudio.ai.agent.mcp.server` (the module
that owns the registry) and re-exported here so tool modules can import it
from the package root.
"""

from __future__ import annotations

# Eagerly import the tool modules so the @mcp.tool decorators run at
# package import time and FastMCP's registry is fully populated by the
# time ``await mcp.list_tools()`` fires.
from scistudio.ai.agent.mcp import (  # noqa: F401
    tools_authoring,
    tools_inspection,
    tools_library,
    tools_plot,
    tools_qa,
    tools_workflow,
)
from scistudio.ai.agent.mcp.server import AUDIENCE_EXTERNAL_TAG, MCPServer, mcp

__all__ = ["AUDIENCE_EXTERNAL_TAG", "MCPServer", "mcp"]
