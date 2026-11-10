# Agent Base

Python 编写的仓库 agent 基座：与 admin-agent 对话，让它安装、运行你提供的 Git repo。外网使用 DeepSeek Harness CLI 的原生 agent 循环，Python 管理工作目录、安装、并发、状态、取消和交付物。

根 [AGENTS.md](AGENTS.md) 是 admin-agent 的指令，可以直接修改其管理方式。Python 不重新实现模型—工具循环，不调用 OpenAI-compatible 模型 API；模型请求由所选 CLI 负责。

完整说明：[项目设计书与 TODO](DESIGN.md) · [CodeAgent CLI 迁移指南](CODEAGENT_MIGRATION.md) · [Git 双远端与内网交接](GIT_GUIDE.md) · [验收记录](VALIDATION.md)。

Repo 引入：[本地 Agent 导出指南](export-guild.md) · [entrypoint 与结构化结果契约](RepoContract.md)。

搁置设计：[用户隔离草案](USER_ISOLATION_DESIGN.md)（暂不实施，当前仍是单用户实现）。

## Repo 引入与无人值守入口

将本地 Agent 整理为完整 repo，可把 [export-guild.md](export-guild.md) 交给本地编码 Agent。新导出的 repo 提供 AGENTS.md、Installation.md 和根 entrypoint.md：分别定义长期角色、安装资源和无人值守工作流程。

CLI Run、Web 工作触发、cron 和 Admin 提交的后台任务启动时，基座组合 `AGENTS.md + entrypoint.md + 本次参数/触发说明 + 如何汇报结果`。报告要求由基座动态注入，repo 不复制维护。普通网页/Admin 对话不自动执行 entrypoint。

最终回复必须符合结构化 JSON 契约，解析到 `result.agent_report`；基座另写运行目录内的 `report.json`，记录可信状态、独立检查和归档产物。blocked、格式错误或产物缺失不会被判为成功。没有 entrypoint 的旧 repo 仍可使用 task/task.default，但实际 Run 也执行新的最终汇报契约。完整字段与兼容说明见 [RepoContract.md](RepoContract.md)。

## 直接开始

新副本先按 [Installation.md](Installation.md) 执行 `python3 scripts/bootstrap.py`。已有本地 dsh、Python 环境和配置时，在已设置 `DEEPSEEK_API_KEY` 的终端执行：

```sh
.venv/bin/agent-base admin
```

然后直接说：

```text
安装并运行 https://github.com/your-org/report-agent.git，任务是分析 input 里的数据并生成报告。
```

也可以连续说“先只安装这个仓库”“运行刚才安装的任务”“现在有哪几个任务”“取消任务 <run_id>”。admin 使用 MCP 管理工具操作后台任务，保持同名原生会话上下文；退出对话后后台任务继续运行。

新机器安装见 [Installation.md](Installation.md)。本机默认配置为 `agent-base.toml`，运行数据保存在 `.agent-base`；两者和凭据文件都已加入忽略列表。

## 网页对话

从 AGENTS 列表选择 Agent 发起新对话；最近对话默认显示全部，可按 Agent 筛选。独立 [定时页面](SchedulingUI.md) 位于 `/cron`，可图形化选择 Agent、一次性日期或每天/每周计划，使用 entrypoint 并查看结构化报告。

启动 `.venv/bin/agent-base serve` 后打开 <http://127.0.0.1:8765/chat>，可以与 Admin Agent 和所有已安装 repo Agent 多轮对话。支持历史、实时文本与工具卡片、断线恢复、停止回复、模型/推理强度选择以及会话产物下载；repo 沿用固定 skills 和常驻 MCP。

会话由原生 dsh session 续接，页面刷新或切换对话不会取消生成。原调度页面仍在首页。设计参考、接口和测试见 [WebChat.md](WebChat.md)。

## Repo 长期安装与资源命名空间

`schema_version: 2` 支持在 `agent.yaml` 或 `Installation.md` 的固定配置块中列出 skills/MCP：同一安装版本共享固定 skills 与依赖，MCP 安装后常驻，任务结束不会停止。每个运行只发现自己 repo 的资源；提示词使用相对路径，安装时检查 AGENTS.md 与 skill 文档中的绝对路径。

已提供财务、库存、研究三个独立示例以及四实例真实 dsh 验收。安装、常驻服务管理、路径检查与复现方法见 [RepoInstallation.md](RepoInstallation.md)。

## Web / cron 工作调度

现在支持注册可重复触发的工作，通过 Web、cron、CLI 或 admin MCP 入队，由持久化调度器控制并发。每次运行独立 clone、复制输入到临时目录、创建新 session；产物验证通过后归档并清理目录，超时或失败保留现场。

```sh
.venv/bin/agent-base serve --port 8765
```

本机页面：<http://127.0.0.1:8765>。创建测试仓库、注册工作、cron/HTTP 调用和完整生命周期说明见 [Scheduling.md](Scheduling.md)。`serve` 含调度器，无需 Web 时可用 `agent-base worker`。只有注册或触发命令时，需另有常驻调度器负责出队。

## 本地示例

```sh
.venv/bin/python scripts/create_demo.py
.venv/bin/agent-base admin
```

create_demo 会把示例复制为独立的本地 Git 仓库并输出绝对路径，保留已存在目录。在对话中说：

```text
安装并运行刚才输出的本地 Git 仓库，使用默认任务，等待完成并给我产物。
```

示例真正经过 admin MCP → 后台任务 → 独立 clone → dsh → 业务 MCP 工具 → 报告文件 → Python 检查。不会通过管理员直接写文件来冒充仓库 agent 成功。

## 命令行入口

```sh
agent-base doctor
agent-base run <repo-url-or-local-git-path> --task "本次任务" --model <model-id> --effort high
agent-base install <repo-url-or-local-git-path> --wait
agent-base start <prepared-run-id> --task "本次任务"
agent-base list
agent-base status <run-id>
agent-base wait <run-id> --seconds 30
agent-base events <run-id> --after 0
agent-base cancel <run-id>
agent-base admin --session another-chat
agent-base admin --message "列出当前任务"
```

以上假定已激活 `.venv` 或使用完整可执行路径。`--config config-file.toml` 放在子命令前。每个 run 独立检出 Git SHA；运行中不会跟随远端分支移动。

`run` 默认返回后台任务 ID；`--wait` 最多等待 300 秒，仍未结束时返回当前状态。`start` 只接受 prepared 状态，不会重发运行中、失败或已完成任务。仓库多轮交互通过网页对话提供；单次任务 Run 的自动断点续跑和未知结果重试不是首版功能。

## 仓库约定

最小仓库只需根 `AGENTS.md`，并在提交时给出 task。需要自动安装或额外能力时使用 `agent.yaml`，或 `Installation.md` 中唯一的 agent-base-install 配置块。Markdown 正文不会作为自由 shell 脚本执行。下面保留 v1 兼容示例，repo 长期安装使用 v2。

当前可运行的清单示例：

```yaml
schema_version: 1
package:
  name: report-agent
  version: 0.1.0
task:
  default: 生成 artifacts/report.txt
environment:
  lockfile: uv.lock  # 可选，仓库同时提供 pyproject.toml
installation:
  steps:
    - argv: ["${runtime.python}", "${package.root}/setup.py"]
      timeout_seconds: 60
tools:
  local:
    - name: make_report
      description: 按输入生成报告
      argv: ["${runtime.python}", "${package.root}/tools/make_report.py"]
      input_schema:
        type: object
        properties:
          text: {type: string}
        required: [text]
mcp:
  servers:
    - id: knowledge
      transport: stdio
      argv: ["${runtime.python}", "${package.root}/mcp/server.py"]
      environment:
        TOKEN: {from_env: KNOWLEDGE_TOKEN}
skills:
  - name: report-writing
    path: skills/report-writing
completion:
  artifacts:
    - path: artifacts/report.txt
      required: true
      min_bytes: 1
  checks:
    - argv: ["${runtime.python}", "${package.root}/tools/check_report.py"]
limits:
  wall_timeout_seconds: 1800
```

这个片段说明字段，运行时需仓库实际提供声明的资源。不使用的部分直接省略。可直接参考 [可运行示例](examples/hello-agent/agent.yaml)。

工具接收 stdin JSON：`{"protocol_version":1,"arguments":{...},"context":{"workspace":"...","artifact_dir":"..."}}`，stdout 返回一个 JSON 对象，诊断写 stderr。它们通过原生 `mcp__local__<name>` 工具暴露给 dsh。业务工具不继承名称含 API_KEY/TOKEN/SECRET/PASSWORD 的环境变量；需要认证的外部 MCP 使用显式 `from_env` 引用。生成的 patch 保留环境引用，不把凭据值写入文件。

远程 MCP 使用 `transport: streamable_http`、`url` 和 `headers`，header 也可以用 `{from_env: ENV_NAME}`。stdio/HTTP 连接由 dsh 原生 MCP 插件处理；已通过本地业务 MCP 路径验收，远程服务的身份和协议兼容性需按实际服务测试。dsh 可将可选插件启动失败记录为 warning，故安装收据仅证明资源准备完成，不证明所有外部 MCP 已实际加载；运行错误和日志仍需核对。

旧 v1 skills 在启动前读取冻结的 SKILL.md 并附加到任务，`uv.lock` 环境装到 run 专属环境。v2 使用固定安装环境、skill 链接和 read_skill，供同一安装版本的实例共享。两者都不模拟其他 CLI 的 slash 技能发现，也不按需动态安装技能；包源与验证脚本保留冻结副本，工作区修改不会替换验证标准。

## 状态与并发

正常路径：queued → preparing → running → verifying → succeeded。只安装时终态为 prepared。其他终态：failed、cancelled、timed_out、interrupted。

默认最多两个活动任务，SQLite 原子领取，排队 FIFO，每个任务独立进程、repo、环境和 DSH_HOME。安装脚本和仓库任务都支持取消及超时；终止运行器时同时清理其受管进程组。监督进程意外消失会记为 interrupted 并处理已知子进程，不会自动重复未知结果的任务。

产物检查通过后复制到受管 artifacts 目录，记录大小和 SHA-256。没有独立检查命令时标记 `structural_only`；有命令且通过时标记 `checks_passed`，这不等于证明任意业务结论正确。

本版本面向本人/团队维护的可信仓库。独立目录/venv 不能隔离任意恶意 Python 或 MCP 代码，不提供公网多租户服务。任务可能执行声明的安装代码和 CLI 原生命令。

## dsh 与内网 CodeAgent

本地 dsh 固定 npm `@deepseek-ai/dsh@0.2.1-rc.5`，可执行文件在 `.runtime/dsh/node_modules/.bin/dsh`。该发布版的 headless 只有 session/final 文本事件，final 没有主干新增的 reason/sessionId；适配器检查进程退出码 0、opening session 和唯一 final，记录 `legacy_final_exit_code`，不会伪造 finish_reason。

model/effort 通过 invocation patch 的 agent-default-model 传递。权限模式通过该版本的环境和 approval patch 设置，未使用主干才新增的 CLI flags。admin 使用原生 `--session-id` 保持对话。

CodeAgent 在 [engines.py](src/agent_base/engines.py) 保留了基于旧 MVP 的 print-mode 基本调用。可用 [配置模板](config/codeagent.example.toml) 准备内网单次推理探测，并将并发设为 1。**尚未内网验收，也未完成 MCP、Admin 和网页会话适配**；有 MCP/业务工具的清单会明确拒绝。完整迁移需同时调整 service、repo_runtime、admin 和 conversations 的运行时调用边界，步骤见 [迁移指南](CODEAGENT_MIGRATION.md)。

参考资料：[dsh CLI](https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/cli)、[headless](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/bundle/headless)、[MCP 插件](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/mcp/mcp-client)。发布版行为以本地安装及验收为准。

## 测试

```sh
uv run --frozen pytest -q
```

自动测试使用 fake dsh，覆盖任务全流程、Git 固定版本、安装后启动、并发、排队、取消、超时、失败产物、原生会话续接，并通过官方 Python MCP 客户端连接真实管理/业务 MCP server。真实外网 dsh 已另外通过 admin 和示例仓库验收，记录见 [VALIDATION.md](VALIDATION.md)。
