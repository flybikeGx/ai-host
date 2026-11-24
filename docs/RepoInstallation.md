# Repo 安装、固定 skills 与常驻 MCP

`schema_version: 2` 将资源从 Run 提升到 repo 安装版本。skills、依赖环境和 MCP 程序安装一次；MCP 在安装成功时就已启动，没有任务时也继续运行。Run 只连接自己的命名空间，结束或超时不停止共享服务。当前采用资源发现与提示词约束，允许感知其他目录，不引入容器强隔离。

`schema_version: 3` 进一步把**共享 MCP 服务**与 Agent 安装分离：服务包按 `kind: mcp-service` 独立安装/发布/绑定，Agent 用 `services` 声明依赖。v1/v2 的 repo 私有 MCP 语义保持不变；需要跨 Agent 复用时使用 v3，详见 [共享 MCP 托管与看护](ManagedServices.md)。

## 三个可以直接运行的 repo

| 示例 | skill 名称 | MCP 工具名 | 对 [2, 3, 5] 的结果 |
| --- | --- | --- | --- |
| finance | analysis | domain__analyze | 求和：10 |
| inventory | analysis | domain__analyze | 求积：30 |
| research | analysis | domain__analyze | 求平均：约 3.3333 |

每个 [示例目录](../examples/repo-resources) 都包含独立 AGENTS.md、Installation.md、skill、MCP 程序、初始化脚本、报告工具和验证脚本。相同的资源名称用于验证命名空间不会串用。

```sh
.venv/bin/python scripts/create_resource_demos.py
.venv/bin/agent-base repo install .demo/resources/finance
.venv/bin/agent-base repo install .demo/resources/inventory
.venv/bin/agent-base repo install .demo/resources/research
.venv/bin/agent-base repo list
```

创建脚本生成三个独立本地 Git repo，已有目录保留。安装命令会输出 installation_id、namespace、固定资源位置和服务 PID。演示使用基座解释器中的 MCP SDK；实际 repo 可声明 uv.lock 安装自己的依赖。

```sh
.venv/bin/agent-base run .demo/resources/finance --wait
.venv/bin/agent-base run .demo/resources/finance --wait
```

两次运行绑定同一安装版本、skill 文件和常驻 MCP。v2 的首次 `run/install` 也能自动安装资源；建议先显式 `repo install`，便于提前检查并复用资源。已有可用安装时，正常 run 采用已安装版本，不自动跟随远端更新；更新源码后再次 `repo install` 才准备新版本。

## Installation 配置

机器配置允许二选一：

- 根 `agent.yaml`。
- 根 `Installation.md` 或 `installation.md` 中唯一的 `agent-base-install` 代码块。

两处不能同时放机器配置，Markdown 正文用于说明资源和依赖，不作为自由 shell 安装脚本。推荐参照 [finance/Installation.md](../examples/repo-resources/finance/Installation.md)。

````markdown
# 示例初始化

提供 analysis skill、domain MCP；安装完成后 MCP 保持运行。

```agent-base-install
schema_version: 2
package:
  name: example-agent
skills:
  - name: analysis
    path: skills/analysis
mcp:
  servers:
    - id: domain
      transport: stdio
      argv: ["${runtime.python}", "${package.root}/mcp/server.py"]
      timeout_seconds: 30
```
````

延续现有 task、environment、installation.steps、tools.local、completion 和 limits 格式。`installation.steps` 在固定安装目录执行一次；脚本使用安装目录作为 cwd。安装器不扫描文档文本；路径规范由 Admin 在安装前语义审阅。安装失败不会成为可复用的 ready 版本。

只加载声明的 skills 和 MCP。安装依赖、命令与接入层内部可以使用基座解析出的实际路径；仓库的 AGENTS.md 和 skill 文档使用相对路径。

新 repo 还应提供根 entrypoint.md，定义无人值守工作。每次 Run 由基座注入 AGENTS + entrypoint + 本次参数 + 统一汇报契约；报告格式不放入安装清单。入口和技能一起冻结，判断其是否依赖本机绝对路径同样在安装前由 Admin 审阅，普通对话不自动执行入口。旧 task.default 的回退与结构化最终回复见 [RepoContract.md](RepoContract.md)，本地能力导出见 [export-guild.md](export-guild.md)。

## v3：Agent 与共享 MCP 分离

同一 repo 可以同时提交 Agent 和 MCP 组件；它们按组件相对路径分别审阅、安装和发布。

**服务包**（`kind: mcp-service`）声明安装步骤、启动方式、数据目录、工具契约和探针。Admin 的调用顺序是
`prepare_service_install` → `apply_service_install` → `publish_service`；安装会产生不可变 revision 和独立部署，
`${service.data}` 是部署的持久数据目录。只有验证达到 `protocol_verified` / `business_verified` 才能发布，
发布也不会自动授权任何 Agent。

**Agent** 用显式服务声明代替 repo 私有 `mcp.servers`：

```yaml
schema_version: 3
kind: agent
package: {name: triage-agent, version: 0.1.0}
task: {default: 使用 tickets 服务完成分诊。}
services:
  - alias: tickets
    service: ticket-center
    contract: ticket-center/v1
    permissions: [ticket_search]
    required: true
```

实际授权来自平台绑定（`bind_agent_service`），不是清单里的 `permissions`。同一 Agent 对同一服务 ID 的多个部署要使用不同 alias。
运行时的 dsh 连接和 `.agent/resources.json` 来自同一份解析结果；撤权后新调用立即失败。任务所需服务缺失时在产生副作用前报错，
可选服务不可用时提示本轮不要编造结果。

可运行示例：

- [ticket-center-stdio](../examples/managed-services/ticket-center-stdio/Installation.md)（共享 stdio）
- [ticket-center-http](../examples/managed-services/ticket-center-http/Installation.md)（原生 HTTP + 私有端口）
- [triage-agent](../examples/managed-services/agents/triage-agent/agent.yaml)、[ops-agent](../examples/managed-services/agents/ops-agent/agent.yaml)（显式绑定）

统一本地验收：

```sh
UV_CACHE_DIR=/tmp/agent-base-uv-cache uv run --frozen python scripts/validate_managed_services.py
```

服务状态、故障单、绑定和管理页见 [ManagedServices.md](ManagedServices.md)。

## 资源复用

同一安装版本共享源码、环境、skills 和常驻 MCP；不同 commit 可共存。每次 Run 有独立工作区，通过所属 namespace 使用资源。任务结束不停止服务；使用 repo start/stop/restart 显式管理。

共享 stdio 调用串行化，HTTP 服务生命周期归远端系统。平台只转发 MCP tools；不要假设 resources、prompts 或 sampling 已提供。目录链接清理不应删除共享目标；Windows junction 仍需原生验收。

## 故障、超时与恢复

MCP 网关独立于任务监督进程，不属于 CLI 的进程组。Run 超时或取消会结束本次 CLI；本地业务工具检查该 Run 的取消状态和 deadline。共享上游调用的时限取工具超时和 Run 剩余时限中的较小值。

单次业务调用失败或超时不会直接重启共享服务。工具错误返回真实失败，超时标记 `timeout_outcome_unknown`，不自动重放业务操作。远端取消尽力执行，不能据此证明副作用已经撤销。

网关每五秒对空闲上游连接执行 ping；繁忙连接不探测，以免把正常长任务误判为故障。`serve/worker` 每十五秒检查服务状态，对异常安装退避恢复，连续失败三次后停止自动尝试。手动 start 可重置恢复计数。没有常驻调度器时，服务仍运行，但故障后需下一次使用或手动 start 才会恢复。

```sh
agent-base repo status <installation-id>
agent-base repo stop <installation-id>
agent-base repo start <installation-id>
agent-base repo restart <installation-id>
```

显式 stop 会影响共享该安装的所有实例，并停用自动恢复。重启服务只恢复连接，不重放中断的工具调用。升级基座代码后，已有服务仍使用启动时的代码，必要时显式 restart。

## Web 与 admin

`agent-base serve` 页面增加 Repo 资源列表，显示命名空间、skill 名称和 MCP 进程状态。已注册的工作仍从“立即运行”触发。

API：

- `GET /api/repos`、`GET /api/repos/{installation_id}`：资源清单和健康状态。
- `POST /api/repos/install`：body 为 `{"origin":"...","ref":"..."}`，ref 可省略。
- `POST /api/repos/{installation_id}/start` 或 `stop`：管理服务。

admin MCP 对应 install_repo_resources、list_repo_resources、get_repo_resources、start_repo_resources、stop_repo_resources。普通 repo 实例不挂载这些管理工具。

## 安装前审阅

安装文档是否依赖本机绝对路径由 Admin Agent 语义审阅，不再由安装器正则扫描。Admin 在安装或更新仓库前：

1. `prepare_repo_review` 准备候选：clone 仓库并把 ref 固定为 commit，返回候选 ID、commit 和 AGENTS.md、Installation.md/installation.md、entrypoint.md 的存在状态；不执行安装脚本、不启动业务 MCP、不触发任务，临时检出超过 24 小时自动清理。
2. `read_repo_review_doc` 读取上述文档（单文件不超过 128 KiB），结合上下文判断是否依赖本机绝对路径；URL、业务文本或仅用于说明的例子不算错误，缺少可选文件时如实说明。
3. 发现问题时向用户报告文件、位置、原因和建议的相对路径写法，不擅自改写用户仓库；审阅通过后用带 candidate_id 的 `install_repo_resources` 安装同一 commit，避免审阅后分支移动安装到另一个版本。
4. 安装步骤生成或修改文档时，可用 installation_id 复核安装后的同一版本文件，返回 phase=installed 区分阶段。

该审阅是语义判断而不是确定性检查；直接 CLI/API 安装不经审阅也不会被阻断。真实 CLI 自身生成的环境描述不在审阅范围内。

## 试运行

先只安装并核对资源，再提交短任务，核对实际工具调用、产物和 verification。示例创建脚本保留已有目录，不会自动更新旧 demo。历史证据见 [验收记录](VALIDATION.md)，默认限制见 [规格速查](Specifications.md)。
