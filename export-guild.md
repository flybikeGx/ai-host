# 将本地 Agent 整理为可导入的 Agent Repo

这份文档可以直接交给你的本地编码 Agent，要求它把**你明确指定的一个本地 Agent**整理为独立 Git repo。目标平台是 Agent Base：Python 负责安装、调度和检查，原生 CLI 负责推理、工具循环和上下文。

导出的是可重复安装的能力与工作约定，不是整台电脑的配置、聊天记录或登录状态。默认新建目标目录，保留原 Agent 可继续使用；未经明确要求，不安装全局依赖、不迁移凭据、不推送远程仓库。

## 1. 三份核心文件各管什么

| 文件 | 内容 | 不应包含 |
| --- | --- | --- |
| `AGENTS.md` | 角色、能力范围、长期业务规则、工具使用与路径约定 | 本机绝对路径、聊天历史、模型凭据 |
| `Installation.md` | 依赖、初始化、skills/MCP/tools 清单及产物检查 | 个人登录数据、仅在原机器存在的命令路径 |
| `entrypoint.md` | 无人值守启动后要做什么、输入、步骤、产物、缺输入时如何处理 | 固定最终 JSON 汇报 schema、要求等待人工对话 |

基座在每次 Run 启动时组合 `AGENTS.md + entrypoint.md + 本次参数/触发说明 + 如何汇报结果`。最后一部分由基座提供；不要复制到 AGENTS、skill 或 entrypoint 中维护。正常交互对话只使用角色、资源和用户消息，不自动执行 entrypoint。

新导出的 repo 应有 entrypoint。旧 repo 没有 entrypoint 时，基座仍支持显式 task 或 task.default，但无人值守最终回复同样按启动时注入的报告契约执行。

## 2. 交给本地编码 Agent 的导出指令

将下面这段发给本地 Agent，并告诉它源目录、目标目录和期望的无人值守工作：

> 请按本文件约定，将我指定的本地 Agent 整理成一个独立、可供 Agent Base 导入的 Git repo。
>
> 先盘点指定目录中的角色指令、业务流程、skills、工具、MCP、依赖和认证引用，列出保留、重写、缺失项。不要扫描或搬运整个用户 HOME，不要读取、输出或复制密钥、Cookie、个人认证目录和原生会话。
>
> 在目标目录整理 AGENTS.md、Installation.md 和 entrypoint.md，复制真正需要的 skills/工具/MCP 源码和必要测试数据。把机器专属路径改为相对引用或受支持的清单占位符；可执行逻辑不得只藏在不可移植的个人设置中。
>
> 清单采用 schema_version: 2，逐项声明 skill、MCP 和 tools。认证使用 from_env 引用；发现缺失实现或私有服务时明确列出，不编造可用工具、模型名、地址或登录方式。初始化只安装和准备资源，不执行一次完整业务任务。
>
> entrypoint 应在没有人继续回复的情况下执行一个明确工作；输入缺失时报告 blocked，不盲目重试有副作用的操作。最终报告格式由基座启动时注入，不要在 repo 中自建一套竞争的 JSON 汇报规则。
>
> 为关键交付声明 completion.artifacts 与独立 completion.checks。先完成本地静态检查和可执行验证，给出目标文件清单、所需环境变量名、未解决项和试运行命令。不要仅凭模型说“成功”判定可导入。没有授权时只准备本地仓库，不发布远程。

## 3. 推荐目录

```text
my-agent/
  AGENTS.md
  Installation.md
  entrypoint.md
  README.md
  .gitignore
  pyproject.toml                 有额外 Python 依赖时提供
  uv.lock                        与 pyproject.toml 配套
  skills/
    analysis/
      SKILL.md
      references/                可选，保持相对引用
  tools/
    initialize.py
    write_report.py
    verify.py
  mcp/
    domain_server.py
  examples/
    input/                       无敏感信息的输入示例
```

不预置保留目录 `.agent/`、根 `.agent-base-root` 标记或触发文件 `activation.json`；它们由基座按运行生成。演示数据放 examples，不把上次运行的 artifacts、输入副本、venv、CLI home 一起导出。

## 4. 整理角色和入口

AGENTS.md 示例：

```markdown
# 报表 Agent

你负责把已授权输入整理成业务报表。先读取本 namespace 声明的 skill。
业务计算通过声明的工具完成，不编造工具结果。不要修改输入。
文件路径相对于当前工作区，交付文件写到 artifacts。
只使用当前 repo 声明的资源。
```

entrypoint.md 示例：

```markdown
# 无人值守报表入口

读取 input/data.json；有 activation.json 时读取其 payload 作为本次参数。
缺少必需输入时报告 blocked 和所需字段，不等待用户回复。
读取 analysis skill，调用本 repo 的分析工具，保存 artifacts/report.json。
核对文件实际存在，并概述结果与未完成项。
最终回复遵循基座在启动时注入的汇报契约。
```

把“每次 cron 应执行的固定流程”写进入口，把“什么角色、哪些业务规则长期有效”留在 AGENTS。显式 task 是本次补充说明，不替代已存在的 entrypoint；入口存在时，不再自动附加旧 task.default。

## 5. 资源清单

下面是字段示意，所引用的脚本/skill 必须真实提供；完整可运行例子见 [finance](examples/repo-resources/finance/Installation.md)。

````markdown
# 安装

说明 Python、依赖来源、所需环境变量名和可用外部服务。

```agent-base-install
schema_version: 2
package:
  name: report-agent
  version: 0.1.0
environment:
  lockfile: uv.lock
installation:
  steps:
    - argv: ["${runtime.python}", "${package.root}/tools/initialize.py"]
      timeout_seconds: 60
skills:
  - name: analysis
    path: skills/analysis
mcp:
  servers:
    - id: domain
      transport: stdio
      argv: ["${runtime.python}", "${package.root}/mcp/domain_server.py"]
      environment:
        DOMAIN_TOKEN: {from_env: DOMAIN_TOKEN}
      timeout_seconds: 30
completion:
  artifacts:
    - path: artifacts/report.json
      required: true
      min_bytes: 10
  checks:
    - argv: ["${runtime.python}", "${package.root}/tools/verify.py"]
      timeout_seconds: 20
limits:
  wall_timeout_seconds: 300
```
````

没有额外环境依赖、初始化步骤或凭据时，删掉相应字段。不要为了填模板虚构 uv.lock 或工具。机器配置只选一个来源：Installation.md 中唯一配置块，或 agent.yaml；不要双写。完整字段约定见 [RepoInstallation.md](RepoInstallation.md)。

迁移本地 CLI 的 MCP 设置时，只保留当前 repo 需要且能实际使用的服务器，并改写成基座清单格式。旧 CLI 配置中的 JSON 字段不保证与基座同名。由宿主机原生软件提供、无法带走的 MCP 要标出依赖和验证方法。

## 6. 工具与路径要求

本地工具使用 argv 数组，不提供自由 shell 字符串。Python 工具可使用 `${runtime.python}`，固定脚本使用 `${package.root}`，运行数据使用 `${workspace.root}`。这些占位符只由清单命令展开；AGENTS、entrypoint 和 skill 中直接使用工作区相对路径。

`tools.local` 的 JSON stdio 协议：

```json
{"protocol_version":1,"arguments":{"text":"示例"},"context":{"workspace":"由基座提供","artifact_dir":"由基座提供"}}
```

工具读取 stdin JSON，stdout 只返回一个 JSON 对象，诊断信息写 stderr；用 context 指定的工作区处理输入/产物。不要把工具导入时的目录或作者机器路径当成任务目录。

skills 声明路径必须包含 SKILL.md，关联 Markdown 也使用相对引用。同一安装版本的技能保持固定，任务不会每轮重装；MCP 在安装完成后常驻，同一安装版本的多个实例共享进程。MCP 不能把某次 Run 的 cwd 或可变“当前任务”永久保存后供其他实例使用。

## 7. 检查与交付

在 Agent Base 项目目录执行，以下 repo 路径仅为示例：

```sh
.venv/bin/agent-base lint path-to-exported-repo
.venv/bin/agent-base repo install path-to-exported-repo
.venv/bin/agent-base run path-to-exported-repo --wait
```

Git 导入前要把目标 repo 的必要文件提交为 Git commit。不要把作者机器实际路径粘进 AGENTS/entrypoint/skill；路径扫描也应包含这些文档的引用文件。

验收应核对：安装只初始化一次、skills 和常驻 MCP 跨任务复用、实际工具调用、业务产物与 completion 检查、Run 的 report.json 和 agent_report。更换模型或 CLI 后重新试运行，不能仅凭安装成功宣布完整可用。

交付清单：

- [ ] AGENTS.md、Installation.md、entrypoint.md 各司其职。
- [ ] 所有声明文件、skill、工具和检查脚本真实存在且已提交。
- [ ] 依赖与环境变量**名称**说明清楚，没有真实凭据或原生会话。
- [ ] AGENTS、entrypoint、skill 与其 Markdown 引用没有本机绝对路径。
- [ ] 不携带 .agent、activation.json、运行日志、业务历史与私有输入。
- [ ] 缺输入时可明确报告 blocked，无需等待人工回复。
- [ ] 业务产物有独立检查；模型的自报检查不会冒充 checks_passed。
- [ ] 最终汇报格式由基座注入，repo 不复制维护该 schema。
- [ ] 安装与真实运行已分别验证，或明确列出尚未验证的依赖。

结构化汇报与新旧 repo 的兼容规则见 [Repo 运行契约](RepoContract.md)。
