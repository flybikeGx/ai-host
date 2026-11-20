"""Local admin HTTP API and a small UI; the same queue as CLI/cron."""
from __future__ import annotations

import os
import secrets
import threading
import asyncio
import json
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
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


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    title: str | None = Field(default=None, max_length=100)
    frequency: Literal["once", "daily", "weekly"] = "once"
    date: str | None = None
    time: str
    timezone: str = "Asia/Shanghai"
    weekdays: list[StrictInt] = Field(default_factory=list)
    task: str | None = Field(default=None, max_length=65536)
    model: str | None = None
    effort: str | None = None
    timeout_seconds: float = Field(default=1800, gt=0, le=86400)


def create_app(config, *, background=True):
    scheduler = Scheduler(config)
    from .repositories import Repositories
    repositories = Repositories(config)
    from .conversations import (Conversations, ConversationBusy, ChatError, ChatForbidden,
                                ChatInvalid, ChatQuota, StreamLimiter)
    conversations = Conversations(config)
    from .identity import Identity, get_page_identity
    from .schedules import Schedules
    schedules = Schedules(scheduler)
    from .dashboard import Dashboard
    dashboard = Dashboard(config, scheduler)
    from .knowledge_service import KnowledgeService
    knowledge_service = KnowledgeService(config)
    token = os.environ.get("AGENT_BASE_WEB_TOKEN")

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=scheduler.run, name="agent-scheduler", daemon=True)
        dashboard.scheduler_thread = thread if background else None
        if background:
            try:
                await asyncio.to_thread(knowledge_service.ensure)
            except ValueError:
                import logging
                logging.exception('Knowledge MCP unavailable; management UI remains accessible')
            thread.start()
            dashboard.start()
        yield
        scheduler.stop_event.set()
        dashboard.stop()
        if background:
            thread.join(timeout=5)
        # Detached run supervisors continue; restarting HTTP does not replay them.

    app = FastAPI(title="Agent Base", lifespan=lifespan)
    app.state.chat_streams = StreamLimiter(config.per_user_max_streams)
    app.state.scheduler = scheduler
    app.state.dashboard = dashboard
    from .knowledge_web import register as register_knowledge
    register_knowledge(app, config)
    from .governance.web import register as register_governance
    register_governance(app, config)
    from .chat_migration import migrate_chat
    migrate_chat(repositories.store, app.state.governance.store)
    from .web_auth import register as register_browser_auth
    register_browser_auth(app)
    from .identity import register as register_identity
    register_identity(app, config)
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
        return JSONResponse({"detail": str(error), "code": "conversation_busy"}, status_code=409)

    @app.exception_handler(ChatError)
    async def chat_error(request, error):
        return JSONResponse({"detail": error.detail, "code": error.code}, status_code=error.status)

    def chat_scope(identity: Identity, scope: str) -> bool:
        if scope not in {"own", "all"}:
            raise ChatInvalid("scope 只能是 own 或 all", code="invalid_scope")
        if scope == "all" and not (identity.platform_admin or identity.is_bootstrap):
            raise ChatForbidden("只有平台管理员可以使用只读范围", code="scope_denied")
        return scope == "all"

    def require_chat(identity: Identity, permission: str) -> None:
        if identity.user_type == "service":
            raise ChatForbidden("服务账号不能使用页面对话接口", code="service_account_forbidden")
        if not identity.can(permission):
            raise ChatForbidden(f"缺少权限：{permission}", code="permission_denied")

    def reject_write_scope(scope: str | None) -> None:
        # §5.1: write routes never accept scope; scope=all is a read-only bypass.
        if scope is None:
            return
        if scope == "all":
            raise ChatForbidden("写操作不接受只读范围", code="scope_denied")
        raise ChatInvalid("写操作不接受 scope 参数", code="invalid_scope")

    def default_domain_id():
        from .chat_migration import default_domain
        with app.state.governance.store.connection() as conn:
            row = default_domain(conn)
        return row[0] if row else None

    def scope_all_audit(identity: Identity, path: str, *, conversation_id=None, count=None, domain_id=None):
        # §10: every platform-admin scope=all read leaves one attributed audit row.
        app.state.governance.store.audit(
            domain_id=domain_id, user_id=identity.user_id, trace_id=None, level="chat",
            action="chat.scope_all",
            detail={"path": path, "conversation_id": conversation_id, "count": count, "mode": identity.mode})

    def authorize(request: Request, authorization: str | None = Header(default=None)):
        if app.state.identity_resolver.mode == "jwt" and token and secrets.compare_digest(authorization or "", "Bearer " + token):
            from .identity import IdentityError
            raise IdentityError(401, "token_invalid", "不接受共享令牌")
        if app.state.identity_resolver.mode == "hybrid" and token and secrets.compare_digest(authorization or "", "Bearer " + token):
            return  # Existing CLI/API management credentials remain supported.
        browser = app.state.browser_auth
        if browser.required() or request.cookies.get("agent_base_session"):
            browser.require_workspace(request)
        elif token or app.state.identity_resolver.mode == "jwt":
            raise HTTPException(401, "需要登录或 Bearer token")

    api = [Depends(authorize)]
    from .service_web import register as register_services
    register_services(app, config, api)
    from .mcp_discovery import register as register_mcp_discovery
    register_mcp_discovery(app, config, api)
    static = Path(__file__).parent / "static" / "chat"
    if (static / "assets").is_dir():
        app.mount("/chat/assets", StaticFiles(directory=static / "assets"), name="chat-assets")

    @app.get("/chat", response_class=HTMLResponse)
    @app.get("/cron", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/knowledge", response_class=HTMLResponse)
    @app.get("/services", response_class=HTMLResponse)
    @app.get("/login", response_class=HTMLResponse)
    def chat_page():
        if not (static / "index.html").is_file():
            raise HTTPException(503, "请先在 frontend 目录执行 npm ci && npm run build")
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/chat/targets")
    def chat_targets(identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "agent.read")
        return conversations.targets(identity, default_domain_id=default_domain_id())

    @app.get("/api/dashboard", dependencies=api)
    def dashboard_snapshot():
        return dashboard.snapshot()

    @app.get("/api/conversations")
    def chat_list(archived: bool = False, domain_id: str | None = None, scope: str = "own",
                  identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "task.read")
        scope_all = chat_scope(identity, scope)
        if domain_id is not None and domain_id not in identity.domains:
            raise ChatForbidden("域不可用或未加入", code="domain_forbidden")
        result = conversations.list(archived=archived, identity=identity, domain_id=domain_id,
                                    scope_all=scope_all, default_domain_id=default_domain_id())
        if scope_all:
            scope_all_audit(identity, "/api/conversations", count=len(result), domain_id=domain_id)
        return result

    @app.post("/api/conversations", status_code=201)
    def chat_create(body: ChatInput, scope: str | None = None,
                    identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "agent.run")
        reject_write_scope(scope)
        return conversations.create(identity=identity, default_domain_id=default_domain_id(), **body.model_dump())

    @app.get("/api/conversations/{chat_id}")
    def chat_get(chat_id: str, scope: str = "own", identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "task.read")
        scope_all = chat_scope(identity, scope)
        result = conversations.snapshot(chat_id, identity, scope_all=scope_all)
        if scope_all:
            scope_all_audit(identity, "/api/conversations/{id}", conversation_id=chat_id,
                            domain_id=result["conversation"].get("domain_id"))
        return result

    @app.get("/api/conversations/{chat_id}/files")
    def chat_files(chat_id: str, scope: str = "own", identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "file.read")
        scope_all = chat_scope(identity, scope)
        row = conversations.owned(chat_id, identity, scope_all=scope_all)
        if scope_all:
            scope_all_audit(identity, "/api/conversations/{id}/files", conversation_id=chat_id,
                            domain_id=row.get("domain_id"))
        return conversations.files(chat_id, identity, scope_all=scope_all)

    @app.get("/api/conversations/{chat_id}/download")
    def chat_download(chat_id: str, path: str, scope: str = "own",
                      identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "file.read")
        scope_all = chat_scope(identity, scope)
        row = conversations.owned(chat_id, identity, scope_all=scope_all)
        if scope_all:
            scope_all_audit(identity, "/api/conversations/{id}/download", conversation_id=chat_id,
                            domain_id=row.get("domain_id"))
        file = conversations.artifact(chat_id, path, identity, scope_all=scope_all)
        return FileResponse(file, filename=file.name, media_type="application/octet-stream")

    @app.patch("/api/conversations/{chat_id}")
    def chat_update(chat_id: str, body: ChatUpdate, scope: str | None = None,
                    identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "task.write")
        reject_write_scope(scope)
        return conversations.rename(chat_id, identity=identity, **body.model_dump(exclude_unset=True))

    @app.post("/api/conversations/{chat_id}/messages", status_code=202)
    def chat_send(chat_id: str, body: ChatMessage, scope: str | None = None,
                  identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "task.write")
        reject_write_scope(scope)
        return conversations.send(chat_id, body.text, body.client_key, identity=identity)

    @app.post("/api/conversations/{chat_id}/stop")
    def chat_stop(chat_id: str, scope: str | None = None,
                  identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "agent.cancel")
        reject_write_scope(scope)
        return conversations.cancel(chat_id, identity=identity)

    @app.get("/api/conversations/{chat_id}/events")
    async def chat_events(chat_id: str, request: Request, after: int = 0, once: bool = False,
                          scope: str = "own", identity: Identity = Depends(get_page_identity)):
        require_chat(identity, "task.read")
        scope_all = chat_scope(identity, scope)
        row = conversations.owned(chat_id, identity, scope_all=scope_all)
        if scope_all:
            scope_all_audit(identity, "/api/conversations/{id}/events", conversation_id=chat_id,
                            domain_id=row.get("domain_id"))
        try:
            cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "事件游标无效")
        stream_key = identity.user_id or "anonymous"
        if not app.state.chat_streams.acquire(stream_key):
            raise ChatQuota("同时打开的对话流已达上限", code="user_stream_quota")

        def revoked() -> bool:
            # §6.3: once the stream is open, a disabled user / removed domain /
            # revoked permission / scope loss must end it within 30 seconds.
            try:
                fresh = app.state.identity_resolver.resolve_page(request)
            except Exception:
                return True
            return (fresh is None or fresh.user_id != identity.user_id
                    or fresh.current_domain != identity.current_domain
                    or not fresh.can("task.read")
                    or (scope_all and not (fresh.platform_admin or fresh.is_bootstrap)))

        async def stream():
            nonlocal cursor
            try:
                idle = 0
                checked = time.monotonic()
                while not await request.is_disconnected():
                    if time.monotonic() - checked >= 2:
                        checked = time.monotonic()
                        if await asyncio.to_thread(revoked):
                            yield 'event: error\ndata: {"code": "unauthorized"}\n\n'
                            return
                    batch = await asyncio.to_thread(conversations.events, chat_id, cursor, identity, scope_all=scope_all)
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
            finally:
                app.state.chat_streams.release(stream_key)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/repos", dependencies=api)
    def repos():
        return repositories.list()

    @app.post("/api/repos/install", dependencies=api, status_code=201)
    def install_repo(body: RepoInput, request: Request):
        from .chat_migration import default_domain
        # Keep existing repos authorization; resolve page identity only when
        # supplied. CLI/shared-token callers retain the default-domain fallback.
        domain_id = None
        if request.cookies.get("agent_base_session") or (
            request.headers.get("authorization") and
            request.headers.get("authorization") != "Bearer " + (token or "")
        ):
            identity = app.state.identity_resolver.resolve(request, page=True)
            domain_id = identity.current_domain
        if domain_id is None:
            with app.state.governance.store.connection() as conn:
                domain = default_domain(conn)
                domain_id = domain[0] if domain else None
        return repositories.install(body.origin, body.ref, domain_id=domain_id)

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

    @app.get("/")
    def home():
        return RedirectResponse('/dashboard', status_code=307)

    @app.get("/workbench", response_class=HTMLResponse)
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

    @app.get("/api/schedules/catalog", dependencies=api)
    def schedule_catalog():
        return schedules.catalog()

    @app.get("/api/schedules", dependencies=api)
    def schedule_list():
        return schedules.list()

    @app.post("/api/schedules/preview", dependencies=api)
    def schedule_preview(body: ScheduleInput):
        return schedules.preview(body.model_dump())

    @app.post("/api/schedules", dependencies=api, status_code=201)
    def schedule_create(body: ScheduleInput):
        return schedules.create(body.model_dump())

    @app.post("/api/schedules/{name}/pause", dependencies=api)
    def schedule_pause(name: str):
        return schedules.enable(name, False)

    @app.post("/api/schedules/{name}/resume", dependencies=api)
    def schedule_resume(name: str):
        return schedules.enable(name, True)

    @app.get("/api/schedules/{name}/runs", dependencies=api)
    def schedule_runs(name: str):
        return schedules.runs(name)

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
<h1>Agent Base</h1><p><a href="/dashboard">返回主页</a> · <a href="/chat">Agent 对话</a> · <a href="/knowledge">知识库</a> · <a href="/cron">定时任务</a></p><p>触发工作，查看执行状态和产物。页面每两秒刷新。</p>
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
