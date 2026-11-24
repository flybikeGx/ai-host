# 安装与启动

## 环境要求

Python 3.11+、uv、Git；默认 dsh 需要 Node.js 24.9+ 和 npm。首次安装需要依赖源网络，企业环境可配置 uv/npm 镜像。

```sh
python3 scripts/bootstrap.py
```

脚本按锁文件安装到项目内 `.venv` 与 `.runtime/dsh`，保留已有 `agent-base.toml`，不全局安装 dsh。已有 dsh 可加 `--skip-dsh`，并在本机配置中设置 `runtime.command`。这不会自动切换为 CodeAgent。

在启动环境配置 `DEEPSEEK_API_KEY`；自定义网关只在配置中引用环境变量名，见 [Provider 指南](docs/Providers.md)。不要把凭据写入仓库。

## 选择入口

```sh
.venv/bin/agent-base doctor
.venv/bin/agent-base admin       # 管理对话
.venv/bin/agent-base serve       # 网页 + 调度器 + 服务监督
# 不需要网页时可单独运行：
.venv/bin/agent-base worker
```

doctor 检查 CLI、版本和凭据是否配置，不验证在线推理。网页地址为 <http://127.0.0.1:8765/>；默认只监听本机。仅注册工作不表示调度器已经运行。

Windows 将命令前缀换为 `.venv\Scripts\agent-base.exe`。目录链接有 junction 回退，但 Windows 安装、取消和清理仍需真机验收。

## 账号与知识管理

首次管理员在本机执行 `agent-base control init`；已分配账号从 `/login` 登录。无账号的本机知识管理连接使用 `agent-base open`，由命令直接打开一次性链接，无需复制凭据。

账号启用后全局工作台需要全局管理权限；普通用户仅能访问账号页。详见 [登录说明](docs/WebChat.md#账号登录与会话)。

## 更新已有副本

```sh
uv sync --frozen
```

需要更新 dsh 时重新执行 bootstrap；配置和运行数据保留。生产前端已随 Python 包提交，普通使用无需启动 Node 服务。修改前端后在 `frontend` 执行 `npm ci`、`npm run build`，并交付源码与构建产物。

## 安装后检查

先确认 doctor 通过，再运行一个短任务核对真实模型、工具和产物；安装成功不能代替业务验收。自己的 Agent 见 [导出指南](docs/export-guild.md)，迁入内网见 [CodeAgent 指南](docs/CODEAGENT_MIGRATION.md)，默认值和限制见 [规格速查](docs/Specifications.md)。
