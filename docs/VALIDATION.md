# 初版验证记录

## 2026-11-17：MCP 发现本地层与设计文档整合

**背景**：平台全部 MCP 已统一为 Streamable HTTP，但外部 Agent 客户端（opencode、Claude Code 等）没有发现与接入入口。本次新增 `mcp_discovery.py` 发现层；同时因该实现未先对照既有 MCP 发现与托管设计（事实设计并入总纲 §12，远程愿景并入 §17.3；原文见 Git 历史 docs/MCP_DISCOVERY_HOSTING_DESIGN.md）评审即动手（流程失误），将全部历史设计文档整合为 [系统设计总纲](ARCHITECTURE.md) 并建立"先设计后实现"的变更约定。

**实现范围**（设计总纲 §12 为事实设计）：

- `GET /api/mcp` 全量清单（repo/managed/external/builtin/legacy 五类；凭据只引用不返回值）、`GET /api/mcp/config?agent=dsh|opencode|claude-code` 按客户端生成可合并配置片段（opencode `type:remote` + `{env:VAR}`、Claude Code `type:http` + `${VAR}`、dsh patch insert 行）、`GET /.well-known/mcp.json` 最小发现文档、CLI `agent-base mcp list / config`。
- `POST /api/mcp/grants` 会话授权：面向外部 Agent 的无进程绑定 grant（TTL 60s–24h），复用现有绑定体系；网关逐调用校验绑定存活性、工具白名单与项目范围不变，解绑即时失效。legacy 知识库签发只读 admin 范围；builtin 部署经 `builtin_grant` 按绑定范围签发。
- 明确边界：repo 命名空间因 `X-Agent-Run` 约束标记不可直连；本层为单用户本机发现，远程 Registry/exposure/OAuth 愿景收敛为总纲 §17.3 路线图，并对原设计（Git 历史 docs/MCP_DISCOVERY_HOSTING_DESIGN.md）§10.1 的 7 项源码问题做了 2026-11-17 复核（#1/#2/#4/#6 已修，#3/#5/#7 开放）。

**验证结果**：

- 新增 `tests/test_mcp_discovery.py` 6 项：清单覆盖各 kind 与凭据不落值、三种客户端片段、repo 命名空间平台专用语义、会话授权真实网关调用与解绑即时失效（401）、参数校验、legacy 知识库授权真实 MCP 调用。
- `uv run pytest -q tests tickets/acceptance`：**244 passed**，1 个既有弃用提示，239.43 秒（项目测试 223 项含新增 6 项、第一阶段验收与独立复现 21 项）。
- 真实数据端到端：发现 → 签发 → Streamable HTTP 连接 → `knowledge_catalog` 调用成功。过程中发现本机知识库常驻服务为 11-11 启动的旧代码（早于 admin-grant 兼容提交 43e45db），`agent-base knowledge restart` 后恢复；该服务重启属运维动作，未改动数据。
- 文档整合：6 个历史设计文档已并入 [系统设计总纲](ARCHITECTURE.md) 并从 docs/ 根删除（原文保留在 Git 历史，删除前路径见总纲开头）；根 README、docs 索引、Knowledge/ManagedServices 与工单引用同步改为指向总纲或 Git 历史。
- Playwright 本轮未执行（改动不涉及前端）；现场待验收项不变。

## 2026-11-17：工单 002 再次边界复验修复（F2/F6）

独立复验在第 14 节的相邻边界中发现 2 个 P1，已修复并加入正式回归。

- **F2 空项目集合**：网关对项目参数的空数组、空字符串、缺省和非字符串值均拒绝；`projects: []` 不能退化为无过滤全库查询。stdio/HTTP 一致。
- **F6 安装期提前激活候选**：安装步骤完成前不改写活动 revision/config；进行中的 install operation 作为持久准入，公共 start/restart/stop 与另一个安装都会冲突，不会切换候选。候选只在步骤成功后的 start/validate 中提升；失败/取消/崩溃恢复活动快照，旧网关和在途调用保留。
- 正式回归：`test_empty_project_filter_cannot_read_outside_binding[stdio|http]`、`test_concurrent_start_and_restart_cannot_activate_install_candidate`，以及既有 F2/F6 回归。

复验结果：

- 原 10 项 + 补充 4 项 + 再次补充 3 项独立复现：**17 passed**，26.88 秒。
- `pytest -q tests tickets/acceptance`：**233 passed**，1 个既有弃用提示，233.00 秒（项目测试 213 项、第一阶段验收 3 项、独立复现 17 项）。
- Playwright：**19 passed**，39.9 秒。
- `scripts/validate_managed_services.py`：**28 项通过**，收据 `.agent-base/validations/managed-services-20261117-093033/result.json`。
- 最终证据：`tickets/acceptance/002-final-20261117.json`。未接管现有业务部署/知识库，未执行真实付费模型调用。

Windows 真机、真实企业 provider/代理/CA、真实内网 MCP 与真实工单后端仍为现场待验收项。

## 2026-11-16：工单 002 补充边界复验修复（F2/F6/F8）

独立复验在第 12 节的补充检查中发现 3 个边界问题，已修复并加入正式回归。

- **F2 无项目参数绕过**：网关读取完整工具 input schema；`scope.projects` 下工具没有项目参数、调用缺少项目参数、嵌套或显式参数越界都拒绝，不再放行全库查询；stdio 与 HTTP 一致。
- **F6 繁忙升级清理旧代际**：安装计划记录活动快照与候选 revision；拒绝切换/失败时若旧网关存活，恢复原 revision/config/config_revision、generation、PID/出生时间、期望/观测状态，不执行 stop，旧在途调用继续完成；只清理本操作创建的网关。旧代际已停止时记录 `upgrade_failed_requires_input`，不伪造服务仍运行。
- **F8 详情关闭后迟到响应重开浮层**：服务页详情请求使用单调 epoch，关闭/切换/自动刷新/动作后检查，旧响应不能重新设置 detail；新增 1.2 秒延迟浏览器回归。
- 独立验收 busy-upgrade 脚本的 HTTP 客户端默认 5 秒超时恰好等于测试后端 5 秒慢调用，会把保留成功误报为超时；将该测试客户端超时校正为 20 秒，不改变业务断言、mock 或慢调用。

复验结果：

- 原 10 项 + 补充 4 项独立复现：**14 passed**，22.70 秒。
- `pytest -q tests tickets/acceptance`：**227 passed**，1 个既有弃用提示，221.82 秒（项目测试 210 项、第一阶段验收 3 项、独立复现 14 项）。
- Playwright：**19 passed**，39.9 秒（新增迟到响应回归）。
- `scripts/validate_managed_services.py`：**28 项通过**，收据 `.agent-base/validations/managed-services-20261116-175525/result.json`。
- 最终证据：`tickets/acceptance/002-final-after-boundary-fixes-20261116.json`。未接管现有业务部署/知识库，未执行真实付费模型调用。

Windows 真机、真实企业 provider/代理/CA、真实内网 MCP 与真实工单后端仍为现场待验收项。

## 2026-11-16：工单 002 独立验收问题修复（F1–F7）

独立验收发现 7 类问题并记录在 `tickets/002-phase-2-acceptance.md`；本轮修复后 10 项独立复现全部通过。

- **F1 外部服务绕过授权**：外部端点不再直接挂给 Agent。`register_external_service` 启动平台本机接入层；Agent 只获得本机地址和平台 grant，工具目录按绑定过滤，调用校验工具与 `scope.projects`，撤权后返回协议错误。外部业务凭据只存在于接入层环境，平台仍不启动/重启远端进程。
- **F2 项目范围未执行**：普通共享服务范围明确为 `scope={"projects":[...]}`；绑定拒绝未知结构，stdio 与 HTTP 网关逐次校验 `project`/`project_id`/`namespace` 等参数，范围外调用失败。
- **F3 看护默认工具**：看护 patch 禁用 dsh 默认 fs/搜索、shell、jobs、skill、subagent、workflow、web、goal、todo、plan-mode、MCP 资源目录等工具行；真实 dsh 模型请求的工具目录只含 `mcp__guardian__*`。
- **F4 stdio 契约丢失**：缓存并透传完整工具定义（描述、input/output schema）与完整 `CallToolResult`（`isError`/`structuredContent`/meta）。
- **F5 HTTP 在途计数**：目录过滤分支不再重复增加 `in_flight`，一次请求只计数/释放一次。
- **F6 升级仍运行旧代码**：网关健康响应携带 revision/config 身份；start 早返回同时核对活动身份，不一致时排空并切换代际；相同 revision/config 的重复安装复用代际。
- **F7 v3 空声明回退全部绑定**：v3 `services: []` 或缺失字段精确解析为空；只有 Admin 无 manifest 时才列出全部绑定。

正式回归新增于 `tests/test_services_install.py`、`tests/test_services_catalog.py`、`tests/test_service_supervisor.py`，没有只改独立验收脚本。复验结果：

- `pytest -q tests tickets/acceptance`：**219 passed**，1 个既有弃用提示，209.57 秒（项目测试 206 项、第一阶段验收 3 项、独立复现 10 项）。
- Playwright：**18 passed**，38.0 秒。
- `scripts/validate_managed_services.py`：**28 项通过**，收据 `.agent-base/validations/managed-services-20261116-170939/result.json`。
- 复验证据：`tickets/acceptance/002-review-after-fixes-20261116.json`；隔离目录、虚构凭据，未接管现有业务部署/知识库，未执行真实付费模型调用。

Windows 真机、真实企业 provider/代理/CA、真实内网 MCP 与真实工单后端仍为现场待验收项。

## 2026-11-16：工单 002 第二阶段共享 MCP、看护与管理页（A/B/C/D）

- 完整 Python 回归：`UV_CACHE_DIR=/tmp/agent-base-uv-cache uv run --frozen pytest -q`，**198 passed**（含 1 个既有 Starlette/httpx 弃用提示，147.39 秒）。新增覆盖 v3 清单、stdio/HTTP 托管安装、发布与外部登记、显式绑定与撤权、统一解析、故障单/恢复预算/维护状态、看护受限工具、内置知识库监督、知识库迁移，以及共享服务管理 API。
- 浏览器回归：`frontend` 目录 `npx playwright test`，**18 passed**（原 15 项加 3 项共享服务页面：状态/绑定/详情/维护拦截/空态与错误态/390px 窄屏无整页横向溢出）。测试使用隔离服务、fake CLI 和真实本机 MCP；fixture 启动时会清理上次被强制结束的残留会话进程。
- 统一本地验收：`uv run --frozen python scripts/validate_managed_services.py`，**28 项检查通过**，收据 `.agent-base/validations/managed-services-20261116-155634/result.json`（忽略目录，不含凭据值）。本次环境 `real_dsh: true`，无跳过项。

### A. 安装控制面

- 同一 repo 的 MCP 组件按固定 commit 审阅，计划冻结 service/revision/组件/清单摘要；stdio 与原生 HTTP 两个示例均完成真实 MCP `initialize`、工具目录和只读业务探针，验证等级 `business_verified`。HTTP 后端由平台分配私有端口，外部只暴露部署稳定端点。
- 重复安装复用不可变 revision 与同一活动代际；并发安装只产生一个持久操作，落败请求返回 duplicate 快照；安装步骤失败进入 `needs_input`，不自动重跑未知结果；执行器中断的未知步骤同样要求人工核对；失败安装不遗留孤儿实例；数据目录在失败和重启后保留。
- 安装/运行环境剔除 provider 凭据，只显式传递声明的业务凭据；日志读取按已知文件名并脱敏 Bearer token。

### B. 发布、外部服务与绑定

- 未验证部署拒绝发布；已撤销发布不能直接重新发布；发布不创建绑定。两个 v3 Agent 并发运行、绑定同一 stdio 部署，实际 Run 中工具目录分别为 `ticket_search` 与 `ticket_search + ticket_subscribe`，共享同一 `boot_id`，停止其中一个 Agent 不影响另一个的调用。
- HTTP 代理模式现在按绑定过滤 `tools/list`（JSON/SSE），越权 `tools/call` 仍返回授权范围错误；真实调用返回 `auth_ok`。外部 HTTP 服务经真实 MCP 协议验证后发布，平台拒绝启动/重启，且不显示本机资源指标。
- 撤权后已有授权立即被网关拒绝；必需服务缺失时 Run 在产生业务副作用前失败。运行入口统一生成 dsh patch 与 `.agent/resources.json`，凭据仅存在于进程环境。

### C. 监督器与看护

- 故障注入（直接终止网关）后连续探测为 `degraded, degraded, unhealthy`，形成一张故障单；监督器按预算启动新一代际（recovery_attempts=1）。持续健康后故障单附证据关闭；主动停止、维护中和已撤销发布的服务不会被复活。
- 看护 Agent 经真实 dsh 入口和受限 MCP 工具白名单完成 overview/detail/probe/note，并在部署预授权、`restart_safe`、预算和排空检查通过后执行真实重启（代际 3→4）；模型/看护不可用时监督器仍独立探测，管理页显示看护 `failed`/`stale`。
- 本轮看护的模型决策使用受控 fake dsh（收据中如实标注）；工具授权、服务控制器和实际重启是真实执行，不是 Mock 调用计数。

### D. 知识库迁移与管理页

- 隔离知识库完成 `prepare → apply → verify`：项目 1、文档 1、版本 1、待审投稿 1、审计与检索索引均保留；来源与目标业务摘要完全一致。迁移启动唯一部署代际，发布并生成平台显式绑定；目标数据库完整性、迁移记录、唯一监督者与数据目录分离核对通过。未接管、未停止本机现有知识库。
- `/services` 页面接入同一管理 API：展示期望/发布/观测状态、验证等级、绑定 Agent、故障单、最近操作和脱敏日志；启停/重启/探测/发布撤销/维护/绑定与撤权均显示持久状态，不把请求成功当作已恢复。外部服务只显示远端不可测。仪表盘按 `deployment_id` 且按真实进程归属只计一次资源。
- 共享服务页面桌面/手机截图在浏览器回归输出 `frontend/test-results/services-*.png`（测试产物，不提交）。

### 说明与尚未验收

- 本机为 macOS；Windows 真机上的服务安装/重启/取消/清理、进程组与路径长度仍需现场验收。
- 真实企业 provider/代理/CA、真实内网 MCP、实际企业工单后端未对接；本轮真实 dsh 使用本地 OpenAI 兼容网关和虚构凭据。真实付费模型测试未启用。
- 外部服务远端 CPU/内存不可测；共享 stdio 仅接入可安全复用连接的服务；`session_sharing: isolated` 的服务不能共享给多个 Agent。
- 未建设本地 fetch、dsh GUI 自动连接、集群高可用和完整多用户平台；未改动现有业务部署。设计差异和操作入口见 [ManagedServices.md](ManagedServices.md)。

## 2026-11-13：工单 001 配置快照凭据隔离修复（F1a）

- 常驻网关为每次本地业务工具调用合并本次 Run 配置快照中的 `api_key_env` 声明，以及服务启动配置、当前配置和内部标记中的声明。全局 provider 被删除、更名或配置暂时无法解析时，旧任务使用的凭据变量名仍被过滤；兼容没有 runtime 快照的旧运行记录。
- 将既有“服务先启动、后声明 provider”的真实本地 MCP 集成测试扩展为配置不变、删除 provider、更名 provider/凭据变量、配置无法解析四种场景。修改前为 **1 passed / 3 failed**，失败均为业务工具返回 `late_visible: true`；修复后四种场景及独立验收复现共 **5 passed**，凭据均不可见。
- 完整检查：仓库根目录执行 `UV_CACHE_DIR=/private/tmp/agent-base-acceptance-uv-cache uv run --frozen pytest -q tests tickets/acceptance`，**166 passed、1 个既有 Starlette/httpx 弃用提示，108.91 秒**。其中正式项目回归 163 项，两份本地验收脚本共 3 项；包括真实 dsh + 本地测试网关、真实本地 MCP、任务/cron/对话和目录链接回归。
- 测试仅使用虚构凭据、隔离状态目录与本地端口；未对接真实企业网关，未重启既有业务服务。修复需要在常驻 repo MCP 服务重启后加载。
- 结论：第一阶段本地可验证范围通过；Windows 原生 junction 真机创建/清理、跨卷与普通用户场景、真实内网 provider 仍按约定待现场验收。Linux 实机和 CodeAgent 内网的边界保持此前记录。

## 2026-11-13：工单 001 验收问题修复（F1/F2）

自动回归 **160 项通过**（含 1 项既有 Starlette/httpx 弃用提示）。

### F1 凭据隔离改为按声明名执行

- 启动 dsh 时，运行时快照里所有 provider 声明的 `api_key_env` 名都从 CLI 环境移除；所选 provider 的值改以固定别名 `AGENT_BASE_PROVIDER_API_KEY` 传入。别名含 `KEY`，因此 dsh 自身的子进程环境清洗也会剔除它。pi-ai 的 `apiKeyEnv` 写入别名，不再出现声明名。
- 业务侧不依赖命名习惯：v1 本地工具通过 patch 中的 `AGENT_BASE_CREDENTIAL_ENV_NAMES` 获得声明名清单并逐个剔除，同时保留既有 `API_KEY/TOKEN/SECRET/PASSWORD` 过滤；v2 常驻网关在每次调用时按当前配置的声明名剔除。v1 由 dsh 挂载的业务 MCP server 从父环境继承不到声明名或别名；v2 上游 MCP 使用 MCP SDK 的受限默认环境。
- repo 安装步骤、`uv sync`、clone/checkout、完成检查等 repo 代码子进程，以及知识服务，都不持有 provider 凭据；常驻 repo 服务以无凭据环境启动，不因某个 Run 的 provider 补发凭据。安装步骤的中性名隔离另有集成回归（步骤脚本报告 `neutral_visible: false`、`builtin_visible: false`）。
- 管理 MCP 是获授权进程，显式获得所选 provider 的别名引用，用于它自己提交的 Run；不再依赖固定的 `DEEPSEEK_*` 变量列表。
- 真实 dsh + 本地网关，中性变量名 `ACCEPTANCE_LOGIN`：Run `f855fa00a89f4c6bab44237db9bb84e2` succeeded / checks_passed。工具实际输出 `provider_credential_visible: false`；patch 只含别名与变量名清单，不含密钥值；运行目录全部文本文件均无测试凭据值。收据：`.agent-base/validations/provider-alias-20261113-171302/result.json`。
- v2 常驻网关集成回归调用本地 `probe_credential` 工具，返回 `neutral_visible: false`、`builtin_visible: false`，证明先启动的服务也不会把凭据交给业务工具。
- 验收复现脚本 `tickets/acceptance/test_provider_credential_name.py` 现在通过（2 passed）。网关回归同时覆盖含关键词名与中性名两种声明。

### F2 junction 不再经过命令解释器

- 不再调用 `cmd.exe` / `mklink`：junction 直接通过 `DeviceIoControl(FSCTL_SET_REPARSE_POINT)` 写入 mount point reparse point。目标路径以 UTF-16 数据写入 reparse buffer，无空格的 `&`、`^`、字面 `%NAME%`、`!` 以及空格、中文都不会被解释或展开；模块不再引用 `subprocess`。
- 保留“仅 winerror 1314 缺符号链接权限才回退”的策略、本地卷/UNC 拒绝、创建后 reparse point 与目标身份校验；路径超过 reparse 缓冲上限时明确报错。
- 新增回归：reparse buffer 头部与名字偏移、特殊字符原样保留、超长路径拒绝、回退只在权限错误发生、创建后目标不符拒绝、失败向上报告、模块不含 `subprocess`/`mklink`。macOS 目录符号链接创建、复用与清理回归继续通过。

### 尚未验收

- Windows 真机：junction 的原生创建、普通用户安装/运行/取消/失败清理、跨本地卷与特殊字符路径。当前只有平台无关的数据结构与参数验证。
- 真实内网网关：端点、代理、CA、鉴权方式和实际模型目录。
- Linux 实机与 CodeAgent 内网适配，范围同上一记录。

## 2026-11-13：第一阶段运行配置与跨平台修复

自动回归 **151 项通过**（含 1 项既有 Starlette/httpx 弃用提示）。本次新增回归覆盖固定 commit 审阅、目录链接与 junction 回退、provider 配置校验、dsh patch 生成和真实 dsh 网关链路。

### A. 文本路径扫描改由 Admin 审阅

- 删除 `path_lint.py`、`scripts/check_prompt_paths.py`、CLI `lint` 子命令，以及安装、入口读取、初始化完成和提示词拼装中的调用；没有用另一套正则或硬编码表替代。`within()`、包内相对路径约束、产物越界和符号链接越界检查继续保留，并仍有独立回归。
- 新增 `review.py` 与两个管理 MCP 工具：`prepare_repo_review` 用真实 `git clone` 固定 commit，返回候选 ID、commit 和 AGENTS.md、Installation.md、installation.md、entrypoint.md 的存在状态与大小；`read_repo_review_doc` 只允许这四个文档，单文件上限 128 KiB，越界路径和逃逸符号链接被拒绝。候选只读，不执行安装脚本、不启动业务 MCP、不触发任务，超过 24 小时由清理逻辑移除。
- `install_repo_resources` 接受 `candidate_id`，安装同一候选 commit；审阅后分支移动不会换版本。安装步骤修改文档后可用 `installation_id` 复核同一安装版本，返回 `phase` 区分 candidate 与 installed。
- 回归覆盖：固定 commit、文档缺失、超限、越界链接、候选过期清理、分支移动后仍安装原 commit、安装后复核、非法 origin/ref。根 AGENTS.md 和 RepoInstallation、RepoContract、DESIGN、export-guild 已改为语义审阅说明；本文件更早记录中的 `check_prompt_paths.py` 与 `agent-base lint` 描述是当时行为的历史记录，对应脚本已删除。

### B. dsh provider 配置

- `agent-base.toml` 的 `[providers.<name>]` 定义适配器、协议、端点、凭据环境变量名、模型目录和模型能力（容量、输出上限、输入模态、推理档位）。`deepseek-official` / `deepseek-account` 保持为内置路由，不能被自定义定义覆盖；未写 `[providers.*]` 时现有官方配置校验与行为不变。
- 同一份运行时配置生成 invocation patch：dsh 组合中已休眠加载的 `llm-pi-ai` 条目被就地配置，不重复插入第二个适配器实例。CLI Admin、网页 Admin、普通任务、cron 和 repo 多轮对话都经 `dsh_patch`，Run 和会话写入运行时快照，后续修改全局配置不会热切换已有会话。
- 凭据只以环境变量名出现在配置、patch、快照和收据中。管理 MCP 子进程只额外获得所选 provider 的凭据引用，业务工具和业务 MCP 不继承。缺凭据、未定义 provider、模型不在目录、档位未声明、协议非法、`base_url` 带内嵌凭据或模型目录为空都在加载或启动时明确失败，没有自动换源。
- `doctor` 输出所选 `provider`、`credential_env`、`credential_configured`，缺凭据返回非零；只报告配置状态，不冒充在线推理验证。
- 真实 dsh 链路验证：本地 OpenAI 兼容测试网关记录到 2 次请求，两次均使用目录中的 `internal-flash`，第一次请求带 `gateway_report` 工具并以 `tool_calls` 结束，第二次携带 `tool` 消息并回传 `gateway-roundtrip` 结果；Run `0f8973495aa545eebfc1cb6b1ad893e7` 为 succeeded / checks_passed，Agent 自报 completed，产物 `artifacts/gateway-report.txt` 内容与工具输出一致；运行目录内所有文本文件都没有测试凭据值，patch 只含变量名。同一场景的自动回归另断言业务工具输出 `provider_credential_visible: false`，即业务工具没有继承 provider 凭据。收据：`.agent-base/validations/provider-gateway-20261113-164608/result.json`。
- 示例配置 `config/internal-gateway.example.toml` 与 [Providers.md](Providers.md) 不含凭据；`init` 生成的配置保留原有官方默认值，附注释形式的内部网关示例。

### C. Windows 目录链接与 junction 回退

- 新增 `dirlink.py`，安装技能目录、Run 技能挂载和 package 挂载统一通过 `create_directory_link`：先建目录符号链接，仅在 Windows 上因 winerror 1314 缺权限失败时回退 `cmd.exe /d /c mklink /J`；其他失败（目标已存在、目标不是真实目录、mklink 失败）直接报错，不被当作权限不足吞掉。
- junction 只接受绝对路径的真实本地目录，拒绝 UNC/网络目标和非本地卷；命令使用固定 argv 结构，路径作为独立参数传递，创建后校验确为 reparse point 且指向预期目录。识别同时检查符号链接与 junction；`remove_directory_link` 和 `remove_tree` 只断开链接，真实目录明确拒绝，不递归删除目标。
- 回归覆盖：macOS 符号链接创建/清理并保留目标、拒绝重复创建与真实目录、删除树不跟随链接、权限错误才回退、固定命令结构、相对/文件/UNC 目标拒绝、mklink 失败上报、其他错误不回退、创建后目标不符拒绝、junction 识别仅在 Windows 生效。
- macOS 真机：既有 4 个演示安装的技能挂载仍为目录符号链接且目标完整；完整回归中的真实 repo 安装/运行链路通过。

### 尚未验收

- Windows 真机：尚未在没有开发者模式的普通用户机器上执行安装、运行、取消和失败清理；junction 回退目前只有平台模拟测试，跨本地卷依赖 `GetDriveTypeW`，未在 Windows 上实测。
- 真实内网网关：只用本地 OpenAI 兼容测试网关验证了协议、模型选择、工具调用、多轮工具结果和凭据隔离，尚未对接真实企业网关、代理、CA 和鉴权方式。
- CodeAgent 内网：本阶段未改动其认证与适配，[CODEAGENT_MIGRATION.md](CODEAGENT_MIGRATION.md) 的验收项仍待现场执行。

### 交付说明

- 完成内容：上述 A/B/C 三个工作包及对应文档、示例配置与回归。
- 自动检查：仓库根目录 `uv run --frozen pytest -q` 151 项通过（本次运行把 `UV_CACHE_DIR` 指向可写缓存目录，依赖与结果不变）。
- 遗留限制：Windows 真机与真实内网网关未验收；pi-ai 的 `compat`、`modelOverrides`、图片预算等高级字段暂未开放；详细字段见 [Providers.md](Providers.md)。

## 2026-11-11：集中式项目知识库与统一主页

- Python 完整回归 **105 项通过**；新增 9 项验证正式知识与投稿隔离、版本/归档/恢复、幂等、并发审批冲突、管理员修订保留原稿、事务失败回滚、8,000 字符响应上限、固定版本章节分页、凭证撤销、管理员环境变量剔除、真实 MCP 及三 repo 跨实例链路。最终修改另复核知识/仪表盘 23 项与知识 9 项。
- Playwright Chrome **15 项通过**：新增 3 项覆盖主页与旧页面跳转、管理员连接、项目/文档创建与编辑、未保存编辑保护、历史、绑定、投稿审批后可检索、桌面与手机布局；原聊天、仪表盘和定时任务均通过。
- 回归发现工作区在尚未创建时被缓存为 missing，新目录出现后未及时显示磁盘大小；已修正为目录创建立即失效，并新增断言，重新跑完 15 项浏览器检查。
- 真实 dsh 在独立状态目录运行两个独立原生会话。贡献者实际调用 catalog → search → read outline → read section → submit；投稿待审时正式检索中没有该文档。验收脚本以管理员角色批准后，新读者会话实际 search/read 获取正式版本和章节引用。9 次 Knowledge 工具调用有原生记录，集中 MCP PID 两轮保持一致，数据库核对 native_id 不同。
- 真实投稿 `1786304142494848ac1d81185fd14e36`，发布版本 `8703ab71f03e4c0c9c71a514726a7c94`。隔离收据：`.agent-base/validations/knowledge-real-1794382260316165000/result.json`。未在原有业务状态中创建这两轮任务。
- 本地主页和知识服务已加载。创建分析/研究两个明确标记的演示项目，各含一篇使用约定；财务、库存、计划演示 repo 绑定分析项目，研究 repo 绑定研究项目，Admin 明确授权两项目。只配置中央绑定，不修改冻结 repo 源码或共享 skills。
- 更新前确认没有活动工作；4 个既有业务 MCP 的 PID/出生时间/boot_id 未变。新版仪表盘只计 1 个共享 Knowledge MCP 进程，不按 repo 重复计数。主页和管理连接收据位于 `.agent-base/validations/knowledge-live-1794382541748478000/`，原任务数和对话轮次数未增加。

使用说明及实际首版边界见 [Knowledge.md](Knowledge.md)。

## 2026-11-11：实例、MCP 与工作区统计口径修正

- 完整 Python 回归 **96 项通过**，Playwright Chrome **12 项通过**。新增验证 0/1/2 个 MCP 的不同安装、相同启动命令的独立进程、旧网关唯一匹配/歧义、远程 MCP 不伪造本机内存、真实工作区文件/硬链接/外部 skill 符号链接/缓存/部分扫描，以及准备、验证和失败进程不算 dsh 实例。
- 活动网页对话测试确认只产生 1 个实例，MCP PID 与 dsh PID 不同、CPU/RSS 分开，实例包含非零工作区大小；停止后实例表为空，目录仍作为保留工作区展示。浏览器同时覆盖手机布局、旧聊天和定时任务功能。
- 本机现有 4 个演示安装均声明一个 `domain` MCP，一个本地 `write_report` 工具和一个 `analysis` skill。真实只读采样已拆成 **4 个 MCP 服务（各约 57.6–57.8 MiB）+ 4 个网关（各约 60.8–61.1 MiB）**，每个服务/网关各对应 1 个 Python 进程；当前 dsh 实例为 0。
- 实际识别 10 个已登记保留工作区，逻辑大小合计 75,088 字节，扫描完整；数值为验收当时快照。没有把共享 skill 链接算作每个工作区的副本，也没有把 session/日志大小混入工作区。
- 仅更新无活动工作的 Web 进程，4 个既有 MCP 的 PID/出生时间/boot_id 全部未变；读取前后 Run 与对话轮次数未增加，没有模型调用。已检查并更新桌面、明细、手机截图。

本机收据：`.agent-base/validations/dashboard-attribution-1794364594092711000/result.json`。指标定义与扫描边界见 [Dashboard.md](Dashboard.md)。

## 2026-11-11：运行仪表盘

- 完整 Python 回归 **92 项通过**；新增 10 项覆盖只读采样、CPU 首次未知、有限历史、MCP 健康缓存/停用/异常、进程出生时间与嵌套去重、MCP 资源子集、Repo 对话不重复计数、Admin CLI 登记、鉴权和失败快照。存在 1 项已有 Starlette/httpx 弃用提示。
- Playwright Chrome **12 项通过**；新增 4 项覆盖仪表盘导航/刷新/暂停、实际生成中的实例计数与跨页不中断、Token 共享、未知/过期数据和 390px 手机无整页横向溢出。测试使用隔离服务、fake CLI 和真实本机 MCP。
- 本地服务已加载 `/dashboard`，HTTP 页面和认证 API 可读。实际采样确认 **4 个 MCP 连接健康、0 个活动推理实例**，内置调度器健康，主机 CPU/内存与受管进程指标可读取。
- 重载 Web 前确认没有活动任务/对话轮次；重载后核对 4 个既有 MCP 的 boot_id 均未变。浏览仪表盘前后任务总数与对话轮次总数未增加，没有执行模型调用或新增定时计划。
- 桌面概览、MCP/存储明细与手机截图已检查，保存为 `docs/assets/dashboard-*.png`；截图历史栏筛选为空的演示 Agent，未展示既有私有对话标题。

本机原始采样、只读验证收据与截图位于忽略目录 `.agent-base/validations/dashboard-1794362042239073000/`；接口、指标定义与范围见 [Dashboard.md](Dashboard.md)。

## 2026-11-10：对话入口与图形化定时任务

- 完整 Python 回归 82 项通过，新增 14 项覆盖安装版本绑定、一次性触发去重、停机补触发、暂停/恢复、时区与夏令时边界、API 认证和实际 entrypoint 报告。
- Playwright Chrome 8 项通过：原有聊天交互、Agent 列表唯一创建入口、独立历史筛选、跨页面保留 Token、图形化计划、暂停/恢复、运行记录/报告、手机布局与无横向溢出。使用独立测试数据、fake CLI 和真实本机 MCP。
- 真实验收由 Chrome 页面创建一次性计划 `schedule-d86b92df56b04ae59f0ade3b94dd1715`，指定 **2026-11-10 15:34 Asia/Shanghai**。实际时钟到点后，常驻调度器创建 Run `33059347f56641adb508eb9b185d1c5e`，真实 dsh 按 entrypoint 调用演示 MCP 得到数值 10；最终为 succeeded / checks_passed，Agent 自报 completed。
- 真实计划只有一个 Run，触发后 enabled=false、next_due=null，MCP boot_id 未变。没有保留周期任务。演示 `scheduled-analysis-agent` 仍可从 Agent 列表选择，旧三个 Repo 的安装与历史未修改。
- 网页、`/cron` 和新 API 已在本机 serve 中加载。截图保存在 docs/assets；原始验收收据位于 `.agent-base/validations/graphical-schedule-1794295954561/result.json`，不提交业务运行数据。

界面与 API 说明见 [SchedulingUI.md](SchedulingUI.md)。

## 2026-11-10：Repo 引入契约增强

- Python 回归：68 项通过，其中新增 entrypoint/报告相关 20 项。包含入口单独启动、cron、准备后启动、冻结源、v2 常驻 MCP 复用、普通网页对话不注入入口、错误报告/blocked、额外产物归档、超时/取消及报告 HTTP 接口。
- Playwright Chrome：4 项全部通过，使用独立服务和 fake CLI；确认普通网页对话保持原有交互和资源行为。
- 文档链接、代码块、Python 编译、全项目 AGENTS/entrypoint/skill 路径检查通过。
- 真实 dsh：run_id `5cb3510b1aa949a2a26e02d996ae5c65`，使用隔离状态目录和新建 hello 示例 Git repo；未传 task，实际 prompt 顺序为 AGENTS → entrypoint → 基座报告契约，没有附加旧 task.default。原生记录包含 5 次工具调用，生成真实业务报告，冻结检查脚本通过；Run 为 succeeded / checks_passed，Agent 最终 JSON 状态为 completed，归档 1 个真实产物。

真实执行收据：`.agent-base/validations/entrypoint-1794277460092584000/result.json`。基座 report.json 位于该隔离目录内的 `state/runs/5cb3510b1aa949a2a26e02d996ae5c65/`。运行记录不提交到 Git。

后续收尾已启动新版 serve，网页与报告接口均返回 HTTP 200，列表包含 Admin 和三个 Repo Agent，三个常驻资源服务均存活。启动前无活动 Run 或消息，旧验收 cron 保持暂停；没有新增周期任务或修改/验证 CodeAgent 内网运行。新契约格式与兼容规则见 [RepoContract.md](RepoContract.md)。

---

记录日期：2026-11-05。平台：macOS，Python 3.13.14，Node.js v26.4.0，dsh npm 0.2.1-rc.5。

## 自动验证

执行 `.venv/bin/python -m pytest -q`，48 个用例通过。范围包括：

- 实际 Git clone、固定 commit、AGENTS 与中文多行任务传递、model/effort patch。
- 先安装再启动，拒绝重复 start。
- 两个任务并发、第三个排队、不同工作区隔离。
- 排队和运行中取消、超时、运行器失败、缺少产物。
- 中断状态归结，迟到的中断检查不能覆盖成功状态。
- 管理对话复用原生 session ID。
- 官方 MCP Python 客户端与真实管理 MCP server 交互，安装并启动 fake-runtime 任务。
- 真实业务 MCP server 的中文输入、执行和 schema 错误回执。
- Web 触发、原子幂等键、工作启停、跨来源写请求拒绝、可选 Bearer 认证。
- 同机调度器竞争、队列容量、满队列仍能出队、cron 停机合并和重叠跳过。
- 未派发队列重启保留、实际后台调度线程、成功临时目录清理、输入副本隔离。
- 运行中 session 可见、超时 session 保留、强制杀死监督进程后的 CLI 清理和 interrupted。
- Installation.md 配置块解析、重复配置拒绝、安装复用及并发安装只初始化一次。
- 三 repo 同名 skill/MCP 的命名空间、固定技能文件及常驻 MCP 跨实例复用。
- 任务超时不停止共享服务、工具调用超时不重启服务、服务启停及三次失败恢复限制。
- v2 首次运行自动安装、prepared 后启动、工作触发时绑定已有安装与 Web 资源清单。
- POSIX/Windows/UNC/HOME 路径检查、skill 引用文档检查、相对路径及中文斜杠分隔词的误报回归。
- 网页对话原生 session 续接、独立工作区、并发消息去重、活动轮次互斥、取消/超时/异常状态。
- 会话历史、SSE 游标重放、归档与认证、工具错误状态、屏蔽原生 thinking、产物下载与路径范围。

上述自动测试的模型执行器是 deterministic fake CLI，不是外网模型。

## 真实模型链路

使用启动环境已存在的 DeepSeek 凭据，未写入仓库、patch 或验证记录。

1. `.venv/bin/agent-base admin --session smoke --message '只回复 ADMIN_AGENT_READY，不调用任何工具。'`：实际收到 `ADMIN_AGENT_READY`。
2. 创建本地示例 Git 仓库后，在 admin 对话中要求安装、运行默认任务并等待结果。
3. admin 原生事件记录有 3 次 tool_call/tool_result；后台任务实际创建独立 clone。
4. 仓库 agent 的原生记录有 2 次 tool_call/tool_result，完成业务 MCP 写报告及读取检查。
5. 基座运行冻结的 `verify_report.py`，输出 `report verified`，记录为 succeeded / checks_passed。

本次真实验收 run_id：`b0cb29ddb2d948f6bf5e3367ba9fa7ec`。

本机结果记录：`.agent-base/runs/b0cb29ddb2d948f6bf5e3367ba9fa7ec/result.json`。

封存产物：`.agent-base/runs/b0cb29ddb2d948f6bf5e3367ba9fa7ec/artifacts/artifacts/report.txt`。

文件实际存在，包含 `AGENT_BASE_DEMO_OK`，大小 83 字节，SHA-256 为 `ef31437f98a406d0d9a5290c249dd3e0096ca7d3ce29b20cf0bb29f367ddffed`。

独立检查了文件 hash 与 result.json 一致，没有仅凭模型自述判定通过。本机运行记录位于忽略目录；复制源码后需重新创建自己的示例任务。

## 真实 Web / cron 调度验收

使用 `scripts/create_scheduled_demo.py` 创建独立 Git 仓库 `.demo/scheduled/repo` 和单独的输入工作区 `.demo/scheduled/source`。启动 `agent-base serve --port 8765` 后，执行 `scripts/validate_scheduler.py`，通过真实本机 HTTP 请求激活任务，并等待实际分钟时刻触发 cron；没有使用模拟时钟或 fake CLI。

| 来源 | run_id | 结果 |
| --- | --- | --- |
| Web | `4279c80f4bdf49a1b18154d2fdd85e70` | succeeded / checks_passed |
| cron | `c46e06935e504043adbba85062c70cf4` | succeeded / checks_passed |
| 5 秒时限 | `7d7b8ab011ce46c2b08442f33cc2e4ab` | timed_out，session 保留，CLI 已退出 |

Web 与 cron 均由 dsh 实际调用 summarize_metrics MCP 工具生成 `artifacts/report.json`，独立验证脚本重新读取输入核对每个服务与总计。两份报告都为 **230 次请求、4 次错误**，包含实际触发来源与 key。Web 报告 SHA-256 为 `870ada88c04421222c9edf24424b3e531b026dac7033c3dd3f7b87d943cf48a2`，cron 报告为 `80479090acadb959db32b340a0bf05438d4e72d7efd9543a0701403c73d121ab`。

验证了三个运行各有不同 session；重复 Web key 返回同一 Run；源数据 hash 和源 Git 状态未变；两个成功工作区已删除，归档仍存在；超时工作区保留，按原 PID 与出生时间确认 CLI 不再存活。临时验收 cron 已暂停，未修改系统 crontab。

本机完整收据：`.agent-base/validations/accept-d0ec9f9d/result.json`。产物在对应 `.agent-base/runs/<run_id>/artifacts/artifacts/report.json`。演示仓库、输入和本机收据均位于忽略目录，可通过脚本重建。

## Repo 长期安装 / skills / MCP 真实验收

执行 `scripts/create_resource_demos.py` 创建三个独立 Git repo，每个包含 AGENTS.md、Installation.md、analysis skill、domain MCP、初始化和产物验证脚本。三个 repo 使用相同资源名称但不同计算逻辑。

执行 `scripts/validate_resources.py`，通过真实 dsh 运行四个实例，全部 succeeded / checks_passed：

| 实例 | run_id | MCP 返回结果 | 上游 MCP PID |
| --- | --- | --- | --- |
| finance-1 | `22faf2b6201544199c4e1073e8a6895b` | 求和 10，调用计数 2 | 38545 |
| finance-2 | `7c63faff905b4d148fbeeab456671ce9` | 求和 10，调用计数 1 | 38545 |
| inventory | `8113e3f58de54b1f8a372b248c517909` | 求积 30 | 38865 |
| research | `cd7338b593d645598366e07b6c5b8d75` | 求平均 3.3333333333333335 | 38875 |

每次都在基座事件中核对实际 read_skill、domain__analyze、write_report 调用。finance 两个并发实例共享同一个 MCP PID 和 boot_id，调用计数不同；另外两个 repo 各有独立 MCP 进程和命名空间，未串用结果。

三个安装的 installation-count.txt 均为 1；任务前后共享 skill 的 hash 和 inode 未变；自定义 prompt 不含绝对路径，runtime patch 仅挂载所属 repo namespace。四实例结束后 MCP 网关 PID 仍为 38544、38864、38874；更新 Web 进程后再次核对也未重启这些服务。

本机收据：`.agent-base/validations/resources-1793848182866163000/result.json`；收尾状态：同目录 `final-status.json`。报告在对应 Run 的 artifacts 目录。全部文档路径检查命令 `python scripts/check_prompt_paths.py .` 返回 ok=true。

Web 已登记 finance-demo、inventory-demo、research-demo 三个工作，并展示安装资源清单。三个常驻 MCP 保留运行；没有新增周期 cron。

补充验证 CLI 根标记后，真实 dsh 冒烟 run `af0a5e788ca34d77bdd962634fd38c9a` 同样 succeeded / checks_passed，仍使用原 finance 上游 MCP PID 38545，调用计数增至 3。没有重装技能或重启 MCP。新增的根标记断言也通过三命名空间集成测试。

## 网页对话真实验收

通过 `scripts/validate_chat.py` 对本机 8765 服务执行真实 HTTP 请求，使用真实 dsh，七轮 CLI 调用均正常结束：

- Admin 两轮：第一轮记住随机标记，第二轮只根据原生会话回答；两轮均无工具调用、不读写文件。返回正确标记，native session ID 保持一致；重复 client_key 没有创建新轮次。
- Admin 管理轮：调用实际管理 MCP 查询所有已安装 Agent、skills 和 MCP 状态。接收到 session opening 后主动断开 SSE，随后仍正常完成，证明查看连接断开不取消任务。
- 财务、库存、研究各一轮：实际 read_skill、domain__analyze、write_report，得到正确 repo 的报告；跨命名空间未串用工具，产物可通过会话下载 API 读取。
- 财务第二轮追问：根据原会话回答结果为 10，复用相同 native session 和工作区。
- 三个既有 repo 服务的 boot_id 前后未变，skills 和安装没有被重新创建。

本机完整收据：`.agent-base/validations/chat-1793862715448360000/result.json`。

保留的网页会话完整 ID 见收据；财务会话为 `cb1ec963fae040f5bd45cdb89220b10e`，可在 `/chat#cb1ec963fae040f5bd45cdb89220b10e` 打开。独立记忆验证会话已归档。

另执行四项 Playwright 真实 Chrome 测试，全部通过：多轮历史和 Markdown 安全渲染；刷新恢复且不重复发送；repo 工具卡片、产物下载、切换会话不中断及停止；390px 手机布局和中文输入法回车行为。测试使用独立 8879 服务和 fake CLI，与上面的真实模型验收分开。

实际页面截图在同一收据目录的 `web-chat.png` 和 `web-chat-mobile.png`。只读检查确认页面列出四个 Agent，浏览器没有 JavaScript 运行错误；前端生产构建、Python 编译及全项目提示词路径检查均通过。

## 2026-11-17 并发安装准入修复验证

安装准入改为同一 SQLite 写事务中的幂等查重、活动检查与操作创建；正式新增跨进程竞争、幂等重试和历史列表检查。定向 23 项通过，完整 Python/验收脚本 238 项通过（含此前 18 项验收复现，1 个既有弃用提示），统一服务端到端 28 项通过。前端及浏览器用例未变化，本轮未重跑浏览器，沿用第四轮 19 项通过记录。所有验证使用隔离数据和虚构凭据；未升级/停止现有业务服务，Windows/真实内网仍待现场验收。本轮为用户授权的直接修复及回归。

## 尚未验证

- Windows 真机、CodeAgent 内网、CodeAgent MCP/管理对话接口。
- 远程 MCP 的实际服务和 OAuth，以及所有第三方仓库的安装行为。
- 不可信仓库的操作系统隔离、多租户部署、跨机器恢复。

固定版本 dsh 的 final 事件不含 reason；系统明确记录 legacy_final_exit_code，不声称拿到了不存在的原生字段。原生 MCP 插件可能按可选插件处理启动失败，收据不能替代外部 MCP 上线验收。
