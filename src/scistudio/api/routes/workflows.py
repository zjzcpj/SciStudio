"""Workflow CRUD and execution endpoints."""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from scistudio.api.deps import get_runtime
from scistudio.api.routes import workflow_watcher
from scistudio.api.runtime import WORKFLOW_ENTITY_CLASS, ApiRuntime, WorkflowRun
from scistudio.api.runtime._runs import WorkflowAlreadyRunningError
from scistudio.api.runtime._workflows import WorkflowIdConflictError
from scistudio.api.schemas import (
    CancelPropagationResponse,
    ExecuteFromRequest,
    ExecuteFromResponse,
    ExecuteWorkflowRequest,
    WorkflowCreate,
    WorkflowEdge,
    WorkflowExecutionResponse,
    WorkflowNode,
)
from scistudio.blocks.base.state import BlockState
from scistudio.engine.events import WORKFLOW_CHANGED, EngineEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])
RuntimeDep = Annotated[ApiRuntime, Depends(get_runtime)]
_API_SOURCES = {"canvas", "agent", "gitRestore", "import", "external"}
_NO_ACTIVE_PROJECT_MESSAGE = "No project is currently open."


def _yaml_error_detail(workflow_id: str, exc: yaml.YAMLError) -> dict[str, Any]:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    location = ""
    if mark is not None:
        location = f" at line {int(mark.line) + 1}, column {int(mark.column) + 1}"
    problem_text = f": {problem}" if problem else ""
    detail: dict[str, Any] = {
        "message": f"Workflow '{workflow_id}' on disk is not valid YAML{location}{problem_text}.",
        "error": str(exc),
    }
    if mark is not None:
        detail["line"] = int(mark.line) + 1
        detail["column"] = int(mark.column) + 1
    if problem:
        detail["problem"] = str(problem)
    context = getattr(exc, "context", None)
    if context:
        detail["context"] = str(context)
    return detail


class VersionedWorkflowResponse(BaseModel):
    """Workflow response with ADR-045 server-authoritative state version."""

    id: str
    version: str = Field(default="1.0.0", description="Workflow YAML/schema version.")
    state_version: int = Field(description="ADR-045 state version, monotonic per workflow.")
    workflow_version: str = Field(default="1.0.0", description="Workflow YAML/schema version.")
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    entity_class: str = WORKFLOW_ENTITY_CLASS
    entity_id: str
    source: str | None = None
    source_id: str | None = None
    kind: str = "current"
    timestamp: str


def _mark_self_write(path: Path) -> None:
    with contextlib.suppress(Exception):
        workflow_watcher.mark_self_write(path)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _get_run_or_404(runtime: ApiRuntime, workflow_id: str) -> WorkflowRun:
    try:
        return runtime.get_run(workflow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _workflow_session_error(exc: RuntimeError) -> HTTPException:
    status_code = 409 if str(exc) == _NO_ACTIVE_PROJECT_MESSAGE else 400
    return HTTPException(status_code=status_code, detail=str(exc))


def _bind_engine_api_url(request: Request) -> None:
    """Publish this API process URL so worker subprocesses can call back.

    ADR-055 Spec 0 (FR-006): the URL is derived from the CONFIGURED external
    base — the ``SCISTUDIO_ENGINE_API_URL`` the CLI (``serve``/``gui``)
    exports at startup — with the app's configured mount prefix
    (``app.state.root_path``) appended exactly once. Workers are subprocesses
    on this host, so the startup-time base stays reachable under a prefix
    while the triggering request's ``base_url`` (whose scheme/host may be
    proxy-facing or simply lack the prefix) is never trusted.

    Documented fallback for the unprefixed local case: when no external base
    was configured (direct ``uvicorn scistudio.api.app:create_app`` runs),
    derive from the incoming request and append the configured prefix the
    same way. The endswith guard keeps repeated execute calls idempotent.
    """
    root_path = getattr(request.app.state, "root_path", "") or ""
    base = os.environ.get("SCISTUDIO_ENGINE_API_URL", "").strip().rstrip("/")
    if not base:
        # Fallback: unprefixed local case — no external base configured.
        base = str(request.base_url).rstrip("/")
    if root_path and not base.endswith(root_path):
        base = f"{base}{root_path}"
    os.environ["SCISTUDIO_ENGINE_API_URL"] = base


def _request_source_id(request: Request, body: Any | None = None) -> str | None:
    body_source_id = getattr(body, "source_id", None)
    if isinstance(body_source_id, str):
        return body_source_id
    header_source_id = request.headers.get("X-Source-Id") or request.headers.get("X-Workflow-Source-Id")
    return header_source_id if isinstance(header_source_id, str) else None


def _request_source(request: Request, *, changed_by: str | None = None, default: str = "canvas") -> str:
    header_source = request.headers.get("X-Workflow-Source") or request.headers.get("X-Source")
    explicit = header_source if isinstance(header_source, str) else None
    if explicit in _API_SOURCES:
        return explicit
    if changed_by and changed_by not in {"api", "canvas"}:
        return "agent"
    return default


def _resolved_ports_for_node(runtime: ApiRuntime | None, node: Any) -> Any:
    """Compute the ADR-044 FR-004 ``resolved_ports`` surface for a node.

    Returns a :class:`SubworkflowPortSurface` for ``subworkflow`` /
    ``subworkflow_broken`` nodes (so the editor can render handles from the
    referenced file's ``exposed_ports``), or ``None`` for every other block
    type. Response-only; never persisted. Requires *runtime* (registry + active
    project root); returns ``None`` without it.
    """
    if runtime is None:
        return None
    from scistudio.api.schemas import SubworkflowPortSurface
    from scistudio.workflow.flatten import SUBWORKFLOW_BROKEN_TYPE, SUBWORKFLOW_TYPE, subworkflow_ref_path
    from scistudio.workflow.subworkflow_ports import resolve_port_surface

    if node.block_type not in (SUBWORKFLOW_TYPE, SUBWORKFLOW_BROKEN_TYPE):
        return None
    base_dir = str(runtime.active_project.path) if runtime.active_project else "."
    ref = subworkflow_ref_path(node)
    surface = resolve_port_surface(ref, base_dir, registry=runtime.block_registry)
    return SubworkflowPortSurface.model_validate(surface)


def _workflow_response(
    definition: Any,
    *,
    state_version: int,
    source: str | None = None,
    source_id: str | None = None,
    kind: str = "current",
    timestamp: str | None = None,
    runtime: ApiRuntime | None = None,
) -> VersionedWorkflowResponse:
    return VersionedWorkflowResponse(
        id=definition.id,
        version=definition.version,
        state_version=state_version,
        workflow_version=definition.version,
        description=definition.description,
        nodes=[
            WorkflowNode(
                id=node.id,
                block_type=node.block_type,
                config=node.config,
                execution_mode=node.execution_mode,
                layout=node.layout,
                resolved_ports=_resolved_ports_for_node(runtime, node),
            )
            for node in definition.nodes
        ],
        edges=[WorkflowEdge(source=edge.source, target=edge.target) for edge in definition.edges],
        metadata=definition.metadata,
        entity_id=definition.id,
        source=source,
        source_id=source_id,
        kind=kind,
        timestamp=timestamp or _now_iso(),
    )


async def _emit_workflow_changed(
    runtime: ApiRuntime,
    *,
    workflow_id: str,
    changed_by: str | None,
    source: str,
    source_id: str | None,
    kind: str,
    path: str | None = None,
) -> dict[str, Any]:
    """Broadcast a ``workflow.changed`` event on the shared EventBus.

    Originally introduced by #718 part (a) carrying a ``revision`` field; the
    counter was removed in ADR-039 §5.2 / D39-2.1. The event itself still
    fires after every successful workflow write so other browser tabs (or
    the embedded coding agent's WS subscriber) can invalidate their cached
    view. Cross-process / cross-session durable history now lives in git.
    """
    version = runtime.bump_workflow_version(workflow_id)
    runtime.mark_workflow_first_party_write(workflow_id, version, path=runtime.workflow_path(workflow_id), kind=kind)
    payload = runtime.versioned_change_payload(
        entity_class=WORKFLOW_ENTITY_CLASS,
        entity_id=workflow_id,
        version=version,
        source=source,
        source_id=source_id,
        kind=kind,
        workflow_id=workflow_id,
        path=path,
        changed_by=changed_by,
    )
    await runtime.event_bus.emit(
        EngineEvent(
            event_type=WORKFLOW_CHANGED,
            data=payload,
        )
    )
    return payload


@router.get("/list", response_model=list[str])
async def list_workflows(runtime: RuntimeDep) -> list[str]:
    """List workflow IDs available in the active project."""
    try:
        return runtime.list_project_workflows()
    except RuntimeError as exc:
        raise _workflow_session_error(exc) from exc


@router.post("/import", response_model=VersionedWorkflowResponse)
async def import_workflow(file: UploadFile, runtime: RuntimeDep) -> VersionedWorkflowResponse:
    """Import an external YAML workflow file into the active project."""
    if not file.filename or not file.filename.endswith((".yaml", ".yml")):
        raise HTTPException(status_code=400, detail="Only .yaml/.yml files are accepted.")
    try:
        content = await file.read()
        import tempfile

        from scistudio.workflow.serializer import load_yaml, save_yaml

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="wb") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            definition = load_yaml(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        # #1836: reject an import that would create a duplicate-id collision.
        conflict = runtime.find_workflow_id_conflict(definition.id)
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=str(WorkflowIdConflictError(definition.id, conflict)),
            )

        out_path = runtime.workflow_path(definition.id)
        existed = out_path.exists()
        save_yaml(definition, out_path)
        _mark_self_write(out_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ADR-034 Phase 2: broadcast workflow.changed so any connected client
    # (other browser tabs, the embedded coding agent's WS subscriber)
    # invalidates its cache.
    change = await _emit_workflow_changed(
        runtime,
        workflow_id=definition.id,
        changed_by="import",
        source="import",
        source_id=None,
        kind="modified" if existed else "created",
        path=f"workflows/{definition.id}.yaml",
    )
    return _workflow_response(
        definition,
        state_version=change["version"],
        source=change["source"],
        source_id=change["source_id"],
        kind=change["kind"],
        timestamp=change["timestamp"],
        runtime=runtime,
    )


@router.post("/import-path", response_model=VersionedWorkflowResponse)
async def import_workflow_from_path(body: dict, runtime: RuntimeDep) -> VersionedWorkflowResponse:
    """Import a workflow from a filesystem path (returned by the browse dialog).

    Emits ``workflow.changed`` after the write so other clients refetch
    (ADR-034 Phase 2). Durable history of the change lives in git per
    ADR-039.
    """
    file_path = body.get("path")
    if not file_path:
        raise HTTPException(status_code=400, detail="Missing 'path' field.")
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise HTTPException(status_code=400, detail="Only .yaml/.yml files are accepted.")
    try:
        from scistudio.workflow.serializer import load_yaml, save_yaml

        definition = load_yaml(path)
        # #1836: reject an import that would create a duplicate-id collision.
        conflict = runtime.find_workflow_id_conflict(definition.id)
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=str(WorkflowIdConflictError(definition.id, conflict)),
            )
        out_path = runtime.workflow_path(definition.id)
        existed = out_path.exists()
        save_yaml(definition, out_path)
        _mark_self_write(out_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_id = body.get("source_id") if isinstance(body.get("source_id"), str) else None
    change = await _emit_workflow_changed(
        runtime,
        workflow_id=definition.id,
        changed_by="import-path",
        source="import",
        source_id=source_id,
        kind="modified" if existed else "created",
        path=f"workflows/{definition.id}.yaml",
    )
    return _workflow_response(
        definition,
        state_version=change["version"],
        source=change["source"],
        source_id=change["source_id"],
        kind=change["kind"],
        timestamp=change["timestamp"],
        runtime=runtime,
    )


@router.post("/", response_model=VersionedWorkflowResponse)
async def create_workflow(body: WorkflowCreate, runtime: RuntimeDep, request: Request) -> VersionedWorkflowResponse:
    """Create a new workflow from the supplied graph definition."""
    try:
        existed = runtime.workflow_path(body.id).exists()
        definition = runtime.save_workflow(body.model_dump())
    except WorkflowIdConflictError as exc:
        # #1836: duplicate workflow id within the project → 409 Conflict
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # Cycle detection and other validation errors → 422
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise _workflow_session_error(exc) from exc

    source_id = _request_source_id(request, body)
    source = _request_source(request)
    change = await _emit_workflow_changed(
        runtime,
        workflow_id=definition.id,
        changed_by="create",
        source=source,
        source_id=source_id,
        kind="modified" if existed else "created",
        path=f"workflows/{definition.id}.yaml",
    )
    return _workflow_response(
        definition,
        state_version=change["version"],
        source=change["source"],
        source_id=change["source_id"],
        kind=change["kind"],
        timestamp=change["timestamp"],
        runtime=runtime,
    )


@router.get("/by-path", response_model=VersionedWorkflowResponse)
async def get_workflow_by_path(path: str, runtime: RuntimeDep) -> VersionedWorkflowResponse:
    """Retrieve a workflow by project-relative path (ADR-044 US1 AS3).

    Declared BEFORE the greedy ``/{workflow_id}`` route so ``/api/workflows/
    by-path`` is not swallowed by ``get_workflow`` with ``workflow_id="by-path"``
    (same route-ordering rule as ``/template`` in ``blocks.py``). Used by the
    editor to open a SubWorkflowBlock's referenced file (which may live under
    ``subworkflows/``) in its own tab.
    """
    from pydantic import ValidationError

    try:
        definition = runtime.load_workflow_by_path(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": f"Workflow '{path}' failed schema validation.", "errors": exc.errors()},
        ) from exc
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=_yaml_error_detail(path, exc)) from exc
    return _workflow_response(
        definition,
        state_version=runtime.current_workflow_version(definition.id),
        runtime=runtime,
    )


@router.get("/{workflow_id}", response_model=VersionedWorkflowResponse)
async def get_workflow(workflow_id: str, runtime: RuntimeDep) -> VersionedWorkflowResponse:
    """Retrieve a workflow by its identifier.

    A YAML on disk that fails pydantic validation (e.g. an agent wrote it
    with the wrong edge shape) returns **422** with the structured
    pydantic error list under ``detail.errors`` — NOT 500, which would
    crash the canvas. The agent can then read the same payload via the
    MCP ``get_workflow`` tool and self-correct. The schema itself stays
    strict; we surface the error rather than papering over it.
    """
    from pydantic import ValidationError

    try:
        definition = runtime.load_workflow(workflow_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Workflow '{workflow_id}' on disk failed schema validation.",
                "errors": exc.errors(),
            },
        ) from exc
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=_yaml_error_detail(workflow_id, exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _workflow_response(
        definition,
        state_version=runtime.current_workflow_version(definition.id),
        runtime=runtime,
    )


@router.put("/{workflow_id}", response_model=VersionedWorkflowResponse)
async def update_workflow(
    workflow_id: str,
    body: WorkflowCreate,
    runtime: RuntimeDep,
    request: Request,
) -> VersionedWorkflowResponse:
    """Replace a workflow definition.

    ADR-039 §5.2 / D39-2.1: the in-memory ``If-Match`` revision check has
    been removed. Cross-tab concurrency now relies on the existing
    ``workflow.changed`` event (filesystem watcher echoes external edits)
    plus durable git history (ADR-039) for after-the-fact reconciliation.
    Last-write-wins on the file save itself — same model as VS Code.
    """
    if workflow_id != body.id:
        raise HTTPException(status_code=400, detail="Workflow path/body IDs must match.")

    try:
        definition = runtime.save_workflow(body.model_dump())
    except ValueError as exc:
        # Cycle detection and other validation errors → 422
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise _workflow_session_error(exc) from exc

    # changed_by: prefer an explicit ``X-Changed-By`` header (set by the MCP
    # tool / the embedded agent), else fall back to a generic "api" tag.
    changed_by = request.headers.get("X-Changed-By", "api")
    source_id = _request_source_id(request, body)
    source = _request_source(request, changed_by=changed_by)
    change = await _emit_workflow_changed(
        runtime,
        workflow_id=definition.id,
        changed_by=changed_by,
        source=source,
        source_id=source_id,
        kind="modified",
        path=f"workflows/{definition.id}.yaml",
    )
    return _workflow_response(
        definition,
        state_version=change["version"],
        source=change["source"],
        source_id=change["source_id"],
        kind=change["kind"],
        timestamp=change["timestamp"],
        runtime=runtime,
    )


@router.post("/export-path")
async def export_workflow_to_path(body: dict, runtime: RuntimeDep) -> dict:
    """Export / save a workflow YAML to an arbitrary filesystem path.

    Expects ``{"workflow_id": str, "path": str}``.  The workflow must
    already exist in the project.  The file is written using
    ``save_yaml`` so the output is a valid SciStudio workflow YAML.
    """
    workflow_id = body.get("workflow_id")
    file_path = body.get("path")
    if not workflow_id or not file_path:
        raise HTTPException(status_code=400, detail="Missing 'workflow_id' or 'path' field.")
    try:
        definition = runtime.load_workflow(workflow_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    from scistudio.workflow.serializer import save_yaml

    target = Path(file_path)
    try:
        save_yaml(definition, target)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _mark_self_write(target)
    return {"status": "ok", "path": str(target)}


@router.post("/import-subworkflow")
async def import_subworkflow(body: dict, runtime: RuntimeDep) -> dict:
    """ADR-044 FR-011: import an external workflow file as a project subworkflow.

    Expects ``{"source_path": str}`` (an absolute or project-external path to a
    workflow YAML). Copies it into ``<project>/subworkflows/`` (numeric suffix on
    name collision) and returns ``{"ref_path": "<project-relative>",
    "resolved_ports": {...}}``. The caller writes ``ref_path`` to the
    SubWorkflowBlock's ``config.ref.path`` and refreshes the node's handles from
    ``resolved_ports`` in one step (no reload round-trip).
    """
    source_path = body.get("source_path")
    if not source_path:
        raise HTTPException(status_code=400, detail="Missing 'source_path' field.")
    try:
        ref_path = runtime.import_subworkflow_file(source_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise _workflow_session_error(exc) from exc

    from scistudio.workflow.subworkflow_ports import resolve_port_surface

    base_dir = str(runtime.active_project.path) if runtime.active_project else "."
    resolved = resolve_port_surface(ref_path, base_dir, registry=runtime.block_registry)
    return {"ref_path": ref_path, "resolved_ports": resolved}


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, runtime: RuntimeDep, request: Request) -> None:
    """Delete a workflow.

    #1462 / ADR-045 §3.4: after the unlink, emit a versioned
    ``workflow.changed`` event with ``kind="deleted"`` attributed to
    ``source="canvas"`` (or the ``X-Source`` header) so other clients see a
    user-initiated delete rather than the FS-watcher's ``source="external"``
    echo. ``_emit_workflow_changed`` also marks the write first-party so the
    watcher suppresses the redundant FS-driven event.
    """
    deleted = runtime.delete_workflow(workflow_id)
    if not deleted:
        return
    changed_by = request.headers.get("X-Changed-By", "api")
    source_id = _request_source_id(request)
    source = _request_source(request, changed_by=changed_by)
    await _emit_workflow_changed(
        runtime,
        workflow_id=workflow_id,
        changed_by=changed_by,
        source=source,
        source_id=source_id,
        kind="deleted",
        path=f"workflows/{workflow_id}.yaml",
    )


@router.post("/{workflow_id}/execute", response_model=WorkflowExecutionResponse)
async def execute_workflow(
    workflow_id: str,
    runtime: RuntimeDep,
    request: Request,
    body: ExecuteWorkflowRequest | None = None,
) -> WorkflowExecutionResponse:
    """Start execution of a workflow."""
    try:
        _bind_engine_api_url(request)
        result = runtime.start_workflow(
            workflow_id,
            overwrite_node_ids=set(body.overwrite_node_ids) if body is not None else None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowAlreadyRunningError as exc:
        # #1525: a live run already exists; reject instead of silently
        # orphaning it by starting a second scheduler.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # start_workflow raises ValueError for user-fixable preconditions that
        # are rejected before dispatch rather than surfacing as a 500:
        #   - ADR-044 FR-010 / US6.3: a graph that fails strict validation at
        #     start — e.g. an unresolved SubWorkflowBlock reference or a
        #     reference cycle.
        #   - #1789: a hard validation failure such as a required input port
        #     with no incoming connection.
        # Surface as 422. Mirrors the run-from-here handler below and the
        # schema-validation handlers above.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WorkflowExecutionResponse(**result)


@router.post("/{workflow_id}/pause", response_model=WorkflowExecutionResponse)
async def pause_workflow(workflow_id: str, runtime: RuntimeDep) -> WorkflowExecutionResponse:
    run = _get_run_or_404(runtime, workflow_id)
    await run.scheduler.pause()
    return WorkflowExecutionResponse(workflow_id=workflow_id, status="paused", message="Pause requested.")


@router.post("/{workflow_id}/resume", response_model=WorkflowExecutionResponse)
async def resume_workflow(workflow_id: str, runtime: RuntimeDep) -> WorkflowExecutionResponse:
    run = _get_run_or_404(runtime, workflow_id)
    await run.scheduler.resume()
    return WorkflowExecutionResponse(workflow_id=workflow_id, status="running", message="Workflow resumed.")


@router.post("/{workflow_id}/cancel", response_model=CancelPropagationResponse)
async def cancel_workflow(workflow_id: str, runtime: RuntimeDep) -> CancelPropagationResponse:
    """Cancel an entire workflow."""
    run = _get_run_or_404(runtime, workflow_id)

    await run.scheduler.cancel_workflow()
    block_states = run.scheduler.block_states()
    cancelled = sorted(block_id for block_id, state in block_states.items() if state == BlockState.CANCELLED)
    skipped = sorted(block_id for block_id, state in block_states.items() if state == BlockState.SKIPPED)
    return CancelPropagationResponse(
        cancelled_blocks=cancelled,
        skipped_blocks=skipped,
        skip_reasons=dict(run.scheduler.skip_reasons),
    )


@router.post("/{workflow_id}/blocks/{block_id}/cancel", response_model=CancelPropagationResponse)
async def cancel_block(
    workflow_id: str,
    block_id: str,
    runtime: RuntimeDep,
) -> CancelPropagationResponse:
    """Cancel a single block within a workflow."""
    run = _get_run_or_404(runtime, workflow_id)

    await run.scheduler.cancel_block(block_id)
    block_states = run.scheduler.block_states()
    cancelled = [node_id for node_id, state in block_states.items() if state == BlockState.CANCELLED]
    skipped = [node_id for node_id, state in block_states.items() if state == BlockState.SKIPPED]
    return CancelPropagationResponse(
        cancelled_blocks=sorted(cancelled),
        skipped_blocks=sorted(skipped),
        skip_reasons=dict(run.scheduler.skip_reasons),
    )


@router.post("/{workflow_id}/execute-from", response_model=ExecuteFromResponse)
async def execute_from_workflow(
    workflow_id: str,
    body: ExecuteFromRequest,
    runtime: RuntimeDep,
    request: Request,
) -> ExecuteFromResponse:
    """Re-run a workflow from a specific block using checkpointed inputs.

    Hotfix #992: stamp ``runs.parent_run_id`` on the new run to point at
    the most-recent completed run of this workflow. Per ADR-038 §3.6a the
    new run's ``parent_run_id`` "points at the historical run whose outputs
    are reused"; the checkpoint mirrors the on-disk state at the most
    recent terminal block event, so the most recent completed run is
    semantically that parent. Previously the route called
    ``start_workflow(execute_from=...)`` without forwarding a
    ``parent_run_id``, leaving the column NULL — the Lineage tab had no
    way to render the re-run chain link required by §3.8.
    """
    parent_run_id: str | None = None
    lineage_store = getattr(runtime, "lineage_store", None)
    if lineage_store is not None:
        try:
            recent = lineage_store.list_runs(workflow_id=workflow_id, limit=1)
            if recent:
                parent_run_id = recent[0].get("run_id")
        except Exception:
            # Best-effort: a lineage lookup failure must not block the
            # actual re-run. The run will just have parent_run_id=NULL
            # as the pre-#992 default did.
            logger.warning(
                "execute_from_workflow: parent_run_id lookup failed (non-fatal)",
                exc_info=True,
            )
    try:
        _bind_engine_api_url(request)
        result = runtime.start_workflow(
            workflow_id,
            execute_from=body.block_id,
            parent_run_id=parent_run_id,
            overwrite_node_ids=set(body.overwrite_node_ids),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowAlreadyRunningError as exc:
        # #1525: a live run already exists; reject instead of silently
        # orphaning it by starting a second scheduler.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ExecuteFromResponse(**result)
