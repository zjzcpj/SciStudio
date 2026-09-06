"""MCP server — FastMCP-backed implementation (ADR-040 §3.1).

Owns the module-scope ``fastmcp.FastMCP`` instance the four
``tools_*.py`` modules decorate their tool functions onto, plus the
:class:`MCPServer` lifecycle wrapper preserved from the ADR-033 era so
the FastAPI lifespan in :mod:`scistudio.api.app` and the standalone
``scistudio mcp-bridge`` runtime can construct it by name.

Transport (preserved from pre-FastMCP era so the bridge protocol does
not move):

* **POSIX** — Unix domain socket at the path provided by the caller
  (default ``{project}/.scistudio/mcp.sock``). Line-delimited JSON-RPC.
* **Windows** — TCP loopback on ``127.0.0.1`` with an ephemeral port;
  the port is written to ``<socket_path>.port`` next to the sentinel
  socket-path file so the bridge subprocess can discover it.

The wrapper bridges two impedance mismatches:

1. FastMCP's native ``run_async`` blocks for the lifetime of the
   server; the FastAPI lifespan wants ``start()``/``stop()`` that
   return promptly while a background task owns the serve loop.
2. The bridge subprocess wants a single blocking ``await server.serve()``
   call. ``serve()`` is therefore the merge of ``start()`` + ``wait``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-scope FastMCP instance (ADR-040 §3.1).
#
# Tool modules (``tools_workflow.py`` etc.) import this and decorate their
# functions with ``@mcp.tool(name=..., tags={...})``. FastMCP auto-discovers
# them and exposes via ``await mcp.list_tools()`` (used by
# :mod:`scistudio.ai.agent.system_prompt._render_tool_catalog`).
# ---------------------------------------------------------------------------

mcp: FastMCP = FastMCP(name="scistudio-mcp", version="0.1.0")
"""Module-scope FastMCP instance (ADR-040 §3.1)."""


AUDIENCE_EXTERNAL_TAG = "audience:external"
"""ADR-055 Spec 1 (FR-004): tag marking a tool as external-audience only.

A tool registered with this tag appears in the WebMCP HTTP bridge catalogue
(:mod:`scistudio.api.routes.webmcp`) but is filtered out of the local socket
transport's ``tools/list`` — local agents already have native file/shell
capability and must not pay for external-only tools. Visibility defaults to
both transports; the filter is opt-in per tool tag.
"""


def tool_category_and_mutation(tags: Iterable[str] | None) -> tuple[str, str]:
    """Derive the ``(category, mutation)`` pair from a tool's tag set.

    Single derivation point shared by the socket transport's ``tools/list``
    ``_meta`` block and the webmcp bridge catalogue so the two surfaces
    cannot drift. ``mutation`` is ``"write"`` when the tag set carries the
    ``write`` tag, else ``"read"``; ``category`` comes from the first
    ``category:*`` tag, else ``"uncategorised"``.
    """
    tag_set = set(tags or ())
    category = next(
        (t.split(":", 1)[1] for t in tag_set if t.startswith("category:")),
        "uncategorised",
    )
    mutation = "write" if "write" in tag_set else "read"
    return category, mutation


# JSON-RPC 2.0 error codes preserved for the line-delimited transport
# adapter below.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_POSIX_SOCKET_PATH_LIMIT_BYTES = 100

# Issue #2019 — upper bound on how long ``MCPServer.stop()`` waits for the
# accept loop and the per-client handlers to unwind after their transports
# have been closed. ``stop()`` runs inline on the request that rebinds the
# server to a newly created/opened project, so it must always return.
_STOP_GRACE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# MCPServer lifecycle wrapper — preserved name + shape from ADR-033 era.
# ---------------------------------------------------------------------------


class MCPServer:
    """Thin lifecycle wrapper around the FastMCP server.

    Preserves the constructor signature + ``start``/``stop``/``serve``
    surface the FastAPI lifespan (:mod:`scistudio.api.app`) and the
    standalone-bridge runtime (:mod:`scistudio.ai.agent.mcp.runtime`)
    already call, while delegating dispatch + ``inputSchema`` generation
    to FastMCP.

    Transport stays line-delimited JSON-RPC over a Unix socket (POSIX)
    or TCP loopback (Windows) so the existing bridge subprocess
    (``scistudio mcp-bridge``) and Claude Code's MCP client implementation
    keep working without protocol churn.

    Parameters
    ----------
    socket_path
        Filesystem path for the Unix domain socket (POSIX). On Windows
        this is treated as a sibling sentinel file: the actual TCP port
        is written to ``<socket_path>.port`` next to it.
    project_dir
        Project workspace root. Threaded through to tool handlers via
        :func:`scistudio.ai.agent.mcp._context.set_context` so they can
        resolve relative paths without consulting global state.
    """

    socket_path: Path
    project_dir: Path

    def __init__(self, socket_path: Path, project_dir: Path) -> None:
        self._requested_socket_path = socket_path
        self.socket_path = socket_path
        self.project_dir = project_dir
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None
        # Issue #2019 — live client transports, so ``stop()`` can hang up on
        # them. ``AbstractServer.close()`` only retires the listener; it leaves
        # established connections running, and since Python 3.12
        # ``wait_closed()`` blocks until every handler task has returned. With
        # ``_handle_client`` looping on ``readline()`` until the peer goes away,
        # an attached client would otherwise make ``stop()`` wait forever.
        self._clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        """Bind the transport and start accepting JSON-RPC requests.

        Idempotent: a second call while already started is a no-op.

        Returns after the listener is bound, so the FastAPI lifespan
        can move on to other startup work while the accept loop runs
        as a background asyncio task owned by the asyncio server.
        """
        if self._server is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            # TCP loopback fallback — asyncio's named-pipe API on
            # Windows is partial and varies by Python build.
            self._server = await asyncio.start_server(self._handle_client, host="127.0.0.1", port=0)
            sockets = self._server.sockets or ()
            if sockets:
                self._port = int(sockets[0].getsockname()[1])
                port_file = self.socket_path.with_suffix(self.socket_path.suffix + ".port")
                port_file.parent.mkdir(parents=True, exist_ok=True)
                port_file.write_text(str(self._port), encoding="utf-8")
        else:
            requested_socket_path = self._requested_socket_path
            socket_path = _posix_bind_socket_path(requested_socket_path)
            _unlink_if_present(requested_socket_path)
            if socket_path != requested_socket_path:
                pointer_path = _posix_socket_pointer_path(requested_socket_path)
                _unlink_if_present(pointer_path)
                socket_path.parent.mkdir(parents=True, exist_ok=True)
                _unlink_if_present(socket_path)
                pointer_path.write_text(str(socket_path), encoding="utf-8")
            self.socket_path = socket_path
            self._server = await asyncio.start_unix_server(self._handle_client, path=str(socket_path))

        logger.info(
            "MCPServer: listening on %s (project_dir=%s)",
            self._port or self.socket_path,
            self.project_dir,
        )

    async def stop(self) -> None:
        """Stop accepting connections and tear down the transport.

        Retires the listener, hangs up on every still-attached client, then
        waits — with a bounded grace period — for the handler tasks to unwind.

        Issue #2019: closing the listener alone is not enough. Established
        connections survive ``close()``, and from Python 3.12 on
        ``wait_closed()`` does not return until each handler task has finished.
        Because ``_handle_client`` blocks on ``readline()`` until its peer
        disconnects, an attached client (an AI Chat session holding the
        project's MCP transport, say) used to pin this coroutine forever —
        which stalled the ``POST /api/projects/`` request that rebinds the
        server and left the GUI wedged behind its modal. Dropping the client
        transports makes those handlers observe EOF and return; the timeout is
        a backstop so a wedged handler degrades to a warning instead of a hang.

        The drain below is load-bearing, not tidiness. ``writer.close()`` only
        *schedules* a transport teardown. Returning before those transports have
        actually finished leaves them to be finalized by the garbage collector,
        and on Python 3.13 a server-attached transport reaped after
        ``wait_closed()`` has already cleared the server's waiter list raises
        ``TypeError: 'NoneType' object is not iterable`` out of
        ``_SelectorTransport.__del__`` — an unraisable exception that surfaces
        against whatever unrelated code happens to be running at collection
        time. Awaiting each transport keeps the teardown inside this coroutine,
        where the grace period still bounds it.
        """
        if self._server is None:
            return
        self._server.close()
        clients = list(self._clients)
        for writer in clients:
            with contextlib.suppress(Exception):
                writer.close()

        async def _drain() -> None:
            for writer in clients:
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            await self._server.wait_closed()  # type: ignore[union-attr]

        try:
            await asyncio.wait_for(_drain(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            logger.warning(
                "MCPServer.stop: client handlers still running after %.1fs; abandoning them",
                _STOP_GRACE_SECONDS,
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("MCPServer.stop: wait_closed raised", exc_info=True)
        self._clients.clear()
        if sys.platform != "win32":
            actual_socket_path = self.socket_path
            requested_socket_path = self._requested_socket_path
            _unlink_if_present(actual_socket_path)
            if actual_socket_path != requested_socket_path:
                _unlink_if_present(_posix_socket_pointer_path(requested_socket_path))
                _unlink_if_present(requested_socket_path)
            self.socket_path = requested_socket_path
        else:
            port_file = self.socket_path.with_suffix(self.socket_path.suffix + ".port")
            with contextlib.suppress(OSError):
                if port_file.exists():
                    os.unlink(port_file)
        self._server = None
        self._port = None
        logger.info("MCPServer: stopped")

    @property
    def port(self) -> int | None:
        """Bound TCP port on Windows transport, or ``None`` on POSIX."""
        return self._port

    async def serve(self) -> None:
        """Bind transport and block until shutdown.

        Convenience entry point for the standalone-bridge ``mcp-bridge``
        subprocess, which awaits a single coroutine for its lifetime.
        Equivalent to ``await start()`` followed by waiting for the
        server's accept loop to exit (which only happens via
        ``stop()`` or process termination).
        """
        await self.start()
        if self._server is None:  # pragma: no cover - start always sets _server
            return
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Per-connection accept loop — line-delimited JSON-RPC framing.
    # ------------------------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read line-delimited JSON-RPC frames and write responses."""
        peer = writer.get_extra_info("peername") or writer.get_extra_info("sockname")
        logger.debug("MCPServer: client connected: %s", peer)
        # Issue #2019 — register the transport so ``stop()`` can hang up on a
        # client that is idle mid-``readline()`` rather than waiting on it.
        self._clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response: dict | None
                try:
                    request = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    response = _error_response(None, _PARSE_ERROR, f"parse error: {exc}")
                else:
                    response = await self.dispatch(request)
                if response is None:
                    # Notification — JSON-RPC 2.0 forbids a response.
                    continue
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("MCPServer: per-client loop crashed")
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.debug("MCPServer: client disconnected: %s", peer)

    # ------------------------------------------------------------------
    # Dispatch (the public surface tests exercise directly).
    # ------------------------------------------------------------------

    async def dispatch(self, request: dict) -> dict | None:
        """Route one decoded JSON-RPC request through FastMCP.

        Recognised methods:

        * ``"initialize"`` — MCP handshake; returns server capabilities.
        * ``"tools/list"`` — enumerates the registered tools.
        * ``"tools/call"`` — invokes one tool by name + arguments.

        Returns ``None`` for JSON-RPC notifications (no ``id`` field).
        Per the JSON-RPC 2.0 spec the server MUST NOT respond to
        notifications; the connection loop drops the frame instead of
        writing a bogus error envelope. Strict MCP clients (Codex 2026)
        treat an unexpected message on the transport as fatal and tear
        down the connection — that's the path that surfaced this bug.
        """
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        is_notification = "id" not in request

        if not isinstance(method, str):
            if is_notification:
                return None
            return _error_response(req_id, _INVALID_REQUEST, "missing 'method'")

        try:
            if method == "initialize":
                return _ok(
                    req_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "scistudio-mcp", "version": "0.1.0"},
                    },
                )
            if method == "tools/list":
                fastmcp_tools = await mcp.list_tools()
                tools = []
                for entry in fastmcp_tools:
                    tags = set(entry.tags or set())
                    if AUDIENCE_EXTERNAL_TAG in tags:
                        # ADR-055 Spec 1 (FR-004): external-audience tools are
                        # served by the WebMCP HTTP bridge catalogue only; the
                        # local socket transport filters them out. Untagged
                        # tools stay visible on both transports.
                        continue
                    category, mutation = tool_category_and_mutation(tags)
                    tools.append(
                        {
                            "name": entry.name,
                            "description": entry.description or "",
                            "inputSchema": entry.parameters,
                            "_meta": {"category": category, "mutation": mutation},
                        }
                    )
                return _ok(req_id, {"tools": tools})
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(name, str):
                    return _error_response(req_id, _INVALID_PARAMS, "missing tool 'name'")
                # Pre-check tool existence so the unknown-tool error
                # surfaces as JSON-RPC METHOD_NOT_FOUND (-32601) rather
                # than INVALID_PARAMS (-32602).
                known_tools = await mcp.list_tools()
                if name not in {t.name for t in known_tools}:
                    return _error_response(req_id, _METHOD_NOT_FOUND, f"unknown tool '{name}'")
                try:
                    result = await mcp.call_tool(name, arguments)
                except Exception as exc:
                    return _error_response(
                        req_id,
                        _INVALID_PARAMS,
                        f"call_tool failed for {name}: {type(exc).__name__}: {exc}",
                    )
                content_text = json.dumps(serialise_result(result), default=str)
                return _ok(
                    req_id,
                    {"content": [{"type": "text", "text": content_text}]},
                )

            # Notifications (no ``id``) like ``notifications/initialized``
            # or ``notifications/cancelled`` must be silently accepted —
            # the JSON-RPC 2.0 spec forbids responding to them. Unknown
            # request methods still surface as METHOD_NOT_FOUND.
            if is_notification:
                return None
            return _error_response(req_id, _METHOD_NOT_FOUND, f"unknown method '{method}'")
        except Exception as exc:
            logger.exception("MCPServer.dispatch failed for method=%s", method)
            if is_notification:
                return None
            return _error_response(req_id, _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _ok(req_id: int | str | None, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error_response(req_id: int | str | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _posix_socket_pointer_path(socket_path: Path) -> Path:
    return socket_path.with_suffix(socket_path.suffix + ".path")


def _posix_bind_socket_path(socket_path: Path) -> Path:
    if len(str(socket_path).encode("utf-8")) <= _POSIX_SOCKET_PATH_LIMIT_BYTES:
        return socket_path
    digest = hashlib.sha1(str(socket_path).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"scistudio-mcp-{os.getpid()}-{digest}.sock"


def _unlink_if_present(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.exists() or path.is_symlink():
            os.unlink(path)


def serialise_result(result: object) -> object:
    """Coerce a FastMCP ToolResult-like object to a JSON-friendly value.

    Promoted from ``_serialise_result`` to the shared, importable adapter
    contract by ADR-055 Spec 1 (FR-002): the local socket transport (below)
    and the WebMCP HTTP bridge
    (:func:`adapt_tool_result`, used by
    :mod:`scistudio.api.routes.webmcp`) both normalise results through this
    module so the two transports cannot drift.

    FastMCP's ``call_tool`` returns either a ``ToolResult`` (with
    ``structured_content``) or already-coerced primitives depending on
    version. We normalise to the most informative JSON value available.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content is not None:
        out = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    out.append(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    out.append(text)
            else:
                model_dump = getattr(block, "model_dump", None)
                out.append(model_dump() if callable(model_dump) else str(block))
        return out[0] if len(out) == 1 else out
    return result


def adapt_tool_result(result: object) -> dict[str, Any]:
    """Map a FastMCP tool result to the WebMCP bridge response shape.

    ADR-055 Spec 1 (FR-003) — the explicit adapter contract the demo's
    text-only mapping violated. Declared mappings:

    * **structured content** — ``result.structured_content`` is preserved
      verbatim in the top-level ``structuredContent`` field; there is no
      lossy JSON text round-trip.
    * **text content blocks** — passed through unchanged as
      ``{"type": "text", "text": ...}``.
    * **non-text content blocks** — the WebMCP host API consumes text
      content only, so each non-text block (image, audio, embedded
      resource, ...) is replaced by an explicitly marked substitution:
      a text block whose text names the dropped representation and which
      carries ``"substitutedFrom": <original block type>``.
    * **top-level error flag** — propagated from the result's ``isError``
      (or ``is_error``) attribute into the top-level ``isError`` field;
      absent means ``False``. Thrown exceptions never reach this function:
      the router maps them to ``isError`` content itself (FR-003), so a
      failed tool call is information the agent can act on rather than an
      HTTP 5xx.
    * **primitive results** (neither ``content`` nor
      ``structured_content``) — normalised through
      :func:`serialise_result` and wrapped in a single text block.
    """
    structured = getattr(result, "structured_content", None)
    content = getattr(result, "content", None)
    is_error = bool(getattr(result, "isError", getattr(result, "is_error", False)))

    blocks: list[dict[str, Any]] = []
    if content:
        for block in content:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
            if block_type == "text" and text is not None:
                blocks.append({"type": "text", "text": text})
                continue
            declared_type = str(block_type if block_type is not None else type(block).__name__)
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"[webmcp bridge: a content block of type '{declared_type}' is not "
                        "representable for the host and was substituted with this notice]"
                    ),
                    "substitutedFrom": declared_type,
                }
            )
    elif structured is None:
        blocks.append({"type": "text", "text": json.dumps(serialise_result(result), default=str)})

    response: dict[str, Any] = {"content": blocks, "isError": is_error}
    if structured is not None:
        response["structuredContent"] = structured
    return response


__all__ = [
    "AUDIENCE_EXTERNAL_TAG",
    "MCPServer",
    "adapt_tool_result",
    "mcp",
    "serialise_result",
    "tool_category_and_mutation",
]
