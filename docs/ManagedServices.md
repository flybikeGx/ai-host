# 共享 MCP：安装、发布、绑定与看护

第二阶段把 MCP 从「某个 Agent repo 的私有常驻进程」提升为平台管理的共享服务：一份部署可被多个 Agent 显式绑定，Agent 升级、停止或卸载不会停止它。Admin 通过管理 MCP 完成准备、安装、验证、发布和绑定；确定性监督器持续探测并按预算恢复；看护 Agent 通过受限工具诊断和修复；管理页集中展示状态、故障和资源。

本页描述**已实现并本地验收**的行为。设计背景、目标与非目标见 [PHASE_2_MANAGED_MCP_DESIGN.md](PHASE_2_MANAGED_MCP_DESIGN.md)。

## 1. 边界

- 本阶段的「发布」指登记到本机服务目录并授权给指定 Agent，不推送 Git、不默认开放公网。
- 托管对象是 MCP 服务包（`kind: mcp-service`）或用户维护的外部 HTTP MCP。平台不接管外部机器的进程。
- 不实现下载已发布 Agent、临时本地运行目录、dsh GUI 自动连接、集群高可用和多租户身份平台。
- 目录、绑定、部署数据仍在本机 `.agent-base` 下；不是安全沙箱，适用于可信团队的单机环境。

## 2. 从本机调试到共享使用

1. 开发者在本地用任意 MCP 客户端调试服务，满意后把源码、`Installation.md`（v3 配置块）和运维说明提交到 Git。
2. Admin 审阅固定 commit：`prepare_repo_review` / `read_repo_review_doc`（MCP-only 组件不要求 AGENTS.md）。
3. `prepare_service_install` 生成计划；`apply_service_install` 安装、私有启动并完成真实 MCP 验证；`publish_service` 登记目录；`bind_agent_service` 授权 Agent。
4. Agent repo 用 `schema_version: 3` 的 `services` 显式声明依赖；运行时由统一解析器生成 dsh 连接和 `.agent/resources.json`。
5. `serve` / `worker` 中的监督器按部署探测和恢复；看护 Agent 巡检并执行策略内动作；`/services` 管理页展示全部状态。

同一 repo 可以同时包含 Agent 和 MCP 源码，但按组件路径分别审阅、安装和发布。Agent 更新不会隐式发布 MCP。

## 3. 清单

### 3.1 服务包 `kind: mcp-service`

机器配置来自根 `agent.yaml` 或 `Installation.md`/`installation.md` 中唯一的 `agent-base-install` 配置块。完整可运行示例见
[ticket-center-stdio](../examples/managed-services/ticket-center-stdio/Installation.md) 和
[ticket-center-http](../examples/managed-services/ticket-center-http/Installation.md)。

```yaml
schema_version: 3
kind: mcp-service
service:
  id: ticket-center            # 稳定逻辑能力，跨版本/仓库不变
  version: 0.1.0
  contract: ticket-center/v1
  title: 工单查询与订阅
installation:
  steps:
    - argv: ["${runtime.python}", "${package.root}/prepare.py", "${service.data}"]
      timeout_seconds: 30
runtime:
  transport: stdio             # stdio | streamable_http
  argv: ["${runtime.python}", "${package.root}/server.py", "${service.data}"]
  environment:
    TICKET_AUTH: {from_env: TICKET_SERVICE_AUTH}
health:
  interval_seconds: 30
  timeout_seconds: 5
  failure_threshold: 3
  recovery_threshold: 2
  expected_tools: [ticket_search, ticket_subscribe]
  probe_tool: {tool: ticket_search, arguments: {query: "健康探测"}}
  startup_timeout_seconds: 30
operations:
  runbook: docs/operations.md
  restart_safe: true
  session_sharing: safe        # isolated 的服务不能共享给多个 Agent
```

- 占位符只支持 `${runtime.python}`、`${package.root}`、`${service.data}`，HTTP 服务另有 `${service.port}`。Markdown 正文不当作 shell 执行。
- `runtime.environment` 只能写字符串或 `{from_env: NAME}` 引用；秘密值不进入仓库、日志或收据。
- `${service.data}` 指向部署的持久数据目录，安装失败或版本清理都不会删除它。
- `expected_tools` 参与协议验证；`probe_tool` 必须是声明的只读业务探针（示例为工单查询，不会创建/修改业务数据）。

### 3.2 Agent 声明 `kind: agent`

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
tasks:
  daily-triage:
    requires: [tickets]
```

`services` 是申请，不是授权：实际可用工具和数据范围来自平台绑定。v3 Agent 不再声明 repo 私有 `mcp.servers`；旧 v1/v2 语义保持不变。声明是精确的：`services: []` 或缺少该字段表示不使用任何共享服务，不会回退为平台上的全部绑定；只有 Admin 兼容视图可以列出全部绑定。

## 4. 对象与状态

| 对象 | 含义 |
| --- | --- |
| `service_id` | 稳定逻辑能力，如 `ticket-center` |
| `revision_id` | 固定 commit + 组件路径 + 清单摘要的不可变版本 |
| `deployment_id` | 某环境/数据范围的一份部署，管理生命周期与持久数据 |
| `generation` | 一次启动/切换代际，与网关 PID/出生时间一起核对 |
| Binding | Agent 稳定身份 → 部署 → alias/契约/工具/数据范围，带修订号 |
| Operation | 持久 `operation_id`、计划、幂等键、状态、错误与收据 |
| Incident | 按部署 + 代际归并的故障单，汇总受影响 Agent |

部署分别保存：

- 期望状态 `desired_state`：`running` / `stopped` / `removed`
- 发布状态 `publish_state`：`unpublished` / `published` / `withdrawn`
- 观测状态 `observed_state`：`unknown` / `starting` / `healthy` / `degraded` / `unhealthy` / `stopped`，附检查时间与分层证据
- 验证等级 `verification`：`not_verified` / `protocol_verified` / `business_verified`

「进程存活」「MCP 协议可用」「业务探针通过」是三层证据；只有完成声明的只读业务探针才会标记 `business_verified`。

## 5. Admin 管理工具

| 工具 | 用途 |
| --- | --- |
| `prepare_service_install` | 只读计划：固定候选、组件、清单摘要、凭据缺项、风险 |
| `apply_service_install` / `get_service_operation` / `cancel_service_operation` | 执行与查询安装操作 |
| `publish_service` / `withdraw_service` | 发布/撤销，不自动授权 |
| `register_external_service` | 登记外部 HTTP MCP 并执行真实协议验证 |
| `list_services` / `get_service` / `probe_service` | 目录、详情与立即探测 |
| `read_service_log` / `list_service_incidents` / `get_service_incident` | 脱敏日志与故障单 |
| `start_service` / `stop_service` / `restart_service` | 生命周期；外部服务可启停平台本机路由，`restart_service` 只针对托管进程 |
| `list_service_agents` / `bind_agent_service` / `unbind_agent_service` / `list_agent_bindings` | 稳定身份与显式绑定 |
| `prepare_knowledge_migration` / `apply_knowledge_migration` / `get_knowledge_migration` / `verify_knowledge_migration` / `rollback_knowledge_migration` | 知识库接管 |

常见意图：

```text
只安装这个 MCP，先不要发布。
安装并发布 <repo>，然后授权给 triage-agent 和 ops-agent，只允许 ticket_search。
登记外部端点 https://mcp.internal.example/mcp，预期工具 ticket_search。
迁移现有知识库到共享服务目录，先生成计划给我看。
```

- 只安装 → 私有启动并验证，`publish_state=unpublished`，普通 Agent 不可见。
- 安装并发布 → 验证达标后登记目录；仍需绑定才可被 Agent 使用。
- 凭据缺项、组件/清单不匹配、计划过期或来源 commit 变化会明确失败，不会静默换版本。
- 安装/停止/重启/恢复都经过同一部署锁与代际检查；重复请求用幂等键复用同一操作。

## 6. 发布、绑定与运行入口

- 发布要求部署有活动健康代际且验证等级至少 `protocol_verified`；`withdrawn` 不能直接重新发布，需重新验证。
- 绑定保存 alias、契约、工具、数据范围和修订号；撤权立即使新调用失败，旧的未用授权失效。
- 普通共享服务的数据范围使用显式 `scope: {"projects": [...]}` 项目白名单；不支持的非空结构在绑定时拒绝。网关读取工具的完整 input schema：工具没有项目参数、调用未提供项目参数、项目参数为空集合/空字符串或值超出白名单时都拒绝，不会放行成全库查询。知识库使用自己的项目/投稿范围字段。
- 普通 Run、cron、网页 repo 对话和获授权的 Admin 业务访问共用 [service_resolver.py](../src/agent_base/service_resolver.py)：同一份解析结果生成 dsh patch 和 `.agent/resources.json`，不出现「目录声称可用但客户端未挂载」。
- 凭据只经进程环境传给连接层，不进入提示词、资源目录或日志。
- 托管和外部接入层都在服务端校验授权、绑定状态、任务/轮次存活、工具范围和项目范围；`scope.projects` 对 `project`/`project_id`/`namespace` 等参数逐次校验，范围外调用被拒绝。
- stdio 网关缓存并透传完整工具定义（描述、input/output schema 等）和完整 `CallToolResult`（含 `isError`/`structuredContent`）；HTTP 代理的 `tools/list` 按绑定过滤，目录与实际调用范围一致。
- 必需服务缺失或契约不匹配时，任务在产生业务副作用前失败；可选服务不可用时提示本轮不要编造结果，不自动换源。
- Agent 停止/更新/卸载只影响自己的绑定，不停止共享部署。
- 重新安装同一部署的新 commit 时，候选只在安装步骤成功后写入活动指针；安装期间公共 start/restart/stop 和另一个安装会明确冲突，不会提前激活未完成安装的候选。之后排空旧代际、核对活动 revision/config 身份并启动新代际；只有新代码实际启动并通过验证才报告成功。存在未排空的在途调用时拒绝切换，并恢复原 revision/config/代际与期望状态，旧调用继续完成；只有本操作创建的网关会被清理；执行器崩溃后恢复流程会读取计划中的活动快照并恢复旧代际。相同 revision 和配置的重复安装复用现有代际。
- 安装准入在同一个数据库写事务内完成幂等键查重、活动安装检查和操作创建，跨进程的不同请求只有一个可进入安装。相同幂等键始终复用原操作，包括已完成请求的重试；其他部署可独立提交。活动安装查询不受近期历史列表条数限制。

## 7. 监督器与看护 Agent

确定性监督器运行在 `serve` / `worker` 内，不依赖模型：

- 默认 30 秒探测、5 秒超时；连续 3 次失败进入 `unhealthy`，连续 2 次成功恢复 `healthy`；启动宽限期由部署配置。
- 恢复默认 10 分钟内最多 3 次，退避 30/60/120 秒。`restart_safe` 是维护者声明，仍受部署授权、预算、维护状态和在途调用约束。
- 主动 `stopped`、维护中、`withdrawn` 的服务不会被自动复活；外部服务只探测/报告，不远程重启。
- 存在结果未知的非幂等调用、无法安全排空或安装/升级占用时记录故障单并升级，不强制重启。
- 故障单按部署 + 代际归并，恢复后先进入 `monitoring`，持续健康后附证据关闭。
- 监督器心跳过期时管理页显示 `stale`，不继续展示历史 `healthy`。

看护 Agent 使用专用身份和受限 MCP 工具（`services_overview`、`service_detail`、`read_service_log`、`probe_service`、`restart_service`、`list_incidents`、`record_incident_note`、`close_incident`、`escalate_incident`），通过现有 dsh 运行入口执行巡检。巡检启动时会禁用 dsh 默认可扩展工具行（fs/搜索、bash/pwsh、jobs、skill、subagent、workflow、web、goal、todo、plan-mode、MCP 资源目录等），模型可见目录只保留 `mcp__guardian__*`；新增默认工具会让真实模型目录回归失败，而不是静默扩大权限。重启仍由服务端校验 `restart_safe`、`allow_guardian_restart`、预算与在途调用；没有宿主 shell、模型或平台管理凭据。修改凭据、升级版本、数据修复只能形成计划并升级。

配置：

```toml
[guardian]
enabled = true
interval_seconds = 300
model = "deepseek-flash"     # 可选，默认沿用 runtime
effort = "high"              # 可选
timeout_seconds = 600        # 可选
```

模型不可用、看护进程退出或巡检超时不会停止监督器；管理页显示看护 `failed` / `stale`。故障提示默认留在平台，不自动外发。

## 8. 管理页

打开 <http://127.0.0.1:8765/services>（侧栏「共享服务」）。页面基于同一管理 API 和认证，不使用另一套无权限接口：

- 目录：托管/外部类型、版本/契约、期望/发布/观测状态、验证等级、owner、绑定 Agent、未关闭故障单、监督器与看护状态。
- 详情：三层健康证据、业务统计、操作、绑定（可在页面绑定/撤权）、故障单与动作、最近操作、脱敏日志。
- 操作：发布/撤销、启动（外部为“启用路由”）/停止/重启/探测、维护模式、绑定/撤权。安装由 Admin 管理工具发起，详情页展示安装计划、进度步骤与结果（commit/组件/revision/验证等级/工具/缺项/风险）。请求成功只表示已受理，页面继续显示持久状态；错误（维护中、预算不足、未授权等）在详情内可见。
- 指标：按 `deployment_id` 和真实进程归属只计一次；外部服务不挂本机 CPU/内存，显示「远端不可测」。
- 页面每 4 秒刷新，支持窄屏（详情全屏、无整页横向滚动）；详情请求带 epoch，关闭/切换后迟到的响应不会重新打开浮层。保留既有仪表盘、知识库、对话和定时页面。

## 9. 外部服务

`register_external_service` 登记 HTTP MCP 端点、owner、预期工具、契约和凭据引用，并立即启动一个平台本机的**接入层**：外部端点只被平台受控代理访问，业务凭据（`headers` 的 `from_env` 引用）只存在于接入层进程环境。验证走接入层完成真实 MCP `initialize` 与工具目录检查；仅 HTTP 200 不算验证通过。

- Agent 只拿到接入层的本机地址和平台 grant；工具目录按绑定过滤，调用校验工具范围和 `scope.projects`，撤权后新调用立即被拒绝。
- 平台只探测、授权、撤销平台路由和报告异常；不会启动、杀进程或重启远端机器。`start_service`/`stop_service` 控制的是本机接入层路由（非远端进程），`restart_service` 仍拒绝外部服务。
- 外部服务的远端 CPU/内存不可测，页面只显示可观测状态，不挂本机进程指标。

## 10. 知识库接管

知识库可以通过显式迁移进入同一目录、绑定和授权链路，不再由提示词隐式注入流程：

- `prepare_knowledge_migration` 只读生成固定计划：原实例 PID/端口/状态、监督归属、数据目录、项目、文档/版本/投稿/审计摘要、旧绑定到平台 Agent 身份的映射、风险与 `plan_digest`。
- `apply_knowledge_migration` 在部署锁内执行：用 SQLite 在线备份 API 生成快照并核对摘要；默认复制到 `deployments/<id>/data`，不停止或改写来源数据；登记内置知识部署、启动唯一监督者、真实 MCP 验证、映射平台绑定并按计划发布。
- 迁移默认保留原知识库实例与数据。`in_place` 会先停止兼容管理器再在同一数据目录启动部署代际，必须由管理员明确确认。
- 来源在计划后变化、未映射的旧主体、同一数据目录已有其他监督归属都会被拒绝；重复执行可复用已核对的副本，不会启动第二个实例。
- `verify_knowledge_migration` 核对目标数据库完整性、迁移记录是否完整、监督者数量和数据目录分离；`rollback_knowledge_migration` 停止迁移后的部署并保留数据，就地接管会恢复原实例。
- 旧 v1/v2 知识绑定继续走兼容路径；一旦迁移并完成平台绑定，新路径通过 `ServiceResolver` 挂载共享部署，不会重复挂载。

本地验收在隔离状态目录完成，未接管本机现有知识库，也未删除其数据。

## 11. 示例与统一验收

示例位于 [examples/managed-services](../examples/managed-services)：

- `ticket-center-stdio`：共享 stdio MCP，含只读查询、幂等订阅、持久数据、安装步骤和运维说明。
- `ticket-center-http`：原生 HTTP MCP，由网关分配私有端口并作为稳定 Streamable HTTP 端点暴露。
- `agents/triage-agent`、`agents/ops-agent`：v3 Agent，分别只申请查询和查询+订阅。

统一验证入口：

```sh
UV_CACHE_DIR=/tmp/agent-base-uv-cache \
  uv run --frozen python scripts/validate_managed_services.py
```

脚本使用全新临时状态目录和虚构凭据，完成：stdio/HTTP 安装与验证、发布、两个 Agent 共享一个部署、真实工具调用、HTTP 稳定路由与范围拒绝、订阅跨重启保留、故障注入与限次恢复、看护 Agent 经真实运行入口执行重启、知识库隔离迁移，最后停止它创建的进程。收据默认写到 `.agent-base/validations/managed-services-<timestamp>/result.json`（已忽略），包含每次检查、run_id、代际、故障单、迁移摘要和清理结果，不含凭据值。

本地 dsh 不可用时会改用受控 fake runtime（经真实 MCP 调用）并在收据 `skipped` 中明确标注。真实 dsh + 本地 OpenAI 兼容网关路径在具备 `.runtime/dsh` 时执行。不要用 fake runtime 收据冒充真实模型验收。

## 12. 已知限制与现场待验收

- Windows 真机上的服务安装/重启/取消/清理、路径长度和进程组行为；当前本地验收平台是 macOS。
- 真实企业 provider/代理/CA、真实内网 MCP 和实际企业工单后端；本地只使用虚构端点与凭据。
- 外部服务的远端资源指标无法采集，只报告可观测状态。
- 共享 stdio 只接入能安全复用连接的服务；声明 `session_sharing: isolated` 的服务不能共享给多个 Agent。
- 迁移为显式运维操作。正式接管现有业务知识库前需按计划停机/排空并验证备份恢复；开发测试不会自动执行。
- 本阶段未建设 fetch、GUI 自动连接、集群高可用和完整多用户平台。
