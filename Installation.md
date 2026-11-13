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

Windows 对应执行 `.venv\Scripts\agent-base.exe`。Windows 的 Python 管理逻辑和 `.cmd` 包装适配已准备；目录链接优先创建符号链接，普通用户没有该权限时回退到 `cmd.exe /d /c mklink /J` 创建的 junction，因此未开启开发者模式也能安装 skills 和挂载工作区。Windows 真机安装、运行、取消和失败清理尚未验收；当前实际验收平台是 macOS，详见 [RepoInstallation.md](docs/RepoInstallation.md)。

使用内部网关或其他兼容端点时，在 `agent-base.toml` 增加 `[providers.<name>]` 并把 `runtime.provider` 指向它；配置只写凭据的环境变量名。字段、示例和失败行为见 [Providers.md](docs/Providers.md) 与 [config/internal-gateway.example.toml](config/internal-gateway.example.toml)。

`doctor` 只检查命令、版本和当前 provider 的凭据是否配置，不发模型请求，也不将“有 key”解释成在线可用。它输出所选 `provider`、`credential_env` 和 `credential_configured`，缺凭据时返回非零。真实初版调用已在开发环境通过；更换账号或模型后仍需自己的短任务验收。

手工准备 Python 环境可用 `uv sync --frozen`。已有本地 dsh 时不必重复 npm 安装。首次安装需要网络，企业环境可使用自己的 uv/npm 镜像配置。

升级已有副本后也执行 `uv sync --frozen`，以安装 FastAPI/croniter 等锁定依赖。启动本地 Web 和 cron 调度用 `.venv/bin/agent-base serve`；仅后台调度用 `.venv/bin/agent-base worker`，二者无需同时启动。配置及已有运行数据保持不变。工作注册和演示步骤见 [Scheduling.md](docs/Scheduling.md)。

仓库自身的初始化支持 `schema_version:2`：在 agent.yaml 或 Installation.md 的 agent-base-install 配置块列出 skills、MCP、依赖和初始化步骤。安装后 MCP 常驻、skills 固定共享，使用 `.venv/bin/agent-base repo install <repo>`。三个可运行示例与安装前审阅流程见 [RepoInstallation.md](docs/RepoInstallation.md)。

网页对话地址为 <http://127.0.0.1:8765/chat>，复用同一 serve 进程和模型凭据。生产前端文件已随项目构建，不需要启动单独 Node Web 服务或访问外部 CDN。只有修改 frontend 源码后才需要在该目录执行 `npm ci` 和 `npm run build`。详细说明见 [WebChat.md](docs/WebChat.md)。

需要带入内网时，参照 [CodeAgent CLI 迁移指南](docs/CODEAGENT_MIGRATION.md) 准备独立配置和目标平台依赖；`--skip-dsh` 本身不会完成引擎切换。推送 Gitee 或使用离线 bundle 见 [Git 交接说明](docs/GIT_GUIDE.md)。
## 网页入口补充

服务启动后，主页 `/` 进入仪表盘，原工作台迁到 `/workbench`；`/knowledge`、`/chat`、`/cron` 均可从统一侧栏进入。首次知识管理连接运行 `.venv/bin/agent-base open`，无需复制或显示 Token。Knowledge MCP 随 serve/worker 幂等启动，数据保存在忽略状态目录。详见 [Knowledge.md](docs/Knowledge.md)。
