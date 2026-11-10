"""Local admin HTTP API and a small UI; the same queue as CLI/cron."""
from __future__ import annotations

import os
import secrets
import threading
import asyncio
import json
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .mcp_server import public
from .scheduler import Scheduler


class WorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repo: str
    task: str | None = None
    ref: str | None = None
    model: str | None = None
    effort: str | None = None
    workspace_source: str | None = None
    timeout_seconds: float = Field(default=1800, gt=0, le=86400)
    cron: str | None = None
    timezone: str = "Asia/Shanghai"
    overlap: str = "skip"


class Activation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict = Field(default_factory=dict)


class RepoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str
    ref: str | None = None


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = "admin"
    title: str | None = None
    model: str | None = None
    effort: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    client_key: str


class ChatUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    archived: bool | None = None


def create_app(config, *, background=True):
    scheduler = Scheduler(config)
    from .repositories import Repositories
    repositories = Repositories(config)
    from .conversations import Conversations, ConversationBusy
    conversations = Conversations(config)
    token = os.environ.get("AGENT_BASE_WEB_TOKEN")

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=scheduler.run, name="agent-scheduler", daemon=True)
        if background:
            thread.start()
        yield
        scheduler.stop_event.set()
        if background:
            thread.join(timeout=5)
        # Detached run supervisors continue; restarting HTTP does not replay them.

    app = FastAPI(title="Agent Base", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != str(request.base_url).rstrip("/")):
                return JSONResponse({"detail": "cross-origin write rejected"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(ConversationBusy)
    async def conversation_busy(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def authorize(authorization: str | None = Header(default=None)):
        if token and not secrets.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "需要 Bearer token")

    api = [Depends(authorize)]
    static = Path(__file__).parent / "static" / "chat"
    if (static / "assets").is_dir():
        app.mount("/chat/assets", StaticFiles(directory=static / "assets"), name="chat-assets")

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page():
        if not (static / "index.html").is_file():
            raise HTTPException(503, "请先在 frontend 目录执行 npm ci && npm run build")
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/chat/targets", dependencies=api)
    def chat_targets():
        return conversations.targets()

    @app.get("/api/conversations", dependencies=api)
    def chat_list(archived: bool = False):
        return conversations.list(archived=archived)

    @app.post("/api/conversations", dependencies=api, status_code=201)
    def chat_create(body: ChatInput):
        return conversations.create(**body.model_dump())

    @app.get("/api/conversations/{chat_id}", dependencies=api)
    def chat_get(chat_id: str):
        return conversations.snapshot(chat_id)

    @app.get("/api/conversations/{chat_id}/files", dependencies=api)
    def chat_files(chat_id: str):
        return conversations.files(chat_id)

    @app.get("/api/conversations/{chat_id}/download", dependencies=api)
    def chat_download(chat_id: str, path: str):
        file = conversations.artifact(chat_id, path)
        return FileResponse(file, filename=file.name, media_type="application/octet-stream")

    @app.patch("/api/conversations/{chat_id}", dependencies=api)
    def chat_update(chat_id: str, body: ChatUpdate):
        return conversations.rename(chat_id, **body.model_dump(exclude_unset=True))

    @app.post("/api/conversations/{chat_id}/messages", dependencies=api, status_code=202)
    def chat_send(chat_id: str, body: ChatMessage):
        return conversations.send(chat_id, body.text, body.client_key)

    @app.post("/api/conversations/{chat_id}/stop", dependencies=api)
    def chat_stop(chat_id: str):
        return conversations.cancel(chat_id)

    @app.get("/api/conversations/{chat_id}/events", dependencies=api)
    async def chat_events(chat_id: str, request: Request, after: int = 0, once: bool = False):
        conversations.get(chat_id)
        try:
            cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "事件游标无效")
        async def stream():
            nonlocal cursor
            idle = 0
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(conversations.events, chat_id, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if once:
                    if len(batch) == 200:
                        continue
                    break
                if not batch:
                    idle += 1
                    if idle % 40 == 0:
                        await asyncio.to_thread(conversations.reconcile)
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.25)
                else:
                    idle = 0
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/repos", dependencies=api)
    def repos():
        return repositories.list()

    @app.post("/api/repos/install", dependencies=api, status_code=201)
    def install_repo(body: RepoInput):
        return repositories.install(body.origin, body.ref)

    @app.post("/api/repos/{installation_id}/start", dependencies=api)
    def start_repo(installation_id: str):
        return repositories.start(installation_id)

    @app.post("/api/repos/{installation_id}/stop", dependencies=api)
    def stop_repo(installation_id: str):
        return repositories.stop(installation_id)

    @app.get("/api/repos/{installation_id}", dependencies=api)
    def repo(installation_id: str):
        item = repositories.get(installation_id)
        return {**item, "health": repositories.health(item)}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML

    @app.get("/api/works", dependencies=api)
    def works():
        return scheduler.works()

    @app.post("/api/works", dependencies=api, status_code=201)
    def register(body: WorkInput):
        return scheduler.register(**body.model_dump())

    @app.post("/api/works/{name}/trigger", dependencies=api, status_code=202)
    def trigger(name: str, body: Activation, idempotency_key: str | None = Header(default=None)):
        return scheduler.trigger(name, source="web", key=idempotency_key, payload=body.payload)

    @app.post("/api/works/{name}/pause", dependencies=api)
    def pause(name: str):
        return scheduler.enable(name, False)

    @app.post("/api/works/{name}/resume", dependencies=api)
    def resume(name: str):
        return scheduler.enable(name, True)

    @app.get("/api/works/{name}/triggers", dependencies=api)
    def triggers(name: str):
        return scheduler.triggers(name)

    @app.get("/api/runs", dependencies=api)
    def runs():
        return [public(job) for job in scheduler.service.list(100)]

    @app.get("/api/runs/{run_id}", dependencies=api)
    def run(run_id: str):
        return public(scheduler.service.get(run_id))

    @app.get("/api/runs/{run_id}/events", dependencies=api)
    def events(run_id: str, after: int = 0):
        return scheduler.store.events(run_id, after)

    @app.get("/api/runs/{run_id}/report", dependencies=api)
    def report(run_id: str):
        job = scheduler.service.get(run_id)
        from .reports import write_run_report
        path = write_run_report(job)
        if path is None:
            raise HTTPException(409, "只有已结束的仓库任务有执行报告；对话和仅安装不生成报告")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/api/runs/{run_id}/cancel", dependencies=api)
    def cancel(run_id: str):
        return public(scheduler.service.cancel(run_id))

    return app


HTML = """<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Agent Base</title><style>
body{font:16px system-ui;margin:40px auto;max-width:1080px;padding:0 20px;background:#f5f7fa;color:#17243a}
article{background:white;padding:20px;border-radius:12px;margin:18px 0}button,input,textarea{font:inherit;padding:8px;margin:4px}
textarea{width:94%;height:110px}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;text-align:left;border-collapse:collapse}
td,th{padding:9px;border-bottom:1px solid #ddd}button{cursor:pointer}small{color:#516078}</style>
<h1>Agent Base</h1><p><a href="/chat">打开 Agent 对话台 →</a></p><p>触发工作，查看执行状态和产物。页面每两秒刷新。</p>
<label>管理 Token（如已配置）<input type="password" id="token" autocomplete="off"></label>
<article><h2>Repo 资源</h2><div id="repos"></div></article>
<article><h2>工作</h2><div id="works"></div><h3>本次参数（JSON）</h3><textarea id="payload">{}</textarea></article>
<article><h2>运行记录</h2><div id="runs"></div></article><article><h2>详情</h2><pre id="detail">选择一条运行记录。</pre></article>
<p id="error" role="alert"></p><script>
const $=id=>document.getElementById(id);let selected=null;
async function api(path,method='GET',body,key){const headers={'Content-Type':'application/json'};
if($('token').value)headers.Authorization='Bearer '+$('token').value;if(key)headers['Idempotency-Key']=key;
const r=await fetch('/api/'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
const value=await r.json();if(!r.ok)throw Error(JSON.stringify(value));return value;}
function button(text,action){const b=document.createElement('button');b.textContent=text;b.onclick=()=>action().catch(showError);return b;}
function showError(e){$('error').textContent=e.message;}
async function refresh(){const [works,runs,repos]=await Promise.all([api('works'),api('runs'),api('repos')]);$('works').replaceChildren();
$('repos').replaceChildren();for(const r of repos){const row=document.createElement('p');
row.textContent=(r.manifest.package?.name||r.repo_id)+' · '+r.namespace+' · MCP '+(r.service_alive?'运行中':'未运行')+' · skills: '+(r.manifest.skills||[]).map(s=>s.name).join(', ');
row.append(button('资源详情',async()=>{$('detail').textContent=JSON.stringify(await api('repos/'+r.id),null,2);selected=null;}));$('repos').append(row);}
for(const w of works){const row=document.createElement('p');row.textContent=w.id+' · '+(w.enabled?'启用':'暂停')+' · '+(w.definition.cron||'手动')+' ';
row.append(button('立即运行',async()=>{const payload=JSON.parse($('payload').value);const t=await api('works/'+w.id+'/trigger','POST',{payload},crypto.randomUUID());
selected=t.run_id;$('detail').textContent=JSON.stringify(t,null,2);await refresh();}));$('works').append(row);}
const table=document.createElement('table');for(const r of runs){const row=document.createElement('tr');
for(const value of [r.work_id||'单次任务',r.id.slice(0,12),r.status,r.session_status||'—']){const td=document.createElement('td');td.textContent=value;row.append(td);}
const td=document.createElement('td');td.append(button('详情',async()=>{selected=r.id;await refresh();}));
if(['queued','preparing','running','verifying'].includes(r.status))td.append(button('取消',async()=>{await api('runs/'+r.id+'/cancel','POST',{});await refresh();}));
row.append(td);table.append(row);}$('runs').replaceChildren(table);
if(selected)$('detail').textContent=JSON.stringify(await api('runs/'+selected),null,2);$('error').textContent='';}
setInterval(()=>refresh().catch(showError),2000);refresh().catch(showError);
</script></html>"""
