"""Read-only human API for the shared Info Board.

Writes are intentionally absent: publishing, editing and deleting happen only
through the MCP surface. Reads use the same authentication policy as the rest of
the workspace — a configured management token or browser login once accounts
exist, and the account-free local workspace otherwise.
"""
from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .info_board import InfoBoard, InfoBoardError
from .info_board_service import InfoBoardService


def register(app, config):
    store = InfoBoard(config)
    app.state.info_board = store

    @app.exception_handler(InfoBoardError)
    async def invalid(request, error):
        return JSONResponse({'error': str(error), 'code': error.code, 'status': error.status},
                            status_code=error.status)

    def reader(request: Request):
        token = os.environ.get('AGENT_BASE_WEB_TOKEN')
        if token and secrets.compare_digest(request.headers.get('authorization', ''),
                                            'Bearer ' + token):
            return
        browser = getattr(app.state, 'browser_auth', None)
        if browser and (browser.required() or request.cookies.get('agent_base_session')):
            browser.require_workspace(request)
            return
        if token:
            raise HTTPException(401, '需要登录或 Bearer token')

    @app.get('/api/info/status')
    def status(request: Request):
        reader(request)
        service = InfoBoardService(config).status()
        service.pop('endpoint', None)
        return {'stats': store.stats(), 'service': service, 'readonly': True}

    @app.get('/api/info/posts')
    def posts(request: Request, offset: int = 0, limit: int = 20, query: str = ''):
        reader(request)
        return store.list_posts(query, offset, limit)

    @app.get('/api/info/posts/{post_id}')
    def post(post_id: str, request: Request):
        reader(request)
        return store.get(post_id)
