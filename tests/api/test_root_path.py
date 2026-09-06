"""ADR-055 Spec 0 — mount prefix (``root_path``) behavior.

Covers the spec's verification plan (§4.4):

* prefixed and unprefixed serving of the SPA shell and a representative API
  route (US1),
* the runtime bootstrap injection (``window.__SCISTUDIO_BASE_PATH__``) into
  the served ``index.html`` while hashed assets stay byte-identical (FR-003),
* the WebSocket handshake under the prefix (US1 AS2),
* the unprefixed-root contract while a prefix is configured: 404 for HTTP,
  close-1008 for WebSocket (spec §2 edge cases — the chosen behavior),
* the worker callback URL derivation (FR-006),
* CLI flag/env wiring for ``serve``/``gui`` (FR-007),
* the default empty prefix as a strict no-op (FR-002 / SC-002).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from scistudio.api import app as app_module
from scistudio.api.app import create_app, normalize_root_path
from scistudio.api.routes.workflows import _bind_engine_api_url
from scistudio.cli.main import app as cli_app
from tests.api.helpers import build_linear_workflow, wait_for_workflow_completion

PREFIX = "/user/alice/scistudio"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_prefix_env() -> Any:
    """The CLI commands and _bind_engine_api_url mutate process env directly;
    keep those mutations from leaking across tests."""
    keys = ("SCISTUDIO_ROOT_PATH", "SCISTUDIO_ENGINE_API_URL", "SCISTUDIO_BUNDLED")
    previous = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _make_spa_dir(root: Path) -> Path:
    """Create a minimal SPA tree (index.html + assets/) at ``root``.

    The index.html mirrors a real Vite build (``base: "./"``): asset
    references are document-relative, never root-absolute.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body><div id="root"></div>'
        '<script type="module" src="./assets/main.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "main.js").write_text("console.log('hi')", encoding="utf-8")
    return root


@pytest.fixture()
def prefixed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient for an app configured with a non-empty mount prefix."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    from scistudio.api import runtime as runtime_module

    monkeypatch.setattr(runtime_module.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("SCISTUDIO_ROOT_PATH", PREFIX)
    monkeypatch.setattr(app_module, "_resolve_spa_static_dir", lambda: _make_spa_dir(tmp_path / "static"))

    app = create_app()
    with TestClient(app, root_path=PREFIX) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# FR-008: single normalization point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("   ", ""),
        ("/p", "/p"),
        ("/p/", "/p"),
        ("p", "/p"),
        ("//p//", "/p"),
        ("/user/alice/scistudio/", "/user/alice/scistudio"),
        ("/a//b///c", "/a/b/c"),
    ],
)
def test_normalize_root_path(raw: str | None, expected: str) -> None:
    assert normalize_root_path(raw) == expected


@pytest.mark.parametrize("raw", ["/api", "/api/", "api", "/ws", "/ws/terminal", "/api/v2"])
def test_normalize_root_path_rejects_reserved_namespaces(raw: str) -> None:
    """Codex review (PR #2274): a prefix whose first segment collides with the
    /api or /ws route namespaces makes "already prefixed?" undecidable in the
    frontend helper, so it is rejected at configuration time."""
    with pytest.raises(ValueError, match="reserved"):
        normalize_root_path(raw)


# ---------------------------------------------------------------------------
# FR-002: default empty prefix is a strict no-op
# ---------------------------------------------------------------------------


def test_empty_prefix_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no configured prefix the SPA shell is served byte-identical (no
    injection), API routes answer at the root, and no guard 404s appear."""
    monkeypatch.delenv("SCISTUDIO_ROOT_PATH", raising=False)
    spa = _make_spa_dir(tmp_path / "static")
    monkeypatch.setattr(app_module, "_resolve_spa_static_dir", lambda: spa)
    expected_html = (spa / "index.html").read_text(encoding="utf-8")

    app = create_app()
    assert app.state.root_path == ""
    with TestClient(app) as client:
        shell = client.get("/")
        assert shell.status_code == 200
        assert shell.text == expected_html
        assert "__SCISTUDIO_BASE_PATH__" not in shell.text
        assert "<base" not in shell.text
        assert client.get("/api/version").status_code == 200


# ---------------------------------------------------------------------------
# US1: prefixed serving
# ---------------------------------------------------------------------------


def test_prefixed_api_route_serves_under_prefix(prefixed_client: TestClient) -> None:
    response = prefixed_client.get(f"{PREFIX}/api/version")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_prefixed_spa_shell_carries_injected_base_path(prefixed_client: TestClient) -> None:
    response = prefixed_client.get(f"{PREFIX}/")
    assert response.status_code == 200
    assert f'window.__SCISTUDIO_BASE_PATH__ = "{PREFIX}";' in response.text


def test_prefixed_spa_shell_has_prefix_rooted_base_href(prefixed_client: TestClient) -> None:
    """Codex review (PR #2274): Vite builds with ``base: "./"``, so without a
    prefix-rooted <base> element the shell's relative asset references would
    resolve against the *document* URL. The <base> must precede the first
    relative asset reference."""
    body = prefixed_client.get(f"{PREFIX}/").text
    base_tag = f'<base href="{PREFIX}/">'
    assert base_tag in body
    assert body.index(base_tag) < body.index('src="./assets/')


def test_deep_spa_route_resolves_assets_from_prefix_root(prefixed_client: TestClient) -> None:
    """At a deep route the browser resolves `./assets/main.js` against the
    injected <base>, i.e. to `<prefix>/assets/main.js` — which must serve the
    real file (not the SPA fallback)."""
    body = prefixed_client.get(f"{PREFIX}/projects/some/deep/route").text
    assert f'<base href="{PREFIX}/">' in body
    asset = prefixed_client.get(f"{PREFIX}/assets/main.js")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert asset.text == "console.log('hi')"


def test_prefixed_spa_deep_route_also_carries_base_path(prefixed_client: TestClient) -> None:
    """SPA fallback (client-side route) must inject the bootstrap too."""
    response = prefixed_client.get(f"{PREFIX}/projects/some/deep/route")
    assert response.status_code == 200
    assert f'window.__SCISTUDIO_BASE_PATH__ = "{PREFIX}";' in response.text


def test_hashed_assets_stay_byte_identical_under_prefix(prefixed_client: TestClient) -> None:
    response = prefixed_client.get(f"{PREFIX}/assets/main.js")
    assert response.status_code == 200
    assert response.text == "console.log('hi')"
    # Templating must never touch asset responses.
    assert "__SCISTUDIO_BASE_PATH__" not in response.text


def test_prefixed_shell_has_no_unprefixed_root_relative_references(prefixed_client: TestClient) -> None:
    """FR-009: the served SPA shell must not contain an unprefixed absolute
    path the proxy did not rewrite (root-absolute src/href, "/api/, "/ws).
    The injected <base href> IS root-absolute but prefix-rooted — that is the
    mechanism, not a violation."""
    body = prefixed_client.get(f"{PREFIX}/").text
    assert 'src="/' not in body
    assert '"/api/' not in body
    assert "'/api/" not in body
    assert '"/ws' not in body
    for href in re.findall(r'href="([^"]*)"', body):
        assert not href.startswith("/") or href.startswith(f"{PREFIX}/"), href


def test_bare_prefix_redirects_to_trailing_slash_with_prefix(prefixed_client: TestClient) -> None:
    """Redirect Location headers must be prefix-aware (spec §4.5 risk)."""
    response = prefixed_client.get(PREFIX, follow_redirects=False)
    assert response.status_code in (307, 308)
    location = response.headers["location"]
    assert location.endswith(f"{PREFIX}/")
    assert not location.endswith("://") and "//" not in location.replace("://", "")


# ---------------------------------------------------------------------------
# Edge case: requests at the unprefixed root while a prefix is configured
# ---------------------------------------------------------------------------


def test_unprefixed_root_is_404_while_prefix_configured(prefixed_client: TestClient) -> None:
    assert prefixed_client.get("/").status_code == 404
    assert prefixed_client.get("/api/version").status_code == 404
    assert prefixed_client.get("/assets/main.js").status_code == 404


def test_unprefixed_websocket_is_rejected_while_prefix_configured(prefixed_client: TestClient) -> None:
    """The guard must deny the upgrade with close code 1008 (policy
    violation), not merely fail the handshake with some error."""
    with pytest.raises(WebSocketDisconnect) as exc_info, prefixed_client.websocket_connect("/ws"):
        pass
    assert exc_info.value.code == 1008


def test_prefixed_websocket_handshake_succeeds(prefixed_client: TestClient) -> None:
    """US1 AS2: the WS handshake succeeds under the prefix and the socket is
    usable (the endpoint subscribes the bus on accept)."""
    with prefixed_client.websocket_connect(f"{PREFIX}/ws") as websocket:
        websocket.send_text('{"type": "ping"}')


# ---------------------------------------------------------------------------
# Spec edge cases: doubled separators / trailing-slash normalization
# ---------------------------------------------------------------------------


def test_doubled_separator_after_prefix_is_normalized(prefixed_client: TestClient) -> None:
    """Codex review (PR #2274): ``/p//api/version`` must behave exactly like
    ``/p/api/version`` — the guard collapses doubled separators before
    routing, so this is 200, not a spurious 404."""
    assert prefixed_client.get(f"{PREFIX}//api/version").status_code == 200


def test_doubled_separator_on_deep_spa_route_is_normalized(prefixed_client: TestClient) -> None:
    response = prefixed_client.get(f"{PREFIX}//projects/foo")
    assert response.status_code == 200
    assert f'window.__SCISTUDIO_BASE_PATH__ = "{PREFIX}";' in response.text


def test_prefix_with_trailing_slash_serves_shell(prefixed_client: TestClient) -> None:
    assert prefixed_client.get(f"{PREFIX}/").status_code == 200


# ---------------------------------------------------------------------------
# FR-006: worker callback URL derivation
# ---------------------------------------------------------------------------


def _mock_request(base_url: str, root_path: str) -> MagicMock:
    request = MagicMock()
    request.base_url = base_url
    request.app.state.root_path = root_path
    return request


def test_engine_api_url_uses_configured_base_plus_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCISTUDIO_ENGINE_API_URL", "http://127.0.0.1:8000")
    _bind_engine_api_url(_mock_request("http://external.proxy/", "/p"))
    assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://127.0.0.1:8000/p"


def test_engine_api_url_prefix_applied_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCISTUDIO_ENGINE_API_URL", "http://127.0.0.1:8000")
    request = _mock_request("http://127.0.0.1:8000/", "/p")
    _bind_engine_api_url(request)
    _bind_engine_api_url(request)  # repeated execute calls must not double the prefix
    assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://127.0.0.1:8000/p"


def test_engine_api_url_fallback_uses_request_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented fallback for the unprefixed local case: nothing configured
    the external base, so the incoming request's base URL is used."""
    monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
    _bind_engine_api_url(_mock_request("http://testserver/", ""))
    assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://testserver"


def test_engine_api_url_fallback_appends_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
    _bind_engine_api_url(_mock_request("http://testserver/", "/p"))
    assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://testserver/p"


def test_engine_api_url_empty_prefix_preserves_current_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-002: with no prefix the derivation is exactly the pre-change value."""
    monkeypatch.setenv("SCISTUDIO_ENGINE_API_URL", "http://127.0.0.1:8000")
    _bind_engine_api_url(_mock_request("http://testserver/", ""))
    assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://127.0.0.1:8000"


def test_workflow_execute_publishes_prefixed_callback_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SC-003: a run launched through the prefixed API leaves a callback URL
    ending with the configured prefix in the environment workers inherit."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    from scistudio.api import runtime as runtime_module

    monkeypatch.setattr(runtime_module.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("SCISTUDIO_ROOT_PATH", PREFIX)
    monkeypatch.setenv("SCISTUDIO_ENGINE_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(app_module, "_resolve_spa_static_dir", lambda: _make_spa_dir(tmp_path / "static"))

    app = create_app()
    with TestClient(app, root_path=PREFIX) as client:
        project_parent = tmp_path / "projects"
        project_parent.mkdir()
        created = client.post(
            f"{PREFIX}/api/projects/",
            json={"name": "Prefix Project", "description": "p", "path": str(project_parent)},
        )
        assert created.status_code == 200
        project_path = Path(created.json()["path"])
        payload = build_linear_workflow(project_path, workflow_id="prefix-flow")
        assert client.post(f"{PREFIX}/api/workflows/", json=payload).status_code == 200
        started = client.post(f"{PREFIX}/api/workflows/prefix-flow/execute")
        assert started.status_code == 200
        runtime = client.app.state.runtime
        wait_for_workflow_completion(runtime, "prefix-flow")
        assert os.environ["SCISTUDIO_ENGINE_API_URL"] == f"http://127.0.0.1:8000{PREFIX}"


# ---------------------------------------------------------------------------
# FR-007: CLI flags and environment variables
# ---------------------------------------------------------------------------


def _fake_uvicorn_run(calls: dict[str, Any]):
    def fake_run(app_target: str, **kwargs: object) -> None:
        calls["app_target"] = app_target
        calls.update(kwargs)

    return fake_run


def _cli_command_options(command_name: str) -> dict[str, Any]:
    """Resolve a registered CLI command's options by introspection.

    Deliberately NOT parsed from rendered ``--help`` output: Rich's options
    panel truncates/wraps option names depending on console width, renderer
    version, and platform (the #2274 CI failure — even a pinned COLUMNS did
    not make the rendered literal reliable). The click parameter list is the
    actual command definition, so it is environment-proof by construction.
    """
    group = typer.main.get_command(cli_app)
    command = group.commands[command_name]
    return {param.name: param for param in command.params}


def _assert_host_and_root_path_options(command_name: str) -> None:
    options = _cli_command_options(command_name)
    assert options["host"].opts == ["--host"]
    assert options["host"].envvar == "SCISTUDIO_HOST"
    assert options["root_path"].opts == ["--root-path"]
    assert options["root_path"].envvar == "SCISTUDIO_ROOT_PATH"


class TestServeRootPath:
    def test_serve_help_exits_cleanly(self) -> None:
        """Smoke only: rendered help is Rich-formatted and width-dependent —
        never assert on its text (see _cli_command_options)."""
        result = runner.invoke(cli_app, ["serve", "--help"])
        assert result.exit_code == 0

    def test_serve_exposes_host_and_root_path_options(self) -> None:
        _assert_host_and_root_path_options("serve")

    def test_serve_default_invocation_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FR-002: no new kwargs reach uvicorn and no prefix env is required
        when the default root mount is used."""
        monkeypatch.delenv("SCISTUDIO_ROOT_PATH", raising=False)
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        result = runner.invoke(cli_app, ["serve"])
        assert result.exit_code == 0
        assert "root_path" not in calls
        assert calls["host"] == "0.0.0.0"
        assert os.environ["SCISTUDIO_ROOT_PATH"] == ""

    def test_serve_root_path_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        # Other suites can leave a stale value behind; setdefault must see a
        # clean slate for the derivation assertion to be meaningful.
        monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
        result = runner.invoke(cli_app, ["serve", "--root-path", "/p/"])
        assert result.exit_code == 0
        # The prefix reaches the app via the environment, NOT via uvicorn's
        # root_path kwarg (uvicorn prepends it to incoming paths, which would
        # double-prefix under a verbatim proxy).
        assert "root_path" not in calls
        assert os.environ["SCISTUDIO_ROOT_PATH"] == "/p"  # normalized: no trailing slash
        assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://127.0.0.1:8000/p"
        assert "0.0.0.0:8000/p" in result.output

    def test_serve_root_path_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        monkeypatch.setenv("SCISTUDIO_ROOT_PATH", "/env-prefix")
        result = runner.invoke(cli_app, ["serve"])
        assert result.exit_code == 0
        assert "env-prefix" in result.output

    def test_serve_host_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        monkeypatch.setenv("SCISTUDIO_HOST", "127.0.0.1")
        result = runner.invoke(cli_app, ["serve"])
        assert result.exit_code == 0
        assert calls["host"] == "127.0.0.1"

    def test_serve_reserved_prefix_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codex review (PR #2274): /api and /ws collide with the route
        namespaces and must be refused with a clear error, not served."""
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run({}))
        result = runner.invoke(cli_app, ["serve", "--root-path", "/api"])
        assert result.exit_code == 2
        assert "reserved" in result.output

    @pytest.mark.parametrize(
        ("bind_host", "expected_callback_host"),
        [
            ("0.0.0.0", "127.0.0.1"),  # wildcard: reachable via loopback
            ("::", "127.0.0.1"),  # v6 wildcard: same
            ("127.0.0.1", "127.0.0.1"),  # explicit loopback
            ("192.168.1.5", "192.168.1.5"),  # specific bind: loopback would be dead
        ],
    )
    def test_serve_worker_callback_host_follows_bind(
        self, monkeypatch: pytest.MonkeyPatch, bind_host: str, expected_callback_host: str
    ) -> None:
        """Codex review (PR #2274): workers must be able to reach the API;
        when uvicorn binds a specific interface, 127.0.0.1 is not one."""
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run({}))
        monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
        result = runner.invoke(cli_app, ["serve", "--host", bind_host])
        assert result.exit_code == 0
        assert os.environ["SCISTUDIO_ENGINE_API_URL"] == f"http://{expected_callback_host}:8000"


class TestGuiRootPath:
    def test_gui_help_exits_cleanly(self) -> None:
        """Smoke only — see TestServeRootPath."""
        result = runner.invoke(cli_app, ["gui", "--help"])
        assert result.exit_code == 0

    def test_gui_exposes_host_and_root_path_options(self) -> None:
        _assert_host_and_root_path_options("gui")

    def test_gui_default_invocation_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCISTUDIO_ROOT_PATH", raising=False)
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        result = runner.invoke(cli_app, ["gui", "--no-browser"])
        assert result.exit_code == 0
        assert "root_path" not in calls
        assert calls["host"] == "0.0.0.0"
        assert "Starting SciStudio GUI on http://localhost:8000 ..." in result.output

    def test_gui_root_path_flag_flows_to_url_env_and_uvicorn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        # See test_serve_root_path_flag: clear any stale value from other
        # suites so the setdefault-derived URL is the one asserted.
        monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
        result = runner.invoke(cli_app, ["gui", "--no-browser", "--root-path", "/p"])
        assert result.exit_code == 0
        # The prefix reaches the app via the environment, NOT via uvicorn's
        # root_path kwarg (verbatim-proxy contract; see serve tests).
        assert "root_path" not in calls
        assert "http://localhost:8000/p" in result.output
        assert os.environ["SCISTUDIO_ROOT_PATH"] == "/p"
        assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://127.0.0.1:8000/p"

    def test_gui_bundled_ready_json_carries_prefixed_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: dict[str, Any] = {}
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run(calls))
        result = runner.invoke(cli_app, ["gui", "--bundled", "--port", "0", "--root-path", "/p"])
        assert result.exit_code == 0
        assert '"url":"http://127.0.0.1:' in result.output
        assert '/p"' in result.output

    def test_gui_reserved_prefix_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run({}))
        result = runner.invoke(cli_app, ["gui", "--no-browser", "--root-path", "/ws"])
        assert result.exit_code == 2
        assert "reserved" in result.output

    def test_gui_worker_callback_host_follows_bind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codex review (PR #2274): same callback-host rule as serve."""
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run({}))
        monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
        result = runner.invoke(cli_app, ["gui", "--no-browser", "--host", "10.0.0.2"])
        assert result.exit_code == 0
        assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://10.0.0.2:8000"

    def test_gui_wildcard_bind_keeps_loopback_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run({}))
        monkeypatch.delenv("SCISTUDIO_ENGINE_API_URL", raising=False)
        result = runner.invoke(cli_app, ["gui", "--no-browser"])
        assert result.exit_code == 0
        assert os.environ["SCISTUDIO_ENGINE_API_URL"] == "http://127.0.0.1:8000"
