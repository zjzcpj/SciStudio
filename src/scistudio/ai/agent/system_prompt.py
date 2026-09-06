"""Compose the system prompt for the embedded agent.

ADR-040 §3.3 / §3.4 — I40a Phase 2a implementation:

1. :func:`_load_skill_md` prefers ``importlib.resources`` for
   ``scistudio._skills.scistudio.SKILL.md`` (closes #824, wheel-install
   regression). Falls back to the legacy walk-up resolver if the
   packaged path is empty — the Skills track (S40b) ships the
   relocated SKILL.md on a sibling tracking branch; the fallback keeps
   this branch working in isolation.

   TODO(#1012): drop the legacy walk-up fallback once the Skills track
   merges to main. Followup: ADR-040 cascade Phase 2c.

2. :func:`_render_tool_catalog` enumerates FastMCP's ``list_tools()``
   surface (replacing the deleted ``_registry.TOOL_REGISTRY``).
3. :func:`_render_project_context` renders a per-project dynamic block
   spliced between ``<!-- project_context:begin/end -->`` markers
   (closes #825). Fields: project_name, workflow_count,
   installed_plugins, optional git branch/sha, recent workflows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Markers in SKILL.md that delimit rendered insertion blocks.
_TOOL_CATALOG_BEGIN = "<!-- tool_catalog:begin -->"
_TOOL_CATALOG_END = "<!-- tool_catalog:end -->"
_PROJECT_CONTEXT_BEGIN = "<!-- project_context:begin -->"
_PROJECT_CONTEXT_END = "<!-- project_context:end -->"

__all__ = ["compose_system_prompt"]


def compose_system_prompt(project_dir: Path) -> str:
    """Return the system prompt string for ``project_dir``.

    Per ADR-040 §3.3, the ``project_dir`` argument is now load-bearing:
    a rendered project-context section is spliced into the SKILL.md
    between ``<!-- project_context:begin -->`` and
    ``<!-- project_context:end -->`` markers.

    Parameters
    ----------
    project_dir
        Active project root. Used to render the project_context section.

    Returns
    -------
    str
        The full system prompt — SKILL.md content with both the
        tool_catalog and the project_context blocks re-rendered.

    Raises
    ------
    FileNotFoundError
        When SKILL.md cannot be located (broken install / worktree).
    """
    skill_md = _load_skill_md()
    catalog = _render_tool_catalog()
    project_context = _render_project_context(project_dir)
    with_catalog = _splice(skill_md, _TOOL_CATALOG_BEGIN, _TOOL_CATALOG_END, catalog)
    return _splice(with_catalog, _PROJECT_CONTEXT_BEGIN, _PROJECT_CONTEXT_END, project_context)


def _load_skill_md() -> str:
    """Load the base SKILL.md.

    Resolution order (first hit wins):

    1. ``importlib.resources.files("scistudio") / "_skills" / "scistudio" /
       "SKILL.md"`` — the canonical post-ADR-040 §3.4 location. Survives
       wheel installs (closes #824).
    2. Legacy walk-up from ``__file__`` for repo-root
       ``skills/scistudio/SKILL.md``. Kept so this branch works while the
       Skills track ships the relocated file on a sibling tracking
       branch.

    Returns
    -------
    str
        Full text of the base SKILL.md.

    Raises
    ------
    FileNotFoundError
        When neither resolution path finds a SKILL.md.
    """
    # 1. Packaged path (ADR-040 §3.4 — wheel-safe).
    try:
        packaged = files("scistudio") / "_skills" / "scistudio" / "SKILL.md"
        if packaged.is_file():
            content = packaged.read_text(encoding="utf-8")
            if content:
                return content
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        # importlib.resources returns a Traversable that may raise when
        # the package-data entry is absent. Fall through to legacy path.
        logger.debug(
            "_load_skill_md: importlib.resources lookup failed; falling back to walk-up",
            exc_info=True,
        )

    # 2. Legacy walk-up — kept until Skills track merges to main.
    # TODO(#1012): drop this branch once src/scistudio/_skills/scistudio/SKILL.md
    #   ships on main via the Skills track (S40b/I40b). Out of scope per
    #   ADR-040 §3.4 / cascade Phase 2c. Followup:
    #   https://github.com/zjzcpj/SciStudio/issues/1012.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "skills" / "scistudio" / "SKILL.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        if (parent / "pyproject.toml").is_file():
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
            break
    raise FileNotFoundError(
        "skills/scistudio/SKILL.md was not found via importlib.resources "
        f"(scistudio._skills.scistudio) nor via walk-up from {here}. "
        "Reinstall SciStudio or run from a checkout that includes the "
        "skills/ tree."
    )


def _render_tool_catalog() -> str:
    """Build the ``<!-- tool_catalog -->`` block contents from FastMCP.

    Walks ``await mcp.list_tools()`` in declaration order, grouping by
    category derived from the tool's ``tags`` (``category:<name>``).
    Each line is ``- `<name>` [<mutation>] — <description>``.
    """
    # Local import keeps the ai.agent package import-light at module
    # load (tools are imported from scistudio.ai.agent.mcp.__init__ which
    # may not be triggered yet on the first compose_system_prompt call).
    # Ensure tool modules are imported so the @mcp.tool decorators have run.
    # The package __init__ does this, but compose can be invoked before
    # any other module has touched scistudio.ai.agent.mcp.
    import scistudio.ai.agent.mcp  # noqa: F401  # side-effect: register tools
    from scistudio.ai.agent.mcp.server import AUDIENCE_EXTERNAL_TAG, mcp, tool_category_and_mutation

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Called from inside an event loop (e.g. ai_pty websocket).
        # ``run_coroutine_threadsafe(coro, this_same_loop)`` deadlocks
        # because the coroutine schedules on the same loop the caller
        # is blocking — Codex P1 (PR #1053 review). Spin a fresh event
        # loop on a worker thread so the coroutine has somewhere to run.
        import concurrent.futures

        def _list_in_thread() -> Any:
            return asyncio.run(mcp.list_tools())  # type: ignore[arg-type]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            tools = pool.submit(_list_in_thread).result(timeout=5.0)
    else:
        tools = asyncio.run(mcp.list_tools())  # type: ignore[arg-type]

    category_titles = {
        "workflow": "### (a) Workflow design & execution",
        "authoring": "### (b) Block authoring",
        "inspection": "### (c) Run & data inspection",
        "qa": "### (d) Project Q&A",
        "plot": "### (e) Plot authoring",
    }
    grouped: dict[str, list[str]] = {key: [] for key in category_titles}

    for tool in tools:
        tags = set(tool.tags or set())
        if AUDIENCE_EXTERNAL_TAG in tags:
            # ADR-055 Spec 1 (FR-004): external-audience tools are filtered
            # out of the local socket transport's tools/list; the prompt
            # catalogue must match what the local agent can actually call,
            # or it would be told to use tools absent from its transport.
            continue
        category, mutation = tool_category_and_mutation(tags)
        description = (tool.description or "").strip().split("\n", 1)[0]
        line = f"- `{tool.name}` [{mutation}] — {description}"
        grouped.setdefault(category, []).append(line)

    lines: list[str] = []
    for cat, title in category_titles.items():
        if not grouped.get(cat):
            continue
        if lines:
            lines.append("")
        lines.append(title)
        lines.append("")
        lines.extend(grouped[cat])
    return "\n".join(lines)


def _render_project_context(project_dir: Path) -> str:
    """Render the per-project dynamic context block (ADR-040 §3.3, closes #825).

    Fields per the §3.3 field-source table:

    | Field | Source |
    |---|---|
    | ``project_name`` | ``project.yaml::project.name`` or ``project_dir.name`` fallback |
    | ``workflow_count`` | ``len(list((project_dir / 'workflows').glob('*.yaml')))`` via os.scandir |
    | ``installed_plugins`` | live BlockRegistry enumeration |
    | ``branch``, ``sha`` | best-effort ``git -C <project_dir> rev-parse``; omit on failure |
    | ``recent_workflows`` | top 3 by ``Path.stat().st_mtime`` |

    Performance budget: <100ms even at 1000 workflows (uses os.scandir).
    """
    if not project_dir or not Path(project_dir).is_dir():
        return (
            "No active SciStudio project is open. Most MCP tools (workflow read/write, "
            "block authoring, data inspection) require an open project. Ask the user "
            "to open or create one before invoking them."
        )

    pdir = Path(project_dir)

    # 1. Project name.
    project_name = pdir.name
    project_file = pdir / "project.yaml"
    if project_file.is_file():
        try:
            import yaml as yaml_module

            raw = yaml_module.safe_load(project_file.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                meta = raw.get("project", {})
                if isinstance(meta, dict) and meta.get("name"):
                    project_name = str(meta["name"])
        except Exception:
            logger.debug("_render_project_context: project.yaml parse failed", exc_info=True)

    # 2. Workflow count + recent workflows (top 3 by mtime).
    workflows_dir = pdir / "workflows"
    workflow_count = 0
    recent_entries: list[tuple[float, str]] = []
    if workflows_dir.is_dir():
        try:
            with os.scandir(workflows_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not (name.endswith(".yaml") or name.endswith(".yml")):
                        continue
                    workflow_count += 1
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    recent_entries.append((mtime, name))
        except OSError:
            logger.debug("_render_project_context: scandir(workflows) failed", exc_info=True)
    recent_entries.sort(reverse=True)
    top3 = recent_entries[:3]

    # 3. Installed plugins (best-effort, never blocks).
    installed_plugins: list[str] = []
    try:
        from scistudio.ai.agent.mcp._context import get_optional_context

        ctx = get_optional_context()
        if ctx is not None:
            block_registry = getattr(ctx, "block_registry", None)
            if block_registry is not None:
                # Plugins are tracked via the registry's plugin map when
                # available; otherwise infer from specs metadata.
                plugins_attr = getattr(block_registry, "installed_plugins", None)
                if plugins_attr is not None:
                    try:
                        installed_plugins = sorted(set(plugins_attr() if callable(plugins_attr) else plugins_attr))
                    except Exception:
                        installed_plugins = []
    except Exception:
        logger.debug("_render_project_context: plugin enumeration failed", exc_info=True)

    # 4. Git branch / sha — best effort.
    branch: str | None = None
    sha: str | None = None
    if (pdir / ".git").exists():
        try:
            branch_proc = subprocess.run(
                ["git", "-C", str(pdir), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if branch_proc.returncode == 0:
                branch = branch_proc.stdout.strip() or None
            sha_proc = subprocess.run(
                ["git", "-C", str(pdir), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if sha_proc.returncode == 0:
                sha = sha_proc.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            logger.debug("_render_project_context: git probe failed", exc_info=True)

    # 5. Render.
    lines: list[str] = []
    lines.append(f"**Project:** {project_name}")
    lines.append(f"**Path:** {pdir}")
    if workflow_count == 1:
        lines.append("**Workflows:** 1 workflow on disk")
    else:
        lines.append(f"**Workflows:** {workflow_count} workflows on disk")

    if installed_plugins:
        lines.append(f"**Installed block plugins:** {', '.join(installed_plugins)}")

    if branch or sha:
        git_parts: list[str] = []
        if branch:
            git_parts.append(f"branch `{branch}`")
        if sha:
            git_parts.append(f"sha `{sha}`")
        lines.append(f"**Git:** {' @ '.join(git_parts)}")

    if top3:
        lines.append("")
        lines.append("**Recently-modified workflows:**")
        now = time.time()
        for mtime, name in top3:
            lines.append(f"- `{name}` ({_format_age(now - mtime)})")

    return "\n".join(lines)


def _format_age(seconds_ago: float) -> str:
    """Format an mtime delta as 'Nh ago' / 'Nd ago' / 'Nw ago'."""
    if seconds_ago < 0:
        return "just now"
    minutes = seconds_ago / 60.0
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24.0
    if days < 7:
        return f"{int(days)}d ago"
    weeks = days / 7.0
    return f"{int(weeks)}w ago"


def _splice(text: str, begin_marker: str, end_marker: str, body: str) -> str:
    """Replace the body between ``begin_marker`` and ``end_marker`` in ``text``.

    Falls back to appending the marker block at the end of ``text``
    when the marker pair is missing — surfacing a warning so a stale
    SKILL.md is easy to diagnose without breaking the agent.
    """
    begin = text.find(begin_marker)
    end = text.find(end_marker)
    if begin == -1 or end == -1 or end < begin:
        logger.warning(
            "SKILL.md is missing the %s / %s markers; appending the rendered block at end.",
            begin_marker,
            end_marker,
        )
        return f"{text}\n\n{begin_marker}\n{body}\n{end_marker}\n"
    end_marker_end = end + len(end_marker)
    return text[: begin + len(begin_marker)] + "\n" + body + "\n" + text[end:end_marker_end] + text[end_marker_end:]
