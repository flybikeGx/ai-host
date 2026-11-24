"""Shared Streamable HTTP MCP entry for the Info Board package.

Run as ``python first_party/info_board/server.py --port P --data D`` or via the
package manifest (``service.runtime.argv``). The process binds loopback only,
serves ``/health`` for the platform probe and ``/mcp`` for Streamable HTTP MCP.
The control token lives in ``<data>/service-token``; platform-issued grants are
HMAC-signed and only identify the author for display and audit.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Allow both ``python first_party/info_board/server.py`` and
# ``python -m first_party.info_board.server`` without requiring an install step.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_base.mcp_runtime import verify_grant  # noqa: E402
from first_party.info_board.backend.mcp import TOOLS, call  # noqa: E402
from first_party.info_board.backend.store import InfoBoard, InfoBoardError  # noqa: E402


def error_payload(error):
    status = getattr(error, 'status', 400)
    code = getattr(error, 'code', 'invalid')
    return {'error': str(error)[:300], 'code': code, 'status': status}, status


def application(data: Path):
    store = InfoBoard(data)
    server = Server('info-board')
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
    token_file = store.root / 'service-token'
    try:
        control = token_file.read_text(encoding='utf-8').strip()
    except OSError as error:
        raise SystemExit(f'缺少服务凭证 {token_file}：{error}') from error

    def grant(request):
        token = request.headers.get('authorization', '').removeprefix('Bearer ')
        if secrets.compare_digest(token, control):
            return {'actor': 'local-admin', 'kind': 'agent'}
        try:
            return verify_grant(control, token)
        except ValueError:
            raise InfoBoardError('信息台授权无效或已过期', 401, 'unauthorized') from None

    @server.list_tools()
    async def tools():
        return [types.Tool(**tool) for tool in TOOLS]

    @server.call_tool()
    async def invoke(name, arguments):
        request = server.request_context.request
        try:
            result = call(store, grant(request), name, arguments)
            text = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        except Exception as error:
            payload, _ = error_payload(error)
            return types.CallToolResult(isError=True, content=[
                types.TextContent(type='text', text=json.dumps(payload, ensure_ascii=False,
                                                               separators=(',', ':')))])
        return [types.TextContent(type='text', text=text)]

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def health(request):
        return JSONResponse({'pid': os.getpid(), 'stats': store.stats()})

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    app = Starlette(routes=[Route('/health', health),
                            Route('/mcp', Endpoint(), methods=['GET', 'POST', 'DELETE'])],
                    lifespan=lifespan)

    class Guard:
        async def __call__(self, scope, receive, send):
            if scope['type'] == 'http':
                request = Request(scope)
                if request.headers.get('origin'):
                    payload = {'error': 'MCP 不接受浏览器来源', 'code': 'forbidden', 'status': 403}
                    return await JSONResponse(payload, status_code=403)(scope, receive, send)
                try:
                    if request.url.path == '/health':
                        if not secrets.compare_digest(request.headers.get('authorization', ''),
                                                      'Bearer ' + control):
                            raise InfoBoardError('需要服务凭证', 401, 'unauthorized')
                    else:
                        grant(request)
                except InfoBoardError as error:
                    payload, status = error_payload(error)
                    return await JSONResponse(payload, status_code=status)(scope, receive, send)
            await app(scope, receive, send)

    return Guard()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--data', required=True)
    args = parser.parse_args()
    uvicorn.run(application(Path(args.data)), host='127.0.0.1', port=args.port,
                log_level='warning', timeout_graceful_shutdown=5)


if __name__ == '__main__':
    main()
