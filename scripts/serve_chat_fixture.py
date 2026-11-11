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
    app = create_app(config)
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
    for item in repositories.list():
        repositories.stop(item["id"])
