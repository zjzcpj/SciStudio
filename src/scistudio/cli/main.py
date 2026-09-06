"""CLI entry point -- scistudio init, validate, run, blocks, serve."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import typer
import yaml

app = typer.Typer(name="scistudio", help="SciStudio -- AI-native scientific workflow runtime")

# Register subcommands provided by sibling modules.
# These were dropped during the ADR-033 rollback (PR #808) but the
# underlying modules (`install`, `mcp_bridge`) survived intact.
# See PR #794 for original integration context.
from scistudio.cli import install as _install_cli  # noqa: E402
from scistudio.cli import mcp_bridge as _mcp_bridge_cli  # noqa: E402
from scistudio.cli import storage as _storage_cli  # noqa: E402

_install_cli.register(app)
_mcp_bridge_cli.register(app)
_storage_cli.register(app)


def _version_callback(value: bool) -> None:
    # #1742: ``scistudio --version`` prints the human display string.
    if value:
        from scistudio.version import get_version

        typer.echo(get_version().display)
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """SciStudio -- AI-native scientific workflow runtime."""


# ---------------------------------------------------------------------------
# Shared helpers for validate / run commands
# ---------------------------------------------------------------------------


def _check_file_exists(workflow: str) -> Path:
    """Return a resolved Path, or exit with code 1 if the file does not exist."""
    path = Path(workflow)
    if not path.exists():
        typer.echo(f"Error: file not found: {workflow}", err=True)
        raise typer.Exit(code=1)
    return path


def _load_workflow(path: Path) -> Any:
    """Load a workflow definition via the YAML serializer, or exit on error."""
    try:
        from scistudio.workflow.serializer import load_yaml

        return load_yaml(path)
    except NotImplementedError:
        typer.echo("Error: YAML serializer not yet implemented.", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"Error loading workflow: {exc}", err=True)
        raise typer.Exit(code=1) from None


def _validate_workflow(
    definition: Any,
    *,
    exit_on_stub: bool = True,
    registry: Any = None,
) -> list[str]:
    """Run workflow validation, returning a list of errors.

    When *exit_on_stub* is ``True`` (the ``validate`` command), a stub
    validator causes an immediate exit.  When ``False`` (the ``run``
    command), a stub validator is silently skipped so execution can proceed.

    When *registry* is provided, the validator can perform type-compatibility
    and dangling-port checks (Checks 5-6).
    """
    try:
        from scistudio.workflow.validator import validate_workflow

        return validate_workflow(definition, registry=registry)
    except NotImplementedError:
        if exit_on_stub:
            typer.echo("Error: workflow validator not yet implemented.", err=True)
            raise typer.Exit(code=1) from None
        return []


def _report_validation_errors(diagnostics: list[str]) -> None:
    """Print validation diagnostics and exit only when one is a hard error.

    ``validate_workflow`` returns a single list in which a leading ``Warning:``
    marks an advisory diagnostic. The API layer has always split on that prefix
    (``api/runtime/_workflows.py``), but this CLI treated the list as
    all-or-nothing, so an advisory made ``scistudio run`` refuse to dispatch.

    That was latent until #1988: the validator's unregistered-block-type report
    used to reach only nodes that had edges, and a node whose block does not
    resolve has no ports and therefore no edges — so the warning that now fires
    for those nodes had no way to fire before. Widening the report exposed the
    prefix being ignored here. Warnings are printed either way; only hard errors
    stop the command.
    """
    warnings = [d for d in diagnostics if d.startswith("Warning:")]
    errors = [d for d in diagnostics if not d.startswith("Warning:")]
    if warnings:
        typer.echo("Validation warnings:")
        for warning in warnings:
            typer.echo(f"  - {warning}")
    if errors:
        typer.echo("Validation errors:")
        for err in errors:
            typer.echo(f"  - {err}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def init(name: str = typer.Argument("my_project", help="Project workspace name")) -> None:
    """Create a new project workspace."""
    project_path = Path(name)
    if project_path.exists():
        typer.echo(f"Error: directory '{name}' already exists.", err=True)
        raise typer.Exit(code=1)

    # Create directory structure per ARCHITECTURE.md Section 10.
    # #2095: the list is shared with ``ApiRuntime.create_project`` rather than
    # restated here. The two had drifted -- this one was missing
    # ``data/processed`` while claiming to be symmetric with it, and neither
    # created the previewer or tutorial drop-in directories.
    from scistudio.api.project_layout import PROJECT_SUBDIRS

    for subdir in PROJECT_SUBDIRS:
        (project_path / subdir).mkdir(parents=True, exist_ok=True)

    project_meta = {
        "project": {
            "name": name,
            "version": "0.1.0",
            "created": date.today().isoformat(),
        }
    }
    (project_path / "project.yaml").write_text(yaml.safe_dump(project_meta, default_flow_style=False, sort_keys=False))

    # --------------------------------------------------------------
    # ADR-039 §6 Phase 1 — CLI git-init parity (D39-2.2a skeleton)
    # --------------------------------------------------------------
    # Projects created via ``scistudio init`` must get the same auto-init
    # treatment as those created via the GUI (``ApiRuntime.create_project``).
    # Otherwise CLI-created projects open in the GUI without git history,
    # confusing the ADR-038 lineage join.
    #
    # IMPLEMENTATION FOR D39-2.2b:
    # ----------------------------
    # 1. Lazy import (so ``scistudio init`` doesn't pay the import cost
    #    of the versioning package until needed):
    #        from scistudio.core.versioning.git_engine import GitEngine
    # 2. ``engine = GitEngine(project_path)``
    # 3. Best-effort:
    #        try:
    #            engine.init_repository(project_path)
    #            typer.echo("Initialized git repository.")
    #        except BundledGitMissing:
    #            typer.echo("WARNING: git binary unavailable; project "
    #                       "created without version control.", err=True)
    #        except GitError as exc:
    #            typer.echo(f"WARNING: git init failed: {exc}", err=True)
    #
    # The CLI must NOT abort on git failure — degraded-mode projects
    # (no .git/) are explicitly supported per ADR-039 §3.9.
    try:
        from scistudio.core.versioning.git_binary import BundledGitMissing
        from scistudio.core.versioning.git_engine import GitEngine, GitError

        try:
            engine = GitEngine(project_path)
            engine.init_repository(project_path)
            typer.echo("Initialized git repository.")
        except BundledGitMissing as exc:
            typer.echo(
                f"WARNING: git binary unavailable ({exc}); project created without version control.",
                err=True,
            )
        except GitError as exc:
            typer.echo(f"WARNING: git init failed: {exc}", err=True)
        except FileExistsError:
            # .git already present — already a repo, that's fine.
            pass
    except Exception as exc:  # pragma: no cover — defensive
        typer.echo(f"WARNING: git auto-init errored: {exc}", err=True)

    # ------------------------------------------------------------------
    # ADR-040 §3.8 prod-env agent provisioning wiring (cli init).
    # ------------------------------------------------------------------
    # Runs AFTER git init so the initial commit is clean of provisioned
    # files. Failures are non-fatal per ADR §7 and surfaced via
    # ``typer.echo`` on stderr, mirroring the ADR-039 degraded-mode
    # pattern above.
    try:
        from scistudio.agent_provisioning import install_project_agent_assets

        provision_result = install_project_agent_assets(project_path, force=False)
        if provision_result.failed:
            typer.echo(
                f"WARNING: ADR-040 agent provisioning partial failure: {provision_result.failed}",
                err=True,
            )
    except Exception as exc:  # pragma: no cover — defensive
        typer.echo(
            f"WARNING: ADR-040 agent provisioning failed: {exc}",
            err=True,
        )

    typer.echo(f"Created project workspace: {name}/")


@app.command()
def validate(workflow: str = typer.Argument(..., help="Path to workflow YAML")) -> None:
    """Validate a workflow YAML file."""
    path = _check_file_exists(workflow)
    definition = _load_workflow(path)
    from scistudio.blocks.registry import BlockRegistry

    registry = BlockRegistry()
    try:
        registry.scan()
    except Exception as exc:
        typer.echo(f"Warning: registry scan encountered errors: {exc}", err=True)

    errors = _validate_workflow(definition, exit_on_stub=True, registry=registry)
    _report_validation_errors(errors)
    typer.echo("Valid.")


@app.command()
def run(workflow: str = typer.Argument(..., help="Path to workflow YAML")) -> None:
    """Run a workflow headless."""
    import os

    from scistudio.utils.logging import configure_logging

    # #1741: headless runs previously configured no logging, so engine/block
    # errors were silent. Configure console + file logging up front.
    configure_logging(os.environ.get("SCISTUDIO_LOG_LEVEL", "INFO").upper())
    path = _check_file_exists(workflow)
    definition = _load_workflow(path)
    from scistudio.blocks.registry import BlockRegistry

    registry = BlockRegistry()
    try:
        registry.scan()
    except Exception as exc:
        typer.echo(f"Warning: registry scan encountered errors: {exc}", err=True)

    errors = _validate_workflow(definition, exit_on_stub=False, registry=registry)
    _report_validation_errors(errors)

    # Build DAG and show execution order.
    try:
        from scistudio.engine.dag import build_dag, topological_sort

        dag = build_dag(definition)
        order = topological_sort(dag)
        typer.echo(f"Execution order: {' -> '.join(order)}")
    except Exception as exc:
        typer.echo(f"Error building DAG: {exc}", err=True)
        raise typer.Exit(code=1) from None

    # Execute workflow via DAGScheduler.
    try:
        import asyncio

        from scistudio.engine.events import EventBus
        from scistudio.engine.resources import ResourceManager
        from scistudio.engine.runners.local import LocalRunner
        from scistudio.engine.scheduler import DAGScheduler

        event_bus = EventBus()
        resource_mgr = ResourceManager()
        runner = LocalRunner(event_bus=event_bus)
        scheduler = DAGScheduler(
            workflow=definition,
            event_bus=event_bus,
            resource_manager=resource_mgr,
            process_registry=None,
            runner=runner,
            registry=registry,
        )
        asyncio.run(scheduler.execute())
        typer.echo("Workflow completed.")
    except Exception as exc:
        typer.echo(f"Execution error: {exc}")
        typer.echo("Note: Full block execution requires all block types to be installed and configured.")
        raise typer.Exit(code=1) from None


@app.command()
def blocks() -> None:
    """List all installed blocks."""
    from scistudio.blocks.registry import BlockRegistry

    registry = BlockRegistry()
    try:
        registry.scan()
    except Exception as exc:
        typer.echo(f"Warning: registry scan encountered errors: {exc}", err=True)

    specs = registry.all_specs()
    if not specs:
        typer.echo("No blocks found.")
        return

    all_specs = sorted(specs.values(), key=lambda s: (s.base_category, s.subcategory, s.name))

    name_w = max(max(len(s.name) for s in all_specs), 4)
    base_w = max(max(len(s.base_category) for s in all_specs), 8)
    sub_w = max(max(len(s.subcategory) for s in all_specs), 11)
    ver_w = max(max(len(s.version) for s in all_specs), 7)

    header = f"{'Name':<{name_w}}  {'Category':<{base_w}}  {'Subcategory':<{sub_w}}  {'Version':<{ver_w}}  Description"
    typer.echo(header)
    typer.echo("-" * len(header))

    for spec in all_specs:
        desc = spec.description[:60] if spec.description else ""
        typer.echo(
            f"{spec.name:<{name_w}}  {spec.base_category:<{base_w}}  "
            f"{spec.subcategory:<{sub_w}}  {spec.version:<{ver_w}}  {desc}"
        )

    typer.echo(f"\nFound {len(all_specs)} block(s)")


@app.command("init-block-package")
def init_block_package(
    name: str = typer.Argument(..., help="Package name (e.g. scistudio-blocks-srs)"),
    display_name: str = typer.Option("", "--display-name", help="Human-readable display name"),
    author: str = typer.Option("", "--author", help="Author name"),
    description: str = typer.Option("", "--description", help="One-line package description"),
) -> None:
    """Scaffold a new SciStudio block package.

    Creates a ready-to-develop package directory with pyproject.toml,
    entry-points configuration, example block, and tests.
    """
    from scistudio.cli._scaffold import scaffold_block_package

    output_dir = Path.cwd()
    try:
        result = scaffold_block_package(
            output_dir,
            name,
            author=author,
            description=description,
            display_name=display_name,
        )
    except FileExistsError:
        typer.echo(f"Error: directory '{name}' already exists.", err=True)
        raise typer.Exit(code=1) from None

    root: Path = result["root"]
    files: list[str] = result["files"]

    typer.echo(f"Created block package: {root.name}/")
    for f in files:
        typer.echo(f"  {f}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {root.name}")
    typer.echo("  pip install -e '.[dev]'")
    typer.echo("  pytest")
    typer.echo("  scistudio blocks  # verify registration")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", envvar="SCISTUDIO_HOST", help="Interface to bind."),
    port: int = typer.Option(8000, help="Port to bind."),
    root_path: str = typer.Option(
        "",
        "--root-path",
        envvar="SCISTUDIO_ROOT_PATH",
        help="Mount prefix the app is served under (e.g. /user/alice/scistudio). Default: root mount.",
    ),
) -> None:
    """Start the FastAPI server."""
    import os

    import uvicorn

    from scistudio.utils.logging import configure_logging

    # #1741: console + persistent file logging; log_config=None lets uvicorn's
    # access/error loggers propagate to the root handlers (so they hit the file).
    configure_logging(os.environ.get("SCISTUDIO_LOG_LEVEL", "INFO").upper())
    # ADR-055 Spec 0 (FR-001/FR-007): normalize once here and mirror the value
    # into the environment; create_app applies it as the FastAPI root_path.
    root_path = _normalize_root_path_or_exit(root_path)
    os.environ["SCISTUDIO_ROOT_PATH"] = root_path
    typer.echo(f"Starting SciStudio server on {host}:{port}{root_path}...")
    # ADR-035 §3.10: see comment in `gui` for why this is needed. The mount
    # prefix rides along so worker callbacks resolve under it (FR-006), and
    # the callback host follows the bind host (a specific non-loopback bind
    # does not listen on 127.0.0.1 — Codex review on PR #2274).
    os.environ.setdefault("SCISTUDIO_ENGINE_API_URL", f"http://{_worker_callback_host(host)}:{port}{root_path}")
    # The prefix is deliberately NOT passed as uvicorn's own root_path: modern
    # uvicorn *prepends* its root_path onto every incoming path (built for
    # proxies that strip the prefix), while ADR-055's proxy contract forwards
    # the prefix verbatim. FastAPI's app-level root_path (set from the env var
    # in create_app) is the verbatim-proxy-correct mechanism (FR-001).
    uvicorn.run("scistudio.api.app:create_app", host=host, port=port, factory=True, log_config=None)


def _worker_callback_host(bind_host: str) -> str:
    """Host that worker subprocesses should call back on (ADR-035 §3.10).

    Workers run on the same machine. A wildcard bind (``0.0.0.0``/``::``) or
    an explicit loopback bind is reachable via ``127.0.0.1``; a specific
    non-loopback bind does NOT listen on loopback, so the callback must
    advertise the bind host itself (Codex review on PR #2274).
    """
    if bind_host in ("0.0.0.0", "::"):
        return "127.0.0.1"
    return bind_host


def _normalize_root_path_or_exit(raw: str) -> str:
    """Normalize the CLI mount prefix; exit 2 with a clear error on a
    reserved-namespace collision (see app.normalize_root_path)."""
    from scistudio.api.app import normalize_root_path

    try:
        return normalize_root_path(raw)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _ephemeral_port(host: str) -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@app.command()
def gui(
    port: int = typer.Option(8000, help="Port for the API server"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open browser automatically"),
    bundled: bool = typer.Option(False, "--bundled", help="Run in desktop bundled mode"),
    host: str = typer.Option(
        "",
        "--host",
        envvar="SCISTUDIO_HOST",
        help="Interface to bind. Default: 127.0.0.1 in bundled mode, 0.0.0.0 otherwise.",
    ),
    root_path: str = typer.Option(
        "",
        "--root-path",
        envvar="SCISTUDIO_ROOT_PATH",
        help="Mount prefix the app is served under (e.g. /user/alice/scistudio). Default: root mount.",
    ),
) -> None:
    """Launch SciStudio GUI in your default browser."""
    import json
    import os
    import threading
    import webbrowser

    import uvicorn

    from scistudio.utils.logging import configure_logging

    # #1741: console + persistent JSON-line file logging (replaces bare
    # basicConfig). Backend/engine/event-bus/websocket logs now land on disk for
    # alpha bug reproduction. Idempotent with the create_app fallback.
    log_level = os.environ.get("SCISTUDIO_LOG_LEVEL", "INFO").upper()
    log_file = configure_logging(log_level)
    if log_file is not None and not bundled:
        typer.echo(f"Logging to {log_file}")

    # ADR-055 Spec 0 (FR-001/FR-007): normalize once here and mirror the value
    # into the environment; create_app applies it as the FastAPI root_path.
    root_path = _normalize_root_path_or_exit(root_path)
    os.environ["SCISTUDIO_ROOT_PATH"] = root_path

    server_host = host or ("127.0.0.1" if bundled else "0.0.0.0")
    public_host = host if host and host not in ("0.0.0.0", "::") else ("127.0.0.1" if bundled else "localhost")
    bound_port = _ephemeral_port(public_host) if port == 0 else port
    url = f"http://{public_host}:{bound_port}{root_path}"
    if bundled:
        os.environ.setdefault("SCISTUDIO_BUNDLED", "1")
        typer.echo(
            json.dumps(
                {
                    "event": "scistudio.ready",
                    "host": public_host,
                    "port": bound_port,
                    "url": url,
                },
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"Starting SciStudio GUI on {url} ...")
    # ADR-035 §3.10: workers spawned by the engine call back via this URL to
    # request PTY tabs. The engine alone knows the bound port at startup, so
    # export it here before uvicorn forks any worker. Companion to
    # SCISTUDIO_ENGINE_IPC_TOKEN (set in api.app:lifespan). ADR-055 Spec 0:
    # the mount prefix rides along so callbacks resolve under it (FR-006), and
    # the callback host follows the bind host (a specific non-loopback bind
    # does not listen on 127.0.0.1 — Codex review on PR #2274).
    os.environ.setdefault(
        "SCISTUDIO_ENGINE_API_URL",
        f"http://{_worker_callback_host(server_host)}:{bound_port}{root_path}",
    )
    if not no_browser and not bundled:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    # #1865: in bundled desktop mode, self-reap if the Electron parent dies
    # without signalling us (force-quit / crash / app.exit relaunch) so the
    # backend does not linger as an orphan. POSIX-only: relies on reparent-to-init.
    if bundled and os.name == "posix":
        from scistudio.desktop.parent_watchdog import start_parent_death_watchdog

        start_parent_death_watchdog(os.getppid())
    # #1741: log_config=None lets uvicorn loggers propagate to our root handlers.
    # The prefix is deliberately NOT passed as uvicorn's own root_path: modern
    # uvicorn *prepends* its root_path onto every incoming path (built for
    # proxies that strip the prefix), while ADR-055's proxy contract forwards
    # the prefix verbatim. FastAPI's app-level root_path (set from the env var
    # in create_app) is the verbatim-proxy-correct mechanism (FR-001).
    uvicorn.run(
        "scistudio.api.app:create_app",
        host=server_host,
        port=bound_port,
        factory=True,
        log_config=None,
    )


if __name__ == "__main__":
    app()
