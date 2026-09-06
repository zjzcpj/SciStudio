"""ADR-055 Spec 1 — the WebMCP HTTP bridge over the shared FastMCP registry.

An external AI host's page discovers tools over HTTP, registers browser
callbacks with the host's WebMCP API (``document.modelContext`` /
``navigator.modelContext``), and forwards invocations here. Both routes
dispatch through the same module-level FastMCP registry
(:data:`scistudio.ai.agent.mcp.server.mcp`) that serves the local socket
transport — one tool definition, two front doors (ADR-055 §4). This module
adds no business logic beyond dispatch, adaptation, binding checks, and
logging (FR-011); router-internal tool synthesis is forbidden (spec decision
1), so the demo's synthesized ``about_scistudio``/``import_data``/
``write_file``/``read_file``/``run_bash`` tools are deliberately absent.

Transplanted from the hackathon demo (``scistudio-web-demo`` commit
``952f697b``, read-only reference) and hardened per ADR-055 §9.2:

* results adapt through the shared, documented adapter
  :func:`scistudio.ai.agent.mcp.server.adapt_tool_result` (FR-002/FR-003)
  instead of a lossy text-only serialization;
* tool visibility is tag-driven (:data:`AUDIENCE_EXTERNAL_TAG`, FR-004) —
  this catalogue includes external-tagged tools, the socket transport
  excludes them;
* calls bind the caller's believed-active project, and mutation-tagged
  calls with a stale selection are rejected (FR-005);
* both endpoints sit behind one session middleware with a pluggable
  identity-backend seam (FR-006); this spec ships the loopback token
  backend, and the Hub OAuth backend (``adr-055-lab-deployment``) plugs
  into the same seam without router changes;
* call logging records tool name, outcome, and bounded identifiers only —
  never full arguments, file contents, or command bodies (FR-007).

Read-call policy (FR-005, declared explicitly): read-tagged calls are
dispatched WITHOUT the staleness check — a read cannot silently redirect a
write, and a read issued against a changed project simply observes current
state. Read tools that require a project while none is open fail with the
existing no-active-project error mapped through the adapter.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webmcp"])

# The bridge is mounted at this prefix in ``api.app.create_app`` via the
# standard ``app.include_router(router, prefix=...)`` sequence (FR-011).
ROUTE_PREFIX = "/api/webmcp"

# Header carrying the loopback session token on every bridge call (FR-006).
# The matching value is injected into the served page bootstrap as
# ``window.__SCISTUDIO_WEBMCP_TOKEN__`` by ``api.spa.SPAStaticFiles``.
SESSION_TOKEN_HEADER = "x-scistudio-webmcp-token"

# Machine-readable code in the 409 detail body for a stale project binding
# (FR-005); the frontend maps it to "re-fetch the catalogue and retry".
STALE_PROJECT_CODE = "stale_project_context"


# ---------------------------------------------------------------------------
# Session substrate (FR-006): one middleware, pluggable identity backends.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeIdentity:
    """The authenticated identity attached to a bridge call.

    ``subject`` is a bounded identifier safe for logs (``"loopback"`` for
    this spec's backend; a Hub username for the lab backend).
    """

    subject: str
    backend: str


@runtime_checkable
class BridgeIdentityBackend(Protocol):
    """Identity-backend seam for the bridge session middleware.

    ``adr-055-lab-deployment`` adds the Hub OAuth backend against this same
    interface; the router and middleware do not change when it lands.
    """

    def authenticate(self, headers: dict[str, str]) -> BridgeIdentity | None:
        """Return the request's identity, or ``None`` to reject the call."""


class LoopbackTokenBackend:
    """Loopback identity backend: a per-launch random token.

    The token is generated once per backend launch and delivered through the
    served page bootstrap (the ``adr-055-prefix-independence`` SPA injection),
    so only the page this instance served can call the bridge. The threat
    model is single-user loopback; rotation happens on restart.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def authenticate(self, headers: dict[str, str]) -> BridgeIdentity | None:
        presented = headers.get(SESSION_TOKEN_HEADER, "")
        if presented and secrets.compare_digest(presented, self._token):
            return BridgeIdentity(subject="loopback", backend="loopback-token")
        return None


class WebMCPSessionMiddleware:
    """Authenticate bridge requests through the configured identity backend.

    Scoped to ``/api/webmcp/*`` only (the lab spec widens the scope by
    configuration, not by editing this class); every other request passes
    through untouched. Pure ASGI — no response-body buffering — and added
    inside the CORS layer so preflight handling and CORS headers are
    unaffected.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        backend: BridgeIdentityBackend,
        root_path: str = "",
        route_prefix: str = ROUTE_PREFIX,
    ) -> None:
        self.app = app
        self._backend = backend
        self._root_path = root_path
        self._route_prefix = route_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # Under a configured mount prefix (ADR-055 Spec 0 verbatim proxying)
        # the scope path still carries the prefix; strip it before matching.
        inner = path
        if self._root_path and inner.startswith(f"{self._root_path}/"):
            inner = inner[len(self._root_path) :]
        if inner != self._route_prefix and not inner.startswith(f"{self._route_prefix}/"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        identity = self._backend.authenticate(headers)
        if identity is None:
            response = JSONResponse(
                {"detail": "webmcp bridge calls require a valid session token"},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["webmcp_identity"] = identity
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Bridge call contract.
# ---------------------------------------------------------------------------


class ToolCallRequest(BaseModel):
    """One WebMCP tool invocation forwarded from the browser.

    ``project_id`` is the caller's believed-active project identifier,
    acquired from the catalogue's context snapshot (FR-005).
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Tool name as listed by GET /api/webmcp/tools")
    arguments: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = Field(default=None, alias="projectId")


async def build_catalogue(active_project_id: str | None) -> dict[str, Any]:
    """Build the tool catalogue the SPA registers with the host's WebMCP API.

    Entries come straight from the shared FastMCP registry
    (``mcp.list_tools()``) with ``category``/``mutation`` derived from the
    tool's tags through the same helper the socket transport uses, so the
    two surfaces cannot drift. External-audience-tagged tools are INCLUDED
    here (FR-004). The ``context`` snapshot identifies the active project
    at catalogue-fetch time; the caller presents it back on each call so a
    project switch is detectable (FR-005).
    """
    from scistudio.ai.agent.mcp.server import mcp, tool_category_and_mutation

    tools: list[dict[str, Any]] = []
    for entry in await mcp.list_tools():
        category, mutation = tool_category_and_mutation(entry.tags)
        tools.append(
            {
                "name": entry.name,
                "description": entry.description or "",
                "inputSchema": entry.parameters,
                "category": category,
                "mutation": mutation,
            }
        )
    return {"tools": tools, "context": {"projectId": active_project_id}}


def _active_project_id(request: Request) -> str | None:
    active = request.app.state.runtime.active_project
    return active.id if active is not None else None


@router.get("/tools")
async def list_webmcp_tools(request: Request) -> dict[str, Any]:
    """Return the tool catalogue the SPA registers with ``registerTool()``."""
    return await build_catalogue(_active_project_id(request))


@router.post("/call")
async def call_webmcp_tool(request: Request, body: ToolCallRequest) -> dict[str, Any]:
    """Execute one tool and return the adapted MCP-shaped result payload."""
    from scistudio.ai.agent.mcp.server import adapt_tool_result, mcp, tool_category_and_mutation

    known = {t.name: t for t in await mcp.list_tools()}
    entry = known.get(body.name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown tool '{body.name}'")

    _, mutation = tool_category_and_mutation(entry.tags)
    active_project_id = _active_project_id(request)

    # FR-005 project binding: opening another page must not silently redirect
    # an in-flight write to another project. Mutation-tagged calls whose
    # presented project no longer matches the backend's active project are
    # rejected; the caller re-fetches the catalogue and retries.
    if mutation == "write" and body.project_id != active_project_id:
        logger.info(
            "webmcp call rejected: tool=%s outcome=%s presented_project=%s active_project=%s",
            body.name,
            STALE_PROJECT_CODE,
            body.project_id,
            active_project_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": STALE_PROJECT_CODE,
                "message": (
                    "the active project changed since the catalogue was fetched; "
                    "re-fetch GET /api/webmcp/tools and retry"
                ),
                "presentedProjectId": body.project_id,
                "activeProjectId": active_project_id,
            },
        )

    try:
        result = await mcp.call_tool(body.name, body.arguments)
    except Exception as exc:
        # Surfaced to the agent as isError content rather than as a 500
        # (FR-003): a failed tool call is information it can act on, and an
        # HTTP error would reach it as a dead end. The detail is BOUNDED to
        # the exception type name (CodeQL py/stack-trace-exposure / PR #2275
        # review): the message can embed argument values, absolute paths, or
        # internals, which must not cross the wire to an external caller —
        # and must not be logged either (FR-007: type only).
        logger.warning(
            "webmcp call failed: tool=%s outcome=isError error_type=%s",
            body.name,
            type(exc).__name__,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"{type(exc).__name__}: tool call failed; "
                        "detail withheld by the webmcp bridge (check arguments "
                        "and retry, or inspect the local server logs)"
                    ),
                }
            ],
            "isError": True,
        }

    # FR-007 bounded logging: tool name, outcome, and bounded identifiers
    # only. Arguments, file contents, and command bodies are never logged.
    logger.info(
        "webmcp call: tool=%s mutation=%s outcome=ok project=%s",
        body.name,
        mutation,
        active_project_id,
    )
    return adapt_tool_result(result)


__all__ = [
    "ROUTE_PREFIX",
    "SESSION_TOKEN_HEADER",
    "STALE_PROJECT_CODE",
    "BridgeIdentity",
    "BridgeIdentityBackend",
    "LoopbackTokenBackend",
    "ToolCallRequest",
    "WebMCPSessionMiddleware",
    "build_catalogue",
    "router",
]
