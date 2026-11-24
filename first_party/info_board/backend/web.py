"""Read-only human API for the shared Info Board.

Writes are intentionally absent: publishing, editing and deleting happen only
through MCP tools. Reads use the same authentication policy as the rest of the
workspace — a configured management token or browser login once accounts exist,
and the account-free local workspace otherwise. The package page and API are
unavailable (404) until the package is installed and enabled.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from agent_base.mcp_packages import Installations, load_package

from .store import InfoBoard, InfoBoardError

PACKAGE = load_package(Path(__file__).resolve().parents[1])


def _enabled(config) -> bool:
    return Installations(config.data / "mcp").effective(PACKAGE).enabled


def _data_root(config) -> Path:
    return Path(config.data) / PACKAGE.data_dir


def register(app, config):
    store = InfoBoard(_data_root(config))
    app.state.info_board = store

    def readable(request: Request):
        if not _enabled(config):
            raise HTTPException(404, "信息台页面不可用")
        token = os.environ.get("AGENT_BASE_WEB_TOKEN")
        if token and secrets.compare_digest(request.headers.get("authorization", ""),
                                            "Bearer " + token):
            return
        browser = getattr(app.state, "browser_auth", None)
        if browser and (browser.required() or request.cookies.get("agent_base_session")):
            browser.require_workspace(request)
            return
        if token:
            raise HTTPException(401, "需要登录或 Bearer token")

    def failure(error: InfoBoardError):
        raise HTTPException(error.status, str(error))

    @app.get("/api/info/status")
    def status(request: Request):
        readable(request)
        from agent_base.mcp_runtime import PackageRuntime
        service = PackageRuntime(config, PACKAGE).status()
        service.pop("endpoint", None)
        return {"stats": store.stats(), "service": service, "readonly": True}

    @app.get("/api/info/posts")
    def posts(request: Request, offset: int = 0, limit: int = 20, query: str = ""):
        readable(request)
        try:
            return store.list_posts(query, offset, limit)
        except InfoBoardError as error:
            failure(error)

    @app.get("/api/info/posts/{post_id}")
    def post(post_id: str, request: Request):
        readable(request)
        try:
            return store.get(post_id)
        except InfoBoardError as error:
            failure(error)
