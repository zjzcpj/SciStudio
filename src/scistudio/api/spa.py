"""SPA fallback static file handler.

Returns index.html for any request path that does not match a real static
file.  Required for client-side routing: deep URLs like
``/projects/123/workflows`` must return the SPA shell, not 404.

ADR-055 Spec 0 (FR-003): when the app is mounted under a configured prefix
(``SCISTUDIO_ROOT_PATH`` / ``--root-path``), the served ``index.html`` — and
only that document — is templated with a bootstrap assignment
(``window.__SCISTUDIO_BASE_PATH__``) so the already-built SPA learns the
prefix at runtime. Hashed asset files are served byte-identical, so caching
and OTA packaging are unaffected, and no per-deployment rebuild is needed.
With the default empty prefix the base-path assignment is not emitted
(Spec 0 FR-002).

ADR-055 Spec 1 (FR-006) extends the same bootstrap with the per-launch
WebMCP bridge session token (``window.__SCISTUDIO_WEBMCP_TOKEN__``). Unlike
the base path, the token applies to every mount including the default root
mount — desktop and ordinary local-browser pages acquire it transparently
and present it as a header on every bridge call.
"""

from __future__ import annotations

import json
import os
import re
from html import escape
from pathlib import Path
from typing import cast

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

_HEAD_OPEN = re.compile(r"<head[^>]*>", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html[^>]*>", re.IGNORECASE)


class SPAStaticFiles(StaticFiles):
    """Serve ``index.html`` for paths that do not match a real file.

    Known ``/api/*`` and ``/ws`` requests are handled by FastAPI route
    handlers registered *before* this mount. Unknown API/WebSocket paths must
    remain 404s instead of becoming ``index.html``.
    """

    def __init__(self, *, base_path: str = "", webmcp_session_token: str = "", **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Normalized mount prefix ("" or "/prefix") from app.state.root_path.
        self._base_path = base_path
        # Per-launch WebMCP bridge session token (ADR-055 Spec 1 FR-006);
        # "" disables the token bootstrap assignment.
        self._webmcp_session_token = webmcp_session_token

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve SPA routes, but never rewrite unknown API/WebSocket paths."""
        if _is_api_or_ws_path(path):
            raise HTTPException(status_code=404)
        if (self._base_path or self._webmcp_session_token) and scope.get("method", "GET") == "GET":
            full_path, stat_result = self.lookup_path(path)
            if stat_result is not None:
                if os.path.isdir(full_path):
                    # Directory URL: the redirect-to-trailing-slash stays with
                    # StaticFiles; only the actually-served index.html document
                    # is templated.
                    if scope.get("path", "").endswith("/"):
                        index_candidate = os.path.join(full_path, "index.html")
                        if os.path.isfile(index_candidate):
                            return _templated_index_response(
                                index_candidate, self._base_path, self._webmcp_session_token
                            )
                elif os.path.basename(full_path) == "index.html":
                    # Direct index.html request or SPA fallback (lookup_path
                    # already resolved a missing path to index.html).
                    return _templated_index_response(full_path, self._base_path, self._webmcp_session_token)
        return await super().get_response(path, scope)

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        """Return the real file if it exists, otherwise ``index.html``."""
        full_path, stat_result = super().lookup_path(path)
        if stat_result is None:
            if _is_api_or_ws_path(path):
                return full_path, stat_result
            return cast(tuple[str, os.stat_result | None], super().lookup_path("index.html"))
        return full_path, stat_result


def _templated_index_response(index_path: str, base_path: str, webmcp_session_token: str = "") -> Response:
    """Serve ``index.html`` with the runtime bootstrap injected.

    Three injections, placed immediately after ``<head>`` (falling back to
    ``<html>``, then the top of the document) so they take effect before any
    module script or asset reference:

    * ``<base href="<prefix>/">`` (only when a prefix is configured) — the
      Vite build uses ``base: "./"``, so asset references are
      document-relative; on a deep SPA route (``/p/projects/foo``) they would
      otherwise resolve to ``/p/projects/assets/...`` and hit the SPA
      fallback instead of the static file. The base element pins resolution
      to the prefix root. Root-absolute URLs (``/api/...``) and full
      WebSocket URLs are unaffected by ``<base>``, so the API/WS contract is
      unchanged. The empty prefix emits no ``<base>`` (Spec 0 FR-002).
    * ``window.__SCISTUDIO_BASE_PATH__`` (only when a prefix is configured) —
      the runtime prefix the frontend base-path module reads (Spec 0 FR-003).
    * ``window.__SCISTUDIO_WEBMCP_TOKEN__`` (when a bridge session token is
      configured) — the per-launch WebMCP bridge session token, injected on
      every mount including the default root mount (Spec 1 FR-006).

    ``json.dumps`` keeps each JS value a safely quoted string literal;
    ``html.escape`` does the same for the attribute context.
    ``Cache-Control: no-cache`` because the body no longer matches the file
    on disk — a cached unprefixed shell must never be reused under a
    prefixed deployment, and a cached page must never carry a stale session
    token.
    """
    html = Path(index_path).read_text(encoding="utf-8")
    injection = ""
    if base_path:
        base_href = escape(f"{base_path}/", quote=True)
        injection += f'<base href="{base_href}">'
    assignments = ""
    if base_path:
        assignments += f"window.__SCISTUDIO_BASE_PATH__ = {json.dumps(base_path)};"
    if webmcp_session_token:
        assignments += f"window.__SCISTUDIO_WEBMCP_TOKEN__ = {json.dumps(webmcp_session_token)};"
    injection += f"<script>{assignments}</script>"
    for pattern in (_HEAD_OPEN, _HTML_OPEN):
        match = pattern.search(html)
        if match is not None:
            html = f"{html[: match.end()]}{injection}{html[match.end() :]}"
            break
    else:
        html = f"{injection}{html}"
    return Response(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


def _is_api_or_ws_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == "api" or normalized.startswith("api/") or normalized == "ws" or normalized.startswith("ws/")
