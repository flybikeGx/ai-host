"""Authenticated central Streamable HTTP MCP; intentionally no publish tool."""
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
from .knowledge import Knowledge,KnowledgeError
from .knowledge_mcp import TOOLS,call
from .processes import identity


def application(config):
    store=Knowledge(config);server=Server('knowledge')
    manager=StreamableHTTPSessionManager(server,json_response=True,stateless=True)
    control=(store.root/'service-token').read_text().strip()

    def grant(request):
        authorization=request.headers.get('authorization','')
        return store.grant(authorization.removeprefix('Bearer '))

    @server.list_tools()
    async def tools():return [types.Tool(**t) for t in TOOLS]

    @server.call_tool()
    async def invoke(name,arguments):
        try:result=call(store,grant(server.request_context.request),name,arguments)
        except Exception as error:
            code=error.status if isinstance(error,KnowledgeError) else 400
            return types.CallToolResult(isError=True,content=[types.TextContent(type='text',text=json.dumps({'error':str(error)[:300],'status':code},ensure_ascii=False))])
        return [types.TextContent(type='text',text=json.dumps(result,ensure_ascii=False))]

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():yield

    async def health(request):return JSONResponse({'pid':os.getpid(),'stats':store.stats()})
    class Endpoint:
        async def __call__(self,scope,receive,send):await manager.handle_request(scope,receive,send)
    app=Starlette(routes=[Route('/health',health),Route('/mcp',Endpoint(),methods=['GET','POST','DELETE'])],lifespan=lifespan)
    class Guard:
        async def __call__(self,scope,receive,send):
            if scope['type']=='http':
                request=Request(scope)
                if request.headers.get('origin'):
                    return await JSONResponse({'error':'MCP 不接受浏览器来源'},status_code=403)(scope,receive,send)
                try:
                    if request.url.path=='/health':
                        if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+control):raise KnowledgeError('需要服务凭证',401)
                    else:grant(request)
                except KnowledgeError as error:return await JSONResponse({'error':str(error)},status_code=error.status)(scope,receive,send)
            await app(scope,receive,send)
    return Guard()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=Config.load(args.config);store=Knowledge(config)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        item={'pid':os.getpid(),'birth':identity(os.getpid()),'endpoint':f'http://127.0.0.1:{sock.getsockname()[1]}/mcp'}
        file=store.root/'service.json.tmp';file.write_text(json.dumps(item));file.replace(store.root/'service.json')
        uvicorn.Server(uvicorn.Config(application(config),log_level='warning',timeout_graceful_shutdown=5)).run(sockets=[sock])


if __name__=='__main__':main()
