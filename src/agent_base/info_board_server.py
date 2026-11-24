"""Authenticated shared Streamable HTTP MCP for the platform Info Board."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
from contextlib import asynccontextmanager

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Config
from .info_board import InfoBoard, InfoBoardError
from .info_board_mcp import TOOLS, call
from .processes import identity


def error_payload(error):
    status = getattr(error, 'status', 400)
    code = getattr(error, 'code', 'invalid')
    return {'error': str(error)[:300], 'code': code, 'status': status}, status


def application(config, root=None):
    store = InfoBoard(config, root)
    server = Server('info-board')
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
    control = (store.root / 'service-token').read_text().strip()

    def grant(request):
        authorization = request.headers.get('authorization', '')
        return store.grant(authorization.removeprefix('Bearer '))

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
                types.TextContent(type='text', text=json.dumps(payload, ensure_ascii=False, separators=(',', ':')))])
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
    parser.add_argument('--config', required=True)
    parser.add_argument('--data')
    args = parser.parse_args()
    config = Config.load(args.config)
    store = InfoBoard(config, args.data)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        item = {'pid': os.getpid(), 'birth': identity(os.getpid()),
                'endpoint': f'http://127.0.0.1:{sock.getsockname()[1]}/mcp'}
        temporary = store.root / 'service.json.tmp'
        temporary.write_text(json.dumps(item), encoding='utf-8')
        os.chmod(temporary, 0o600)
        temporary.replace(store.root / 'service.json')
        uvicorn.Server(uvicorn.Config(application(config, args.data), log_level='warning',
                                      timeout_graceful_shutdown=5)).run(sockets=[sock])


if __name__ == '__main__':
    main()
