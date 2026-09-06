"""FastAPI app factory, lifespan, CORS, and realtime endpoints."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scistudio.api.mcp_lifecycle import stop_project_mcp_server
from scistudio.api.routes import (
    ai,
    ai_pty,
    blocks,
    data,
    diagnostics,
    filesystem,
    lint,
    packages,
    plots,
    projects,
    runs,
    tutorials,
    types,
    user_docs,
    user_library,
    work_import,
    workflows,
)
from scistudio.api.routes import (
    git as git_routes,
)
from scistudio.api.routes import workflow_watcher as workflow_watcher_module
from scistudio.api.runtime import ApiRuntime
from scistudio.api.spa import SPAStaticFiles
from scistudio.api.sse import sse_handler
from scistudio.api.ws import websocket_handler
from scistudio.engine.runners.process_handle import ProcessRegistry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and tear down the shared API runtime.

    T-ECA-205: also starts the in-process MCP server (Phase 2 of the
    ADR-033 embedded coding agent cascade). The server listens on a
    project-local socket (POSIX) or TCP loopback port (Windows); the
    ``scistudio mcp-bridge`` subprocess proxies CC stdin/stdout into it.
    Server start is best-effort — if it fails, we log ERROR but let
    FastAPI come up so the rest of the app is usable.
    """
    runtime = ApiRuntime()
    app.state.runtime = runtime
    app.state.registry = ProcessRegistry()

    # ---- ADR-035 §3.10 IPC token ----
    # Audit P1-B (Codex #861-1): the engine must export
    # ``SCISTUDIO_ENGINE_IPC_TOKEN`` BEFORE any AI Block worker is spawned so
    # the worker subprocess inherits it via os.environ and can authenticate
    # internal IPC calls. Without this, every
    # ``POST /api/ai/pty/internal/*`` call returns 401 and the entire AI
    # Block path is dead-on-arrival.
    from scistudio.api.routes.ai_pty import _ensure_ipc_token

    _ensure_ipc_token()

    # ---- ADR-034 Phase 2: workflow filesystem watcher ----
    # Watches the active project's ``workflows/`` directory and republishes
    # YAML mtime/create/delete events as ``workflow.changed`` engine events.
    # The existing ``/ws`` outbound loop forwards that event type, so the
    # browser canvas auto-refetches whenever claude / codex / an external
    # editor mutates a workflow on disk.
    watcher = workflow_watcher_module.WorkflowWatcher(runtime.event_bus)
    workflow_watcher_module.set_active_watcher(watcher)
    app.state.workflow_watcher = watcher
    if runtime.active_project is not None:
        try:
            loop = asyncio.get_running_loop()
            watcher.start_for_project(Path(runtime.active_project.path), loop)
        except Exception:
            import logging

            logging.getLogger(__name__).warning("workflow_watcher: initial start failed", exc_info=True)

    # ---- ADR-039 §3.8: git-state watcher ----
    # NOTE: D39-3.2 (#968) collapsed the previously-separate
    # ``core.versioning.watcher.GitChangeWatcher`` (asyncio-poll) into
    # ``workflow_watcher._GitHeadHandler`` (watchdog Observer) above. The
    # unified watcher attaches a second schedule to the project's
    # ``.git/`` directory inside ``WorkflowWatcher.start_for_project`` and
    # emits ``git.head_changed`` with the canonical ``commit_sha`` field
    # the frontend reads (see ``frontend/src/hooks/useWebSocket.ts``).
    # Project switch already restarts the watcher via
    # ``routes/projects.py::_restart_workflow_watcher``, so a separate
    # git-watcher lifecycle hook is no longer required.

    # ---- Phase 2: MCP context lifecycle ----
    #
    # The actual MCP server binds after a project is opened, through
    # ``api.mcp_lifecycle.ensure_project_mcp_server``. Binding here before
    # an active project exists would force every desktop instance to share
    # the same home-scoped fallback socket, which is not safe across stale
    # packaged backend processes.
    try:
        from scistudio.ai.agent.mcp import _context as _mcp_context

        # ApiRuntime structurally satisfies the MCPContext Protocol once
        # we expose ``project_dir`` as a property — wrap it in a small
        # adapter rather than touching ApiRuntime itself (the AI layer
        # cannot import from ``api/`` and ``api/`` should not know about
        # the MCP Protocol shape).
        class _RuntimeAdapter:
            def __init__(self, rt: ApiRuntime) -> None:
                self._rt = rt

            @property
            def block_registry(self) -> object:
                return self._rt.block_registry

            @property
            def type_registry(self) -> object:
                return self._rt.type_registry

            @property
            def project_dir(self) -> Path | None:
                return Path(self._rt.active_project.path) if self._rt.active_project else None

            @property
            def active_workflow_id(self) -> str | None:
                # ADR-040 Addendum 5 / #1488: surface the runtime field
                # to MCP tools through the protocol member. Defensive
                # getattr so an older ApiRuntime build without the
                # field still satisfies the Protocol (returns None).
                return getattr(self._rt, "active_workflow_id", None)

            @property
            def workflow_runs(self) -> object:
                return self._rt.workflow_runs

            @property
            def event_bus(self) -> object:
                # Exposed so MCP tools can subscribe to engine events
                # (e.g. capture BLOCK_ERROR tracebacks for ``get_run_status``).
                return self._rt.event_bus

            def start_workflow(self, workflow_id: str) -> object:
                return self._rt.start_workflow(workflow_id)

            def register_plot_artifact(
                self,
                artifact_path: str | Path,
                *,
                cache_key: str | None = None,
                workflow_id: str | None = None,
                node_id: str | None = None,
                output_port: str | None = None,
                plot_id: str | None = None,
            ) -> object:
                return self._rt.register_plot_artifact(
                    artifact_path,
                    cache_key=cache_key,
                    workflow_id=workflow_id,
                    node_id=node_id,
                    output_port=output_port,
                    plot_id=plot_id,
                )

        _mcp_context.set_context(_RuntimeAdapter(runtime))  # type: ignore[arg-type]
        app.state.mcp_server = None
    except Exception:
        import logging

        logging.getLogger(__name__).error("MCP context failed to initialize", exc_info=True)
        app.state.mcp_server = None

    try:
        yield
    finally:
        # Stop the FS watcher first so its observer thread does not race
        # against the rest of the teardown.
        try:
            watcher.stop()
        except Exception:
            import logging

            logging.getLogger(__name__).warning("workflow_watcher: stop raised", exc_info=True)
        workflow_watcher_module.set_active_watcher(None)
        # D39-3.2 (#968): the standalone git_watcher was deleted — its
        # ``.git/`` surface is now covered by the unified workflow_watcher
        # observer above. No separate teardown required.
        pending_run_tasks = []
        for run in runtime.workflow_runs.values():
            if not run.task.done():
                run.task.cancel()
                pending_run_tasks.append(run.task)
        if pending_run_tasks:
            await asyncio.gather(*pending_run_tasks, return_exceptions=True)
        app.state.registry.terminate_all(grace_period_sec=5.0)
        await stop_project_mcp_server(app, runtime)
        # Clear the global context so a subsequent app instance starts clean.
        try:
            from scistudio.ai.agent.mcp import _context as _mcp_context

            _mcp_context.set_context(None)
        except Exception:
            pass
        # ADR-039: drop the per-project MetadataStore reference so its
        # sqlite handle is gc-eligible. We cannot call ``close()`` directly
        # because sqlite3.Connection is thread-affine (opened in the
        # FastAPI worker thread; this runs in the lifespan event-loop
        # thread). Setting the module global to None + forcing a gc pass
        # releases the connection on Windows ~99% of the time, which is
        # sufficient for test cleanup.
        try:
            from scistudio.core.metadata_store import set_metadata_store

            set_metadata_store(None)
            import gc

            gc.collect()
        except Exception:
            pass


# First path segments that can never lead a mount prefix: the /api and /ws
# route namespaces live there, so a colliding prefix (e.g. "/api") would make
# "is this path already prefixed?" unanswerable downstream (Codex review on
# PR #2274). Rejected at configuration time — the one place the prefix
# enters the system (FR-008's single normalization point).
RESERVED_ROOT_PATH_SEGMENTS = ("api", "ws")


def normalize_root_path(raw: str | None) -> str:
    """Normalize a configured mount prefix (ADR-055 Spec 0, FR-008).

    This is the single backend normalization point for the mount prefix:
    ``""`` and ``"/"`` both mean "mounted at the root" (the default no-op);
    anything else becomes ``"/prefix"`` — exactly one leading slash, no
    trailing slash, no doubled separators — so ``/prefix``, ``/prefix/`` and
    ``//prefix`` cannot diverge. Call sites MUST NOT concatenate prefixes by
    hand; they read the normalized value from ``app.state.root_path``.

    Raises :class:`ValueError` when the first segment collides with the
    reserved ``/api`` or ``/ws`` route namespaces (see
    ``RESERVED_ROOT_PATH_SEGMENTS``).
    """
    segments = [segment for segment in (raw or "").strip().split("/") if segment]
    if segments and segments[0] in RESERVED_ROOT_PATH_SEGMENTS:
        raise ValueError(
            f"Invalid root path {raw!r}: the first segment {segments[0]!r} is reserved "
            f"({', '.join('/' + s for s in RESERVED_ROOT_PATH_SEGMENTS)} are the API and WebSocket "
            "route namespaces). Choose a prefix that does not collide, "
            "e.g. /user/<name>/scistudio."
        )
    return f"/{'/'.join(segments)}" if segments else ""


class _RootPathGuardMiddleware:
    """Reject requests arriving OUTSIDE the configured mount prefix.

    ADR-055 Spec 0 edge-case contract: while a prefix is configured, the
    unprefixed root MUST NOT silently keep serving — the chosen behavior is
    404 (HTTP) / close-1008 (WebSocket). Serving both forms is forbidden by
    the spec, and redirecting API or WebSocket callers would hide proxy
    misconfiguration behind a success-shaped response.

    Pure ASGI (not BaseHTTPMiddleware) so WebSocket scopes are covered and
    no response-body buffering is added to the hot path. Installed only when
    the prefix is non-empty, so the default mount carries zero overhead.
    """

    def __init__(self, app: ASGIApp, root_path: str) -> None:
        self.app = app
        self.root_path = root_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # Normalize doubled separators here — the single normalization point
        # on the request side (spec §2 edge cases: ``/p//api/...`` must not
        # diverge from ``/p/api/...``). Without this, Starlette strips the
        # prefix and then fails to match ``//api/...`` → a spurious 404.
        collapsed = re.sub(r"/{2,}", "/", path)
        if collapsed != path:
            scope = dict(scope)
            scope["path"] = collapsed
            if "raw_path" in scope:
                scope["raw_path"] = collapsed.encode("ascii", "replace")
            path = collapsed
        if path == self.root_path or path.startswith(f"{self.root_path}/"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Deny the upgrade before any accept: the prefixed deployment does
            # not exist at the unprefixed path.
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Not Found"}, status_code=404)
        await response(scope, receive, send)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # #1741: install console + persistent JSON-line file logging. Idempotent, so
    # it is safe whether the CLI already configured logging (``scistudio gui``)
    # or this is standalone API usage (``uvicorn scistudio.api.app:create_app``).
    from scistudio.utils.logging import configure_logging

    log_level = os.environ.get("SCISTUDIO_LOG_LEVEL", "INFO").upper()
    configure_logging(log_level)

    # #1742: the FastAPI app version derives from the single source of truth.
    from scistudio.version import get_version

    # ADR-055 Spec 0 (FR-001): the configured mount prefix comes from
    # ``SCISTUDIO_ROOT_PATH`` (the CLI mirrors ``--root-path`` into it) and is
    # applied as the FastAPI app-level ``root_path`` — the verbatim-proxy
    # mechanism. uvicorn's own root_path is deliberately unused: modern
    # uvicorn prepends it onto every incoming path, which assumes a
    # prefix-stripping proxy and double-prefixes under ADR-055's verbatim
    # forwarding contract. Empty means "mounted at the root" and is a strict
    # no-op (FR-002).
    root_path = normalize_root_path(os.environ.get("SCISTUDIO_ROOT_PATH", ""))
    app = FastAPI(title="SciStudio API", version=get_version().pep440, lifespan=lifespan, root_path=root_path)
    # Single source the rest of the backend (SPA injection, worker callback
    # URL) reads the normalized prefix from. Never re-parse the env var.
    app.state.root_path = root_path
    cors_origins_raw = os.getenv("SCISTUDIO_CORS_ORIGINS", "").strip()
    if cors_origins_raw == "*":
        origins: list[str] = ["*"]
    elif cors_origins_raw:
        origins = [o.strip() for o in cors_origins_raw.split(",")]
    else:
        origins = [
            "http://localhost:5173",
            "http://localhost:8000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8000",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # #1741: request/exception logging with correlation ids. Added after CORS so
    # it sits OUTERMOST (Starlette runs middleware in reverse add order), seeing
    # every request and any exception that escapes the routes.
    from scistudio.api._logging_middleware import RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)

    # ADR-055 Spec 0: while a prefix is configured, requests outside it get a
    # hard 404 (see _RootPathGuardMiddleware) instead of silently serving both
    # forms. Added last so it sits outermost and rejects before any logging
    # or CORS work happens for out-of-prefix traffic.
    if root_path:
        app.add_middleware(_RootPathGuardMiddleware, root_path=root_path)

    app.include_router(workflows.router)
    app.include_router(blocks.router)
    # ADR-053 §7 — the registered data type listing and the data-type template.
    # Registered beside the block router rather than under it: FR-027 makes the
    # Data types tab independent of the block listing, and a router nested in
    # ``/api/blocks`` would contradict that in the URL if not in the code.
    app.include_router(types.router)
    app.include_router(data.router)
    # ADR-048 SPEC 1: routed previewer session API (additive to data.router).
    app.include_router(data.previews_router)
    # ADR-048 SPEC 2 / #1606: plot-job run + preview-wiring endpoint. Runs a
    # plot job and registers the produced artifact so the frontend can open a
    # routed plot_artifact preview session (producer -> PlotPreviewer link).
    app.include_router(plots.router)
    app.include_router(tutorials.router)
    app.include_router(packages.router)
    # filesystem router must be registered BEFORE projects router because
    # the projects router uses {project_id:path} which would greedily
    # match /api/projects/{id}/tree as a project-id lookup.
    app.include_router(filesystem.router)
    app.include_router(projects.router)
    # ADR-053 §4 — the user-wide library write path. Registered beside the
    # project file endpoints it inverts (FR-007) rather than under them: its
    # target lives outside every project root, so it cannot hang off
    # ``/api/projects/{project_id}``.
    app.include_router(user_library.router)
    app.include_router(ai.router)
    app.include_router(ai_pty.router)
    # ADR-036 §3.3 — server-side ruff lint endpoint for the embedded editor.
    app.include_router(lint.router)
    # ADR-038 §5.2 — ``runs`` router (lineage REST surface, D38-2.4a).
    app.include_router(runs.router)
    # ADR-039 §3.5 — git endpoints (commit / log / diff / restore / branch
    # ops / merge / cherry-pick). D39-2.2b made these live. ADR-039 Addendum 1
    # (#1352) removed the stash CRUD surface (#1353).
    app.include_router(git_routes.router)
    # #1741/#1742: diagnostics — /api/version, /api/client-logs, /api/diagnostics/bundle.
    app.include_router(diagnostics.router)
    # ADR-053 §4 — Bring In My Work session spawn (POST /api/work-import/sessions).
    app.include_router(work_import.router)
    # #2157: the shipped user documentation, read in the Learning Center.
    app.include_router(user_docs.router)

    @app.get("/api/logs/stream")
    async def logs_stream(request: Request) -> object:
        return await sse_handler(request)

    @app.get("/version")
    async def version() -> object:
        # #1742: convenience top-level alias of /api/version for bug reports.
        from scistudio.version import get_version

        return get_version().as_dict()

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        runtime = app.state.runtime
        await websocket_handler(websocket, runtime.event_bus)

    # SPA static files. Must be registered AFTER all /api/* and /ws routes.
    # Resolution depends on the run mode (see ``_resolve_spa_static_dir``):
    #   - Bundled desktop app (``SCISTUDIO_BUNDLED=1``): ONLY the embedded
    #     ``scistudio/api/static/`` is served, so the UI never depends on where
    #     the ``.app`` sits (#1747).
    #   - Editable/dev install: ``<repo-root>/frontend/dist/`` (latest
    #     ``npm run build``) is preferred, falling back to the packaged copy.
    # If neither is present, ``GET /`` redirects to the API docs so users
    # still land on something useful.
    static_dir = _resolve_spa_static_dir()
    if static_dir is not None:
        # base_path drives the runtime bootstrap injection into index.html
        # (ADR-055 Spec 0 FR-003); hashed asset files stay byte-identical.
        app.mount("/", SPAStaticFiles(directory=str(static_dir), html=True, base_path=root_path), name="spa")
    else:

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            # root_path-aware: under a prefix the docs live below it.
            return RedirectResponse(url=f"{root_path}/docs")

    return app


def _resolve_spa_static_dir() -> Path | None:
    """Locate the built SPA assets.

    A bundled/packaged desktop app (``SCISTUDIO_BUNDLED=1``) MUST serve only its
    own embedded ``scistudio/api/static/`` — never the dev walk-up below.
    Otherwise the served frontend would depend on where the ``.app`` physically
    sits: inside a source checkout the walk-up finds a stray ``frontend/dist``
    and serves that; in ``/Applications`` it falls back to the packaged copy.
    Same bundle, different UI (#1747).

    For editable installs (development), ``frontend/dist/`` is preferred because
    it reflects the latest ``npm run build``. The packaged copy at
    ``src/scistudio/api/static/`` is the fallback.

    Returns the first directory that contains an ``index.html``, or ``None`` if
    no built SPA is available.
    """
    packaged = Path(__file__).parent / "static"

    # Bundled desktop app: only ever serve the embedded SPA so the UI is
    # environment-independent (does not depend on the .app's filesystem
    # location). #1747.
    if os.environ.get("SCISTUDIO_BUNDLED") == "1":
        return packaged if (packaged / "index.html").is_file() else None

    # 1. Prefer frontend/dist/ (fresh dev build).
    #    Walk up from ``src/scistudio/api/app.py`` to the repo root.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "frontend" / "dist"
        if (candidate / "index.html").is_file():
            # #406: Warn when frontend/src is newer than the build.
            src_dir = parent / "frontend" / "src"
            if src_dir.exists():
                try:
                    src_mtime = max(
                        (f.stat().st_mtime for f in src_dir.rglob("*") if f.is_file()),
                        default=0.0,
                    )
                    dist_mtime = (candidate / "index.html").stat().st_mtime
                    if src_mtime > dist_mtime:
                        import logging

                        logging.getLogger(__name__).warning(
                            "frontend/dist may be stale (frontend/src has newer files than "
                            "dist/index.html). Run 'cd frontend && npm run build' to rebuild."
                        )
                except Exception:
                    pass
            return candidate
        if (parent / "pyproject.toml").is_file():
            # Reached the repo root without finding frontend/dist/
            break

    # 2. Fall back to packaged static/ (wheel installs).
    if (packaged / "index.html").is_file():
        return packaged

    return None
