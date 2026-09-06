"""FastMCP parity tests (ADR-040 §3.1, I40a Phase 2a).

Asserts the FastMCP-backed MCP server matches the ADR-040 contract:

* 36 tools discoverable via ``await mcp.list_tools()``
  (26 from ADR-040 §3.1 + 1 from Addendum 5 / #1488 + 6 plot tools
  from ADR-048 SPEC 2 + 1 qa tool ``open_gui`` from #1947 + 1 library
  tool ``promote_to_user_library`` from ADR-053 FR-011).
* Every write-class tool's result model has ``next_step: str``.
* ``scaffold_block`` has the widened §3.2a signature with
  ``input_ports`` + ``output_ports`` dict args and a ``warnings`` field.
* ``inputSchema`` is FastMCP-generated (no ``additionalProperties: true``
  fallback from the ADR-033-era hand-rolled JSON-RPC stub).
* §3.2a soft-validation warnings fire on generic-``DataObject`` ports
  and unregistered type names.
* ``finish_ai_block`` returns a ``FinishAIBlockOK | FinishAIBlockError``
  discriminated union.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scistudio.ai.agent.mcp import _context, tools_authoring, tools_workflow
from scistudio.ai.agent.mcp.server import mcp
from scistudio.blocks.registry import BlockRegistry
from scistudio.core.types.registry import TypeRegistry

_EXPECTED_TOOL_NAMES = {
    # category (a) workflow
    "list_blocks",
    "get_block_schema",
    "list_types",
    "get_workflow",
    "validate_workflow",
    "write_workflow",
    # #1912: surgical partial-edit workflow tool
    "edit_workflow",
    "run_workflow",
    "cancel_run",
    "get_run_status",
    "finish_ai_block",
    # ADR-040 Addendum 5 / #1488
    "get_active_workflow_context",
    # category (b) authoring
    "read_block_source",
    "list_block_examples",
    "scaffold_block",
    "reload_blocks",
    "run_block_tests",
    # category (c) inspection
    "get_block_output",
    "inspect_data",
    "preview_data",
    "get_lineage",
    "get_block_config",
    "update_block_config",
    "get_block_logs",
    # category (d) qa
    "search_docs",
    "get_doc",
    "list_data",
    "get_project_info",
    # #1947: open the running GUI in a browser for self-debugging
    "open_gui",
    # category (e) plot (ADR-048 SPEC 2)
    "list_plot_targets",
    "scaffold_plot",
    "list_plot_examples",
    "read_plot_source",
    "validate_plot",
    "run_plot_job",
    # category (f) library (ADR-053 FR-011)
    "promote_to_user_library",
}


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tool registry parity.
# ---------------------------------------------------------------------------


def test_fastmcp_lists_36_tools() -> None:
    """ADR-040 §3.1 + Addendum 5 + ADR-048 SPEC 2 + #1912 + #1947 + ADR-053 FR-011: 36 tools."""
    tools = _run(mcp.list_tools())
    assert len(tools) == 36
    names = {t.name for t in tools}
    assert names == _EXPECTED_TOOL_NAMES, (
        f"missing: {_EXPECTED_TOOL_NAMES - names}; extra: {names - _EXPECTED_TOOL_NAMES}"
    )


def test_write_class_tools_have_next_step() -> None:
    """ADR-040 §3.2: every write-class tool's result model has next_step: str."""
    write_class = {
        "write_workflow",
        "edit_workflow",
        "run_workflow",
        "cancel_run",
        "finish_ai_block",
        "scaffold_block",
        "reload_blocks",
        "run_block_tests",
        "update_block_config",
        # ADR-048 SPEC 2 plot write/run-class.
        "scaffold_plot",
        "run_plot_job",
        # ADR-053 FR-011 library write-class.
        "promote_to_user_library",
    }
    tools = _run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    for name in write_class:
        tool = by_name[name]
        if name == "finish_ai_block":
            # Union[FinishAIBlockOK, FinishAIBlockError]; next_step lives on OK.
            from scistudio.ai.agent.mcp.tools_workflow import FinishAIBlockOK

            assert "next_step" in FinishAIBlockOK.model_fields, f"{name}: FinishAIBlockOK missing next_step field"
            continue
        # FastMCP records the return type — pull from inspect's signature
        # since __annotations__ may carry string forward-refs under
        # ``from __future__ import annotations``.
        sig = inspect.signature(tool.fn)
        return_ann = sig.return_annotation
        # Resolve via tool.fn module globals if still a string.
        if isinstance(return_ann, str):
            return_ann = tool.fn.__globals__.get(return_ann, return_ann)
        target = return_ann if hasattr(return_ann, "model_fields") else None
        assert target is not None, f"{name}: could not resolve return-type model"
        assert "next_step" in target.model_fields, f"{name}: result model missing next_step field"


def test_scaffold_block_signature_widened() -> None:
    """ADR-040 §3.2a (manifest §8.6): scaffold_block accepts input_ports + output_ports dicts."""
    sig = inspect.signature(tools_authoring.scaffold_block)
    params = sig.parameters
    assert "input_ports" in params
    assert "output_ports" in params


# ---------------------------------------------------------------------------
# §3.2a soft validation.
# ---------------------------------------------------------------------------


@dataclass
class _StubRuntime:
    block_registry: BlockRegistry = field(default_factory=BlockRegistry)
    type_registry: TypeRegistry = field(default_factory=TypeRegistry)
    workflow_runs: dict[str, Any] = field(default_factory=dict)
    _project_dir: Path | None = None
    # ADR-040 Addendum 5 / #1488 member of the MCPContext Protocol.
    active_workflow_id: str | None = None

    @property
    def project_dir(self) -> Path | None:
        return self._project_dir


@pytest.fixture
def stub_ctx(tmp_path: Path) -> Iterator[_StubRuntime]:
    runtime = _StubRuntime(_project_dir=tmp_path)
    runtime.type_registry.scan_builtins()
    _context.set_context(runtime)
    yield runtime
    _context.set_context(None)


def test_scaffold_block_warns_on_generic_dataobject_port(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """ADR-040 §3.2a: warning text fires when a port uses generic DataObject."""
    result = _run(
        tools_authoring.scaffold_block(
            name="test_block_dataobject",
            category="process",
            input_ports={"in": {"type": "DataObject"}},
            output_ports={"out": {"type": "DataObject"}},
        )
    )
    assert result.warnings, "expected at least one warning"
    joined = " ".join(result.warnings).lower()
    assert "dataobject" in joined
    assert "list_types" in joined or "concrete type" in joined
    assert Path(result.path).exists()


def test_scaffold_block_warns_on_unregistered_type(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """ADR-040 §3.2a: warning text fires when a port type isn't in TypeRegistry."""
    result = _run(
        tools_authoring.scaffold_block(
            name="test_block_unregistered",
            category="process",
            input_ports={"in": {"type": "ThisTypeDoesNotExist_XYZ"}},
        )
    )
    joined = " ".join(result.warnings).lower()
    assert "unregistered" in joined or "thistypedoesnotexist_xyz" in joined


def test_scaffold_block_emits_live_port_and_run_shape(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """F40-integration F2: scaffold template emits the live API shape.

    The pre-F2 template emitted ``InputPort(name=..., type=Image)`` and
    ``run(self, inputs)`` — the legacy 1-arg arity + the legacy ``type=``
    kwarg that no longer exists on the ``Port`` dataclass. A scaffolded
    block in that shape raised ``TypeError`` at ``reload_blocks``.

    Post-F2 the scaffold template emits
    ``InputPort(name=..., accepted_types=[Image], required=True)`` and
    ``run(self, inputs: dict[str, Any], config: BlockConfig)`` — matching
    ``Block.run`` ABC at ``src/scistudio/blocks/base/block.py`` and the
    ``InputPort`` dataclass at ``src/scistudio/blocks/base/ports.py``.
    """
    result = _run(
        tools_authoring.scaffold_block(
            name="f2_shape_check",
            category="process",
            input_ports={"image": {"type": "Image", "description": "input image"}},
            output_ports={"mask": {"type": "Mask"}},
        )
    )
    body = Path(result.path).read_text(encoding="utf-8")

    # Accepts the live accepted_types= kwarg, NOT the legacy type= kwarg.
    assert "accepted_types=[Image]" in body, (
        "scaffold template must emit InputPort(name=..., accepted_types=[Type]) — "
        "the live Port API has no type= kwarg (F40-integration F2)."
    )
    assert "accepted_types=[Mask]" in body
    assert "type=Image" not in body, (
        "scaffold template MUST NOT emit the legacy ``type=`` kwarg — "
        "it will raise TypeError at reload_blocks (F40-integration F2)."
    )

    # 2-arg run(self, inputs, config) — matches Block.run ABC.
    assert "def run(self, inputs: dict[str, Any], config: BlockConfig)" in body, (
        "scaffold template must emit the 2-arg run(self, inputs, config) signature "
        "matching Block.run ABC (F40-integration F2). The 1-arg form is invalid."
    )

    # BlockConfig import is wired so the type annotation resolves.
    assert "from scistudio.blocks.base.config import BlockConfig" in body


# ---------------------------------------------------------------------------
# inputSchema generation.
# ---------------------------------------------------------------------------


def test_input_schema_rejects_malformed_call() -> None:
    """ADR-040 §3.1: FastMCP-generated inputSchema rejects malformed args before tool body.

    No ``additionalProperties: true`` fallback — FastMCP's strict
    inputSchema generation is the boundary contract.
    """
    tools = _run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["get_block_schema"].parameters
    assert schema.get("additionalProperties") is False, (
        "ADR-040 §3.1 regression: inputSchema must reject extra fields. "
        "Got additionalProperties=" + str(schema.get("additionalProperties"))
    )
    assert "type_name" in schema.get("properties", {}), (
        f"get_block_schema inputSchema missing type_name property: {schema}"
    )
    # Calling with the wrong field name should fail validation at the boundary.
    # FastMCP 3.x raises fastmcp.exceptions.ValidationError (a FastMCPError, not
    # a ToolError/ValueError) for input-schema validation failures; older
    # versions raised ToolError or a pydantic ValueError. Accept either so the
    # boundary-rejection contract holds across the supported fastmcp range.
    from fastmcp.exceptions import ToolError, ValidationError

    with pytest.raises((ValidationError, ToolError, ValueError, TypeError)):
        _run(mcp.call_tool("get_block_schema", {"not_a_param": "x"}))


# ---------------------------------------------------------------------------
# finish_ai_block union return.
# ---------------------------------------------------------------------------


def test_finish_ai_block_returns_union_of_ok_or_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-035 §3.5 + ADR-040 §3.1: finish_ai_block returns OK | Error union envelope."""
    monkeypatch.delenv("SCISTUDIO_AI_BLOCK_RUN_DIR", raising=False)
    _context.set_context(None)
    result = _run(tools_workflow.finish_ai_block({}))
    assert result.status == "error"
    assert result.code == "not_in_ai_block_context"


# ---------------------------------------------------------------------------
# Codex P1/P2 reconcile regressions (PR #1053).
# ---------------------------------------------------------------------------


def test_preview_dataframe_csv_streams_without_full_load(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """Codex P1 (PR #1053): _preview_dataframe must not load the full CSV.

    Pre-fix: ``pcsv.read_csv(path).slice(0, 100)`` materialised the
    whole file before slicing, defeating the 8 MiB cap on large CSVs.
    Post-fix: ``pcsv.open_csv()`` + ``read_next_batch`` streams enough
    rows to satisfy the preview.

    Regression strategy: build a CSV with many more rows than the
    preview cap (100). The preview must return ≤ 100 rows AND must
    *not* be the full table.
    """
    from scistudio.ai.agent.mcp import tools_inspection
    from scistudio.ai.agent.mcp.tools_inspection import _preview_dataframe

    big_csv = tmp_path / "big.csv"
    rows = 5000  # 50x the preview cap
    with big_csv.open("w", encoding="utf-8") as fh:
        fh.write("col_a,col_b\n")
        for i in range(rows):
            fh.write(f"{i},{i * 2}\n")

    result = _preview_dataframe(big_csv)
    assert result["fmt"] == "table"
    assert len(result["payload"]["rows"]) <= tools_inspection._DATAFRAME_PREVIEW_ROWS, (
        "preview must respect _DATAFRAME_PREVIEW_ROWS cap"
    )
    assert result["truncated"] is True, "5000-row CSV must report truncated=True"


def test_preview_dataframe_parquet_streams_without_full_load(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """Codex P1 (PR #1053): _preview_dataframe must not load the full Parquet."""
    from scistudio.ai.agent.mcp import tools_inspection
    from scistudio.ai.agent.mcp.tools_inspection import _preview_dataframe

    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    big_parquet = tmp_path / "big.parquet"
    rows = 5000
    table = pa.table({"col_a": list(range(rows)), "col_b": [i * 2 for i in range(rows)]})
    pq.write_table(table, big_parquet)

    result = _preview_dataframe(big_parquet)
    assert result["fmt"] == "table"
    assert len(result["payload"]["rows"]) <= tools_inspection._DATAFRAME_PREVIEW_ROWS
    assert result["truncated"] is True


def test_search_docs_top_n_from_full_corpus_not_first_20(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """Codex P2 (PR #1053): search_docs top-N must come from full corpus.

    Pre-fix: the search loop broke at 20 raw traversal hits, then sorted
    only that subset. In repositories with many matching docs, higher-
    scoring files encountered later (alphabetically after the first 20)
    were never considered.

    Regression strategy:
      - Create 25 docs, 20 with score 1 and 5 with score 10.
      - The 5 high-score docs come *after* the first 20 alphabetically
        (so they would be discarded under the broken break-at-20 logic).
      - Assert the returned top-20 contains all 5 high-score docs.
    """
    from scistudio.ai.agent.mcp import _context, tools_qa

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    # 20 low-score docs (a_doc_00 .. a_doc_19) — sorted alphabetically first.
    for i in range(20):
        (docs_dir / f"a_doc_{i:02d}.md").write_text("foo appears once\n", encoding="utf-8")
    # 5 high-score docs (z_doc_0 .. z_doc_4) — alphabetically after the 20.
    for i in range(5):
        (docs_dir / f"z_doc_{i}.md").write_text(("foo " * 10) + "\n", encoding="utf-8")

    # Point the QA context at this docs root.
    runtime = _StubRuntime(_project_dir=tmp_path)
    _context.set_context(runtime)
    try:
        results = _run(tools_qa.search_docs("foo", scope=None))
    finally:
        _context.set_context(stub_ctx)

    assert len(results) <= 20
    # All 5 high-score docs must be present in the top-N.
    result_paths = {r.path for r in results}
    for i in range(5):
        assert any(f"z_doc_{i}.md" in p for p in result_paths), (
            f"z_doc_{i}.md missing from top-20 results — search broke at first 20 traversal hits "
            f"(pre-fix bug, PR #1053). Got: {result_paths}"
        )
    # And the top entries must be the high-score docs (score 10), not score 1.
    top_scores = sorted((r.score for r in results), reverse=True)[:5]
    assert all(s >= 10.0 for s in top_scores), f"top-5 scores expected >=10, got {top_scores}"


# ---------------------------------------------------------------------------
# Issue #1097 regression — production docs tools must not leak dev paths.
# ---------------------------------------------------------------------------


def test_docs_tools_no_dev_leak_when_no_project_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1097 P0: docs lookup must not reach the dev source tree, ever.

    Pre-fix: ``_docs_root()`` unconditionally walked ``__file__.parents``
    for any ``docs/`` directory. In an editable install of SciStudio
    (the dev checkout), that walk landed on
    ``C:\\Users\\<dev>\\workspace\\SciStudio\\docs`` and the embedded agent
    could ``search_docs`` / ``get_doc`` its way into SciStudio development
    ADRs — violating ADR-040 §2.1's dev/prod boundary and leaking
    absolute developer-machine paths.

    Post-fix: with the active project carrying no ``docs/``,
    ``search_docs`` returns ``[]`` and ``get_doc`` raises
    ``FileNotFoundError``. The source-tree parents-walk is gone.
    """
    from scistudio.ai.agent.mcp import _context, tools_qa

    # Defensive: ensure no stale env override is in play.
    monkeypatch.delenv("SCISTUDIO_DEV", raising=False)

    # A project workspace with no docs/ subdirectory (matches the e2e
    # report: fresh project at ``C:\\temp\\scistudio-e2e-adr-040\\...``).
    runtime = _StubRuntime(_project_dir=tmp_path)
    _context.set_context(runtime)
    try:
        results = _run(tools_qa.search_docs("ADR", scope=None))
        assert results == [], f"search_docs must return [] when the project has no docs/. Got: {results!r}"
        with pytest.raises(FileNotFoundError):
            _run(tools_qa.get_doc("adr/ADR-040.md"))
    finally:
        _context.set_context(None)


def test_docs_tools_no_env_var_backdoor_into_source_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1097 P0 hardening: ``SCISTUDIO_DEV=1`` MUST NOT re-open the leak.

    An earlier draft of the #1097 fix gated the parents-walk fallback
    behind ``SCISTUDIO_DEV=1`` (which at the time mirrored a now-removed
    monorepo source-scan dev convention; ``SCISTUDIO_DEV`` no longer gates
    anything in core as of issue #1770). That was rejected because any
    env-var-controlled escape into "dev mode" is a soft attack surface:
    even with the env knob now inert, this regression guard keeps it that
    way. A compromised shell init, a
    malicious launcher script, or a supply-chain dependency could set
    ``SCISTUDIO_DEV=1`` and silently re-disclose developer source paths.

    Regression guard: even with ``SCISTUDIO_DEV=1`` set and the active
    project carrying no ``docs/``, the MCP docs tools must NOT reach
    into the SciStudio source repository.
    """
    from scistudio.ai.agent.mcp import _context, tools_qa

    monkeypatch.setenv("SCISTUDIO_DEV", "1")

    runtime = _StubRuntime(_project_dir=tmp_path)  # no docs/ in the project
    _context.set_context(runtime)
    try:
        results = _run(tools_qa.search_docs("ADR-040", scope=None))
        assert results == [], (
            f"SCISTUDIO_DEV=1 must NOT re-open the parents-walk into the source tree. Got non-empty results: {results!r}"
        )
        with pytest.raises(FileNotFoundError):
            _run(tools_qa.get_doc("adr/ADR-040.md"))
    finally:
        _context.set_context(None)


def test_get_doc_path_is_relative_not_absolute(stub_ctx: _StubRuntime, tmp_path: Path) -> None:
    """#1097: ``GetDocResult.path`` must not leak absolute developer paths.

    Pre-fix: ``path=str(resolved)`` exposed e.g.
    ``C:\\Users\\<dev>\\workspace\\SciStudio\\docs\\adr\\ADR-038.md`` to
    the agent. Post-fix the path is relative to the docs/ tree root.
    """
    from scistudio.ai.agent.mcp import tools_qa

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\n", encoding="utf-8")

    result = _run(tools_qa.get_doc("guide.md"))
    assert result.path == "guide.md", (
        f"get_doc must return a path relative to docs/ root, not an absolute "
        f"developer-machine path. Got: {result.path!r}"
    )
    # Hard guard: the response must not contain any absolute-path marker
    # that could disclose the developer checkout location.
    assert ":\\" not in result.path and not result.path.startswith("/"), (
        f"get_doc.path leaked absolute path: {result.path!r}"
    )


# ---------------------------------------------------------------------------
# JSON-RPC notification handling (ADR-040 Addendum 3 hotfix).
# ---------------------------------------------------------------------------


def test_dispatch_silently_drops_notifications(tmp_path: Path) -> None:
    """JSON-RPC notifications (no ``id``) MUST NOT receive a response.

    Phase 4 e2e on Codex 2026 surfaced this: the pre-fix MCPServer.dispatch
    returned ``{"error":{"code":-32601,"message":"unknown method
    'notifications/initialized'"}}`` to every notification. Claude Code
    tolerated the bogus error envelope, but Codex 2026's stricter MCP
    transport treats an unexpected frame as fatal and closes the
    connection — meaning the Codex provider got ZERO scistudio MCP tools.

    Per JSON-RPC 2.0 § 4.1, notifications (requests without ``id``) MUST
    NOT trigger a response — error or otherwise.
    """
    from scistudio.ai.agent.mcp.server import MCPServer

    server = MCPServer(socket_path=tmp_path / "mcp.sock", project_dir=tmp_path)

    # The MCP-mandatory client→server notification.
    assert _run(server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    # Another common notification (cancellation).
    assert (
        _run(server.dispatch({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "x"}}))
        is None
    )
    # Notification with an unknown method also drops silently (not an error).
    assert _run(server.dispatch({"jsonrpc": "2.0", "method": "totally/unknown"})) is None
    # Notification with missing/invalid method ALSO drops silently — spec
    # says no response to notifications regardless of validity.
    assert _run(server.dispatch({"jsonrpc": "2.0"})) is None

    # Regression guard: an unknown method WITH an id is still an error.
    resp = _run(server.dispatch({"jsonrpc": "2.0", "id": 7, "method": "totally/unknown"}))
    assert resp is not None
    assert resp.get("error", {}).get("code") == -32601


# ---------------------------------------------------------------------------
# ADR-055 Spec 1 (FR-004 / US6): audience-tag per-transport visibility.
# ---------------------------------------------------------------------------


@pytest.fixture
def audience_fixture_tools() -> Iterator[None]:
    """Register an external-tagged and an untagged fixture tool, then clean up.

    The registry must return to its 36-tool baseline afterwards — the parity
    tests above assert the exact count.
    """
    from scistudio.ai.agent.mcp import AUDIENCE_EXTERNAL_TAG

    @mcp.tool(
        name="audience_fixture_external",
        tags={"category:testing", "read", AUDIENCE_EXTERNAL_TAG},
    )
    def _external() -> dict[str, Any]:
        return {"ok": True}

    @mcp.tool(name="audience_fixture_untagged", tags={"category:testing", "read"})
    def _untagged() -> dict[str, Any]:
        return {"ok": True}

    try:
        yield
    finally:
        mcp.local_provider.remove_tool("audience_fixture_external")
        mcp.local_provider.remove_tool("audience_fixture_untagged")


def test_audience_external_tag_is_defined_once() -> None:
    """FR-004: one constant, importable from the package root and server module."""
    import scistudio.ai.agent.mcp as mcp_package
    from scistudio.ai.agent.mcp import server as server_module

    assert mcp_package.AUDIENCE_EXTERNAL_TAG == server_module.AUDIENCE_EXTERNAL_TAG == "audience:external"


def test_socket_tools_list_excludes_external_tagged(audience_fixture_tools: None, tmp_path: Path) -> None:
    """US6 AS1 socket side: external-tagged absent, untagged present."""
    from scistudio.ai.agent.mcp.server import MCPServer

    server = MCPServer(socket_path=tmp_path / "mcp.sock", project_dir=tmp_path)
    response = _run(server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert response is not None
    names = {t["name"] for t in response["result"]["tools"]}
    assert "audience_fixture_external" not in names
    assert "audience_fixture_untagged" in names


def test_webmcp_catalogue_includes_external_tagged(audience_fixture_tools: None) -> None:
    """US6 AS1/AS2 bridge side: external-tagged AND untagged both present."""
    from scistudio.api.routes.webmcp import build_catalogue

    catalogue = _run(build_catalogue(None))
    names = {t["name"] for t in catalogue["tools"]}
    assert "audience_fixture_external" in names
    assert "audience_fixture_untagged" in names
    assert catalogue["context"] == {"projectId": None}
