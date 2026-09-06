"""ADR-055 Spec 1 — WebMCP bridge tests (spec §4.4 verification plan).

Covers:

* session substrate (FR-006 / US5): bridge endpoints reject calls without
  the loopback session token, accept the injected token, leave non-bridge
  routes untouched, and keep CORS preflight handling intact;
* catalogue parity (FR-001 / SC-001): ``GET /api/webmcp/tools`` matches
  ``mcp.list_tools()`` and carries the active-project context snapshot;
* dispatch + adapter contract (FR-002/FR-003 / US1/US2): unknown-tool 404,
  structured content preserved, non-text blocks substituted explicitly,
  top-level error flag propagation, thrown exceptions mapped to ``isError``
  content rather than HTTP 5xx;
* project binding (FR-005 / US4): stale mutation calls rejected, re-fetch
  and retry succeeds, read calls follow the declared read policy;
* bounded logging (FR-007 / SC-005): logs carry tool name and outcome but
  never full argument bodies.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scistudio.ai.agent.mcp.server import adapt_tool_result, mcp
from scistudio.api.runtime import ApiRuntime


def _token_headers(client: TestClient) -> dict[str, str]:
    return {"X-SciStudio-WebMCP-Token": client.app.state.webmcp_session_token}


@pytest.fixture()
def fixture_tools() -> Iterator[dict[str, list[str]]]:
    """Register write/read fixture tools on the shared registry, then remove them.

    The global registry must return to its baseline afterwards — the parity
    tests in ``tests/ai/test_mcp_fastmcp.py`` assert the exact tool count.
    """
    calls: dict[str, list[str]] = {"write": [], "read": []}

    @mcp.tool(name="webmcp_fixture_write", tags={"category:testing", "write"})
    def _fixture_write(marker: str = "") -> dict[str, Any]:
        calls["write"].append(marker)
        return {"ok": True, "marker": marker}

    @mcp.tool(name="webmcp_fixture_read", tags={"category:testing", "read"})
    def _fixture_read() -> dict[str, Any]:
        calls["read"].append("called")
        return {"ok": True}

    @mcp.tool(name="webmcp_fixture_raise", tags={"category:testing", "read"})
    def _fixture_raise() -> dict[str, Any]:
        raise RuntimeError("SECRET-DETAIL-9f2b /abs/internal/path must not cross the wire")

    try:
        yield calls
    finally:
        mcp.local_provider.remove_tool("webmcp_fixture_write")
        mcp.local_provider.remove_tool("webmcp_fixture_read")
        mcp.local_provider.remove_tool("webmcp_fixture_raise")


# ---------------------------------------------------------------------------
# FR-006 / US5: session substrate.
# ---------------------------------------------------------------------------


def test_bridge_requires_session_token(client: TestClient) -> None:
    """No token and a wrong token are both authentication rejections, not dispatches."""
    assert client.get("/api/webmcp/tools").status_code == 401
    assert client.get("/api/webmcp/tools", headers={"X-SciStudio-WebMCP-Token": "wrong"}).status_code == 401
    assert client.post("/api/webmcp/call", json={"name": "list_types", "arguments": {}}).status_code == 401


def test_bridge_accepts_injected_session_token(client: TestClient) -> None:
    response = client.get("/api/webmcp/tools", headers=_token_headers(client))
    assert response.status_code == 200


def test_session_middleware_leaves_other_routes_untouched(client: TestClient) -> None:
    assert client.get("/api/version").status_code == 200


def test_cors_preflight_still_handled_by_cors_layer(client: TestClient) -> None:
    """The session middleware sits inside the CORS layer: preflight never reaches it."""
    response = client.options(
        "/api/webmcp/call",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_served_page_bootstrap_carries_session_token(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US5 AS2: the served index.html carries the token the bridge accepts."""
    from scistudio.api import app as app_module

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "_resolve_spa_static_dir", lambda: static_dir)
    app = app_module.create_app()
    with TestClient(app) as spa_client:
        shell = spa_client.get("/")
        assert shell.status_code == 200
        token = app.state.webmcp_session_token
        assert f"window.__SCISTUDIO_WEBMCP_TOKEN__ = {json.dumps(token)};" in shell.text
        assert shell.headers["cache-control"] == "no-cache"
        # The injected token actually authenticates bridge calls (US5 AS2).
        response = spa_client.get("/api/webmcp/tools", headers={"X-SciStudio-WebMCP-Token": token})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# FR-001 / SC-001: catalogue parity and context snapshot.
# ---------------------------------------------------------------------------


def test_catalogue_matches_registry(client: TestClient, runtime: ApiRuntime, opened_project: Path) -> None:
    import asyncio

    registry_tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    response = client.get("/api/webmcp/tools", headers=_token_headers(client))
    assert response.status_code == 200
    body = response.json()

    catalogue = {entry["name"]: entry for entry in body["tools"]}
    assert set(catalogue) == set(registry_tools)
    for name, entry in catalogue.items():
        assert entry["inputSchema"] == registry_tools[name].parameters
        assert entry["description"] == (registry_tools[name].description or "")
        assert entry["category"]
        assert entry["mutation"] in ("read", "write")

    # Context snapshot identifies the active project (FR-001/FR-005).
    assert runtime.active_project is not None
    assert body["context"] == {"projectId": runtime.active_project.id}


def test_catalogue_context_snapshot_without_project(client: TestClient) -> None:
    body = client.get("/api/webmcp/tools", headers=_token_headers(client)).json()
    assert body["context"] == {"projectId": None}


# ---------------------------------------------------------------------------
# FR-002: dispatch.
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_404(client: TestClient, opened_project: Path) -> None:
    response = client.post(
        "/api/webmcp/call",
        headers=_token_headers(client),
        json={"name": "no_such_tool", "arguments": {}},
    )
    assert response.status_code == 404
    assert "unknown tool" in response.json()["detail"]


def test_read_tool_dispatches_through_bridge(
    client: TestClient, opened_project: Path, fixture_tools: dict[str, list[str]]
) -> None:
    snapshot = client.get("/api/webmcp/tools", headers=_token_headers(client)).json()
    response = client.post(
        "/api/webmcp/call",
        headers=_token_headers(client),
        json={
            "name": "webmcp_fixture_read",
            "arguments": {},
            "projectId": snapshot["context"]["projectId"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["isError"] is False
    assert fixture_tools["read"] == ["called"]


# ---------------------------------------------------------------------------
# FR-003 / US2: the adapter contract.
# ---------------------------------------------------------------------------


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ImageBlock:
    type = "image"

    def __init__(self) -> None:
        self.data = "aGVsbG8="
        self.mimeType = "image/png"


class _FakeResult:
    def __init__(
        self,
        *,
        content: list[object] | None = None,
        structured: dict[str, Any] | None = None,
        is_error: bool = False,
    ) -> None:
        self.content = content
        self.structured_content = structured
        self.isError = is_error


def test_adapter_preserves_structured_content() -> None:
    """SC-002: structured content survives in its declared field, no text round-trip."""
    structured = {"points": [{"x": 1, "y": [2, 3]}], "unit": "nm"}
    result = _FakeResult(content=[_TextBlock("summary")], structured=structured)
    adapted = adapt_tool_result(result)
    assert adapted["structuredContent"] == structured
    assert adapted["content"] == [{"type": "text", "text": "summary"}]
    assert adapted["isError"] is False


def test_adapter_substitutes_non_text_blocks_with_marker() -> None:
    result = _FakeResult(content=[_TextBlock("before"), _ImageBlock()])
    adapted = adapt_tool_result(result)
    assert adapted["content"][0] == {"type": "text", "text": "before"}
    substituted = adapted["content"][1]
    assert substituted["type"] == "text"
    assert substituted["substitutedFrom"] == "image"
    assert "image" in substituted["text"]


def test_adapter_propagates_top_level_error_flag() -> None:
    result = _FakeResult(content=[_TextBlock("boom")], is_error=True)
    adapted = adapt_tool_result(result)
    assert adapted["isError"] is True
    assert adapted["content"] == [{"type": "text", "text": "boom"}]


def test_adapter_wraps_primitive_result() -> None:
    adapted = adapt_tool_result({"plain": "dict"})
    assert adapted["content"] == [{"type": "text", "text": json.dumps({"plain": "dict"})}]
    assert adapted["isError"] is False


def test_thrown_exception_maps_to_iserror_not_5xx(client: TestClient, opened_project: Path) -> None:
    """A tool call that fails argument validation surfaces as isError content."""
    response = client.post(
        "/api/webmcp/call",
        headers=_token_headers(client),
        # get_block_schema requires block_name; omitting it makes FastMCP raise.
        json={"name": "get_block_schema", "arguments": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["isError"] is True
    assert body["content"][0]["type"] == "text"


def test_exception_mapping_is_bounded_no_internals_exposed(
    client: TestClient, opened_project: Path, fixture_tools: dict[str, list[str]]
) -> None:
    """CodeQL py/stack-trace-exposure (PR #2275 review): the exception mapping
    carries the exception TYPE name and a generic message only — never the
    exception message, which can embed argument values, paths, or internals."""
    response = client.post(
        "/api/webmcp/call",
        headers=_token_headers(client),
        json={"name": "webmcp_fixture_raise", "arguments": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["isError"] is True
    assert len(body["content"]) == 1
    text = body["content"][0]["text"]
    # Bounded type name only: FastMCP wraps tool exceptions in ToolError.
    assert "ToolError" in text or "RuntimeError" in text
    assert "SECRET-DETAIL-9f2b" not in text
    assert "/abs/internal/path" not in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# FR-005 / US4: project binding.
# ---------------------------------------------------------------------------


def _create_project(client: TestClient, parent: Path, name: str) -> dict[str, Any]:
    response = client.post(
        "/api/projects/",
        json={"name": name, "description": "webmcp test", "path": str(parent)},
    )
    assert response.status_code == 200
    return response.json()


def test_stale_project_mutation_rejected_then_refetch_succeeds(
    client: TestClient,
    runtime: ApiRuntime,
    project_parent: Path,
    opened_project: Path,
    fixture_tools: dict[str, list[str]],
) -> None:
    headers = _token_headers(client)
    project_a = runtime.active_project
    assert project_a is not None

    snapshot_a = client.get("/api/webmcp/tools", headers=headers).json()
    assert snapshot_a["context"]["projectId"] == project_a.id

    # Baseline: a mutation call matching the snapshot dispatches.
    ok = client.post(
        "/api/webmcp/call",
        headers=headers,
        json={"name": "webmcp_fixture_write", "arguments": {"marker": "a"}, "projectId": project_a.id},
    )
    assert ok.status_code == 200
    assert fixture_tools["write"] == ["a"]

    # A second page opens project B — the backend's active project changes.
    _create_project(client, project_parent, "Project B")
    assert runtime.active_project is not None
    assert runtime.active_project.id != project_a.id

    # The in-flight write with A's snapshot is rejected and does NOT execute.
    stale = client.post(
        "/api/webmcp/call",
        headers=headers,
        json={"name": "webmcp_fixture_write", "arguments": {"marker": "stale"}, "projectId": project_a.id},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "stale_project_context"
    assert detail["presentedProjectId"] == project_a.id
    assert detail["activeProjectId"] == runtime.active_project.id
    assert fixture_tools["write"] == ["a"], "stale mutation must not execute"

    # Re-fetch the catalogue and retry: the call dispatches against B.
    snapshot_b = client.get("/api/webmcp/tools", headers=headers).json()
    retry = client.post(
        "/api/webmcp/call",
        headers=headers,
        json={
            "name": "webmcp_fixture_write",
            "arguments": {"marker": "b"},
            "projectId": snapshot_b["context"]["projectId"],
        },
    )
    assert retry.status_code == 200
    assert fixture_tools["write"] == ["a", "b"]


def test_read_calls_follow_declared_policy(
    client: TestClient,
    runtime: ApiRuntime,
    project_parent: Path,
    opened_project: Path,
    fixture_tools: dict[str, list[str]],
) -> None:
    """Read-tagged calls are dispatched without the staleness check (FR-005 read policy)."""
    headers = _token_headers(client)
    project_a = runtime.active_project
    assert project_a is not None
    _create_project(client, project_parent, "Project B")

    response = client.post(
        "/api/webmcp/call",
        headers=headers,
        json={"name": "webmcp_fixture_read", "arguments": {}, "projectId": project_a.id},
    )
    assert response.status_code == 200
    assert fixture_tools["read"] == ["called"]


# ---------------------------------------------------------------------------
# FR-007 / SC-005: bounded logging.
# ---------------------------------------------------------------------------


def test_logs_never_contain_argument_bodies(
    client: TestClient,
    opened_project: Path,
    fixture_tools: dict[str, list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SECRET-FILE-BODY-7f3a9c-lorem-ipsum-dolor"
    with caplog.at_level("INFO", logger="scistudio.api.routes.webmcp"):
        response = client.post(
            "/api/webmcp/call",
            headers=_token_headers(client),
            json={"name": "webmcp_fixture_write", "arguments": {"marker": secret}, "projectId": None},
        )
        # projectId None vs active project A -> stale rejection, also logged.
        assert response.status_code == 409
        ok = client.post(
            "/api/webmcp/call",
            headers=_token_headers(client),
            json={
                "name": "webmcp_fixture_write",
                "arguments": {"marker": secret},
                "projectId": client.app.state.runtime.active_project.id,
            },
        )
        assert ok.status_code == 200

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "webmcp_fixture_write" in rendered, "tool name must be logged"
    assert secret not in rendered, "argument bodies must never be logged"
