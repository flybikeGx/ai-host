# 安装与启动

需要 Python 3.11+、uv、Git、Node.js 24.9+ 和 npm。安装仅写入本项目的 `.venv`、`.runtime`，不会全局安装 dsh。

```sh
python3 scripts/bootstrap.py
```

bootstrap 按 `uv.lock` 和 `runtime/package-lock.json` 安装，dsh 固定为 `0.2.1-rc.5`。已有配置会保留。若 dsh 已单独安装，可运行 `python3 scripts/bootstrap.py --skip-dsh`，并在 `agent-base.toml` 里将 `runtime.command` 指向其 CLI。

外网模型凭据使用环境变量 `DEEPSEEK_API_KEY`，不要写入清单、AGENTS 或提交到 Git。先在自己的终端配置，再启动；已有环境凭据会直接复用。

```sh
.venv/bin/agent-base doctor
.venv/bin/agent-base admin
```

Windows 对应执行 `.venv\Scripts\agent-base.exe`。Windows 的 Python 管理逻辑和 `.cmd` 包装适配已准备，但尚未经过 Windows 真机验收；当前实际验收平台是 macOS。

`doctor` 只检查命令、版本和凭据是否配置，不发模型请求，也不将“有 key”解释成在线可用。真实初版调用已在开发环境通过；更换账号或模型后仍需自己的短任务验收。

手工准备 Python 环境可用 `uv sync --frozen`。已有本地 dsh 时不必重复 npm 安装。首次安装需要网络，企业环境可使用自己的 uv/npm 镜像配置。

升级已有副本后也执行 `uv sync --frozen`，以安装 FastAPI/croniter 等锁定依赖。启动本地 Web 和 cron 调度用 `.venv/bin/agent-base serve`；仅后台调度用 `.venv/bin/agent-base worker`，二者无需同时启动。配置及已有运行数据保持不变。工作注册和演示步骤见 [Scheduling.md](Scheduling.md)。

仓库自身的初始化支持 `schema_version:2`：在 agent.yaml 或 Installation.md 的 agent-base-install 配置块列出 skills、MCP、依赖和初始化步骤。安装后 MCP 常驻、skills 固定共享，使用 `.venv/bin/agent-base repo install <repo>`。三个可运行示例与路径检查见 [RepoInstallation.md](RepoInstallation.md)。

网页对话地址为 <http://127.0.0.1:8765/chat>，复用同一 serve 进程和模型凭据。生产前端文件已随项目构建，不需要启动单独 Node Web 服务或访问外部 CDN。只有修改 frontend 源码后才需要在该目录执行 `npm ci` 和 `npm run build`。详细说明见 [WebChat.md](WebChat.md)。

需要带入内网时，参照 [CodeAgent CLI 迁移指南](CODEAGENT_MIGRATION.md) 准备独立配置和目标平台依赖；`--skip-dsh` 本身不会完成引擎切换。推送 Gitee 或使用离线 bundle 见 [Git 交接说明](GIT_GUIDE.md)。
