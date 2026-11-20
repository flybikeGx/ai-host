"""Isolated browser-test backend. Uses fake CLI; never calls a model."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uvicorn

from agent_base.config import Config
from agent_base.repositories import Repositories
from agent_base.web import create_app

project = Path(__file__).resolve().parents[1]
root = project / ".agent-base/ui-fixture"
root.mkdir(parents=True, exist_ok=True)
# A previous fixture whose web server was force-killed can leave detached
# gateways/repo servers behind. Remove sessions and their recorded processes
# before creating the new isolated one.
import psutil


def cleanup_sessions(base: Path):
    for session in base.iterdir():
        if not session.is_dir():
            continue
        for process in psutil.process_iter(["pid", "cmdline"]):
            if process.pid == os.getpid():
                continue
            try:
                command = process.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if any(str(session) in argument for argument in command):
                try:
                    process.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        shutil.rmtree(session, ignore_errors=True)


cleanup_sessions(root)
root = Path(tempfile.mkdtemp(prefix="session-", dir=root))
file = root / "config.toml"
file.write_text('[storage]\nroot="data"\n[queue]\nmax_concurrent=2\n[runtime]\nengine="dsh"\ncommand='
    + json.dumps([sys.executable, str(project / "tests/fixtures/fake_dsh.py")])
    + '\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=20\n')
config = Config.load(file)
repositories = Repositories(config)
try:
    repo = root / "finance"
    shutil.copytree(project / "examples/repo-resources/finance", repo, ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "UI fixture"], check=True)
    repositories.install(str(repo))
    # One isolated managed MCP deployment so the shared-services page (and its
    # state/action wiring) has a real local service to display.
    os.environ.setdefault("TICKET_SERVICE_AUTH", "ui-fixture-ticket-secret")
    service_repo = root / "ticket-center-stdio"
    shutil.copytree(project / "examples/managed-services/ticket-center-stdio", service_repo,
                    ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "--quiet", str(service_repo)], check=True)
    subprocess.run(["git", "-C", str(service_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(service_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "UI service fixture"], check=True)
    from agent_base.review import Reviews
    from agent_base.service_install import ManagedServices
    from agent_base.service_catalog import ServiceCatalog
    from agent_base.service_store import ServiceStore
    managed = ManagedServices(config)
    candidate = Reviews(config).prepare(str(service_repo), None)
    plan = managed.plan_install(candidate, deployment_id="ticket-ui", owner="UI fixture")
    operation = managed.apply_install(plan, background=False)
    assert operation["status"] == "succeeded", operation
    ServiceCatalog(config).publish("ticket-ui")
    stores = ServiceStore(config.data)
    ui_agent = stores.ensure_agent_identity(source="repo:" + "u" * 16, name="ui-triage-agent")
    ServiceCatalog(config).bind(agent_id=ui_agent["id"], deployment_id="ticket-ui", alias="tickets",
                                tools=["ticket_search"])
    app = create_app(config)
    @app.post('/fixture/auth/setup')
    def setup_browser_auth():
        gov = app.state.governance
        gov.bootstrap(admin_password='fixture-browser-password')
        if gov.store.get_user_by_username('member') is None:
            gov.create_user('member', password='fixture-member-password', display_name='测试成员')
        return {'ready': True}

    @app.post('/fixture/auth/reset')
    def reset_browser_auth():
        # Only this isolated browser fixture owns these accounts and sessions.
        with app.state.governance.store.connection() as conn:
            conn.execute('DELETE FROM browser_sessions')
            conn.execute('DELETE FROM user_roles')
            conn.execute('DELETE FROM user_permissions')
            conn.execute('DELETE FROM user_domains')
            conn.execute('DELETE FROM users')
        return {'reset': True}

    @app.post('/fixture/auth/expire')
    def expire_browser_auth():
        with app.state.governance.store.connection() as conn:
            conn.execute('UPDATE browser_sessions SET expires=?', (time.time()-1,))
        return {'expired': True}

    @app.post('/fixture/auth/shared-token')
    def shared_token(body: dict | None = None):
        # Test-only switch for the hybrid `AGENT_BASE_WEB_TOKEN` bootstrap entry.
        # This fixture never receives the real environment token.
        app.state.identity_resolver.shared_token = (body or {}).get('token') or None
        return {'shared_token': bool(app.state.identity_resolver.shared_token)}
    from agent_base.knowledge import Knowledge
    from fastapi.responses import JSONResponse
    knowledge = Knowledge(config)
    project = knowledge.save_namespace({'slug':'kb-ui-demo','name':'知识演示项目','description':'浏览器隔离验收'})
    knowledge.bind('admin', {'primary_namespace_id':project['id'],'read_namespace_ids':[project['id']],'submit_namespace_ids':[project['id']]})
    proposal = knowledge.submit(knowledge.binding('admin'), project['id'], 'create',
        {'title':'待审批日切规则','summary':'Agent 贡献的规则','body_markdown':'# 日切\n每日北京时间零点结算。','tags':['规则']}, '从演示流程核对', 'ui-review')
    @app.post('/fixture/knowledge-login')
    def knowledge_login():
        response = JSONResponse({'namespace':project['id'],'submission':proposal['id']})
        response.set_cookie('kb_admin', knowledge.issue('admin',{'actor':'local-admin'},3600),httponly=True,samesite='strict',path='/api/knowledge')
        return response
    @app.post("/fixture/fire/{name}")
    def fire_fixture_schedule(name: str):
        # Deterministic clock hook only in this fake-CLI test server, never in production.
        app.state.scheduler.work(name)
        with app.state.scheduler.store.connection() as conn:
            conn.execute("UPDATE works SET next_due=? WHERE id=?", (time.time()-1, name))
        app.state.scheduler.cron_tick()
        return {"dispatched": app.state.scheduler.dispatch()}
    uvicorn.run(app, host="127.0.0.1", port=8879, log_level="warning")
finally:
    from agent_base.knowledge_service import KnowledgeService
    KnowledgeService(config).stop()
    try:
        from agent_base.service_install import ManagedServices
        ManagedServices(config).stop("ticket-ui", reason="ui-fixture-teardown", drain=False)
    except (OSError, ValueError):
        pass
    for item in repositories.list():
        repositories.stop(item["id"])
