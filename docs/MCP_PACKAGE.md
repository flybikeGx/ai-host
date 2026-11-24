# 第一方 MCP 包：目录、清单与安装契约

本文冻结 AIHO-33 D1 的包结构、清单字段和安装/启用语义，是 D2–D4 迁移与 AIHO-32 新增信息台的规范。D1 只落地目录骨架、清单解析器和加载器原型，不移动任何现有 MCP 业务代码；D2 起按 [FIRST_PARTY_MCP_REFACTOR.md](FIRST_PARTY_MCP_REFACTOR.md) 逐包迁移。

## 1. 目标与边界

**要解决的问题**：一个带人类前端的 Streamable HTTP MCP，如何作为一个可安装、可停用、可卸载、可版本化的单元交付；安装后服务、Agent 工具和人类页面同时出现，停用后同时消失而数据保留。

**本期做**：

- 每个 MCP 一个包目录，后端、MCP server、前端、清单、测试、文档同属一个包；
- 一份同时适用于第一方和第三方的 `mcp.yaml` 清单；
- 包级安装状态（已安装/已启用）与现有服务进程、Agent 授权、前端导航解耦；
- 第一方三个 MCP（知识库、文件中转站、信息台）成为首批实现。

**本期不做**：公开市场、远程包仓库、云端分发、多租户强隔离、不可信代码沙箱、重写三个 MCP 的业务逻辑/数据模型/视觉。

平台边界不变：目录和进程隔离不是安全沙箱，适用于可信团队的单机环境；第三方包按“隔离优先”处理，见 [MCP_FRONTEND.md](MCP_FRONTEND.md)。

## 2. 对象模型

包只描述“是什么、怎么启动、怎么显示”，不替代现有部署与授权对象。

| 对象 | 含义 | 生命周期 |
| --- | --- | --- |
| 包 `mcp-package` | 仓库内一个 MCP 交付单元，清单为 `mcp.yaml` | 随代码版本固定 |
| 安装记录 | `data/mcp/installations.json` 中的一个条目：已安装、已启用、版本/摘要 | 由 `agent-base mcp install/enable/disable` 修改 |
| 服务进程 | 包声明的 Streamable HTTP MCP 进程和私有回环地址 | 仅在“已安装且已启用”时由平台拉起 |
| 前端挂载 | 路由、导航、入口资源、所需权限 | 仅在“已安装且已启用”时出现 |
| MCP 授权 | Agent 能调用哪些工具、哪些项目范围 | 仍由现有绑定/grant 机制管理，包不自行授权 |
| 数据根 | 包的持久数据目录，默认沿用现有位置 | 停用/卸载默认不删除 |

关键不变量：

1. **安装 ≠ 发布 ≠ 授权**。包的“已安装”只表示平台拥有该包并已初始化；“已启用”只表示服务和页面可被拉起/访问。Agent 能否调用工具，始终以现有显式绑定或知识 grant 为准。
2. **停用不删数据**。`disable` 停止服务、撤下导航/页面、停止注入；数据根、审计记录、MCP 工具契约保持不变。
3. **服务名、URL、工具名不变**。迁移只改文件位置和加载方式，不改对外契约。

## 3. 目录规范

```text
first_party/
  README.md
  _shared/                  # 跨包共享，不发布为独立 MCP
    frontend/               # 壳、设计 token、Markdown、API client、测试工具
    python/                 # 清单校验、进程/健康探测公共实现
    tests/
  _template/                # 新包骨架，目录名以下划线开头，不参与运行时发现
    mcp.yaml
    server.py
    frontend/src/index.jsx
    tests/
    docs/
  knowledge/                # id: knowledge
    mcp.yaml
    backend/                # 后端、MCP server、Web 路由、迁移
    frontend/               # 页面与样式
    tests/
    docs/
  file_hub/                 # id: file-hub
    ...
  info_board/               # id: info-board，由 AIHO-32 按模板新增
    ...
```

**命名**：根目录用 `first_party/` 而不是示例里的 `first-party/`，因为 Python 后端需要可导入的顶层包名；包的 `id` 保留连字符（如 `file-hub`），目录名使用可导入形式（`file_hub`）。这条规则由清单校验器强制：目录名必须等于 `id.replace("-", "_")`。

**规则**：

- 一个 MCP 一个子目录；后端、MCP server、前端、测试、文档都在同一目录内，不再散落到 `src/agent_base/` 与 `frontend/src/`；
- 包之间不互相 import；共享代码只进 `_shared`。共享前端代码不得复制一份到包内；
- 包不 import 主应用 `frontend/src` 中的页面；主应用只保留宿主壳、登录和通用 API；D2 起 `_shared/frontend` 承接共享壳/令牌；
- 迁移既有文件一律 `git mv`，保留历史；只允许“移动 + 改 import”，不在同一提交里重写业务逻辑；
- `_shared` 与 `_template` 以下划线开头，加载器不把它们当作可安装包；`_template` 只由 `mcp validate` 和脚手架使用；
- 平台通用代码（`src/agent_base/web.py`、`config.py`、Admin MCP、治理、调度）不搬进包；包通过清单和稳定的平台接口接入。

Python 打包在 D2 调整：`pyproject.toml` 的构建目标同时包含 `src/agent_base` 与 `first_party`，`python -m first_party.knowledge.backend.server` 可直接启动服务。第三方包不进入本仓库的 Python 包，按清单 `runtime.argv` 作为独立进程运行。

## 4. 清单 `mcp.yaml`

每个包根目录恰好一个 `mcp.yaml`，`schema_version: 1`、`kind: mcp-package`。YAML 只描述结构，Markdown 正文不是配置来源；清单出现未知字段即拒绝加载。

### 4.1 完整示例（知识库目标形态）

```yaml
schema_version: 1
kind: mcp-package

id: knowledge                 # 稳定标识；目录名 = id 中的 - 换成 _
title: 知识库
version: 1.0.0
contract: knowledge/v1
description: 项目知识的检索、投稿、审批与版本管理

service:
  transport: streamable_http
  runtime:
    argv:                     # 直接 argv，不经过 shell
      - ${runtime.python}
      - -m
      - first_party.knowledge.backend.server
      - --config
      - ${runtime.config}
      - --data
      - ${service.data}
    endpoint_path: /mcp
    environment: {}           # 值只能是字符串或 {from_env: NAME}
  health:
    path: /health
    expected_tools: [knowledge_catalog, knowledge_search, knowledge_read, knowledge_submit]
    probe_tool:
      tool: knowledge_search
      arguments: {query: "健康探测"}
    startup_timeout_seconds: 60
    interval_seconds: 30
    timeout_seconds: 5
  data:
    dir: knowledge            # 相对状态根；沿用现有 data/knowledge，不搬移
    retention: keep           # keep | purge-allowed
  operations:
    restart_safe: true
    session_sharing: isolated # safe | isolated；知识库按调用者 grant 隔离
    drain_timeout_seconds: 10

frontend:
  mount: spa-module           # spa-module | static-iframe
  route: /knowledge
  navigation: {label: 知识库, icon: book-open, order: 30}
  entry: frontend/src/index.jsx
  requires: [knowledge.read]  # 必须是 permissions.api 的子集
  standalone: false

installation:
  default: installed          # installed | available
  steps: []                   # 可选安装步骤，语义同 v3 installation.steps
  upgrade: restart            # restart | drain-restart | manual
  uninstall: keep-data        # keep-data | purge-allowed
  requires_agent_base: ">=0.1.0"

permissions:
  anonymous: false            # v1 只允许 false
  session: same-origin-cookie # same-origin-cookie | capability-token
  api: [knowledge.read, knowledge.write, knowledge.admin]
  service_tools: [knowledge_catalog, knowledge_search, knowledge_read, knowledge_submit]
```

### 4.2 顶层字段

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `schema_version` | 是 | 固定 `1` |
| `kind` | 是 | 固定 `mcp-package` |
| `id` | 是 | `[a-z][a-z0-9-]{0,63}`；全局唯一；目录名 = `id` 中 `-`→`_` |
| `title` | 是 | 1–64 字符，导航/CLI 显示名 |
| `version` | 是 | 形如 `1.0.0` 的包版本；升级必须显式 |
| `contract` | 是 | 形如 `knowledge/v1`；工具契约版本 |
| `description` | 否 | 1–200 字符，仅人类可读 |
| `service` | 是 | 服务定义，见 4.3 |
| `frontend` | 否 | 省略表示无人类前端，见 4.4 |
| `installation` | 否 | 安装策略，见 4.5；缺省按“需显式安装、保留数据” |
| `permissions` | 是 | 权限边界，见 4.6 |

### 4.3 `service`

| 字段 | 规则 |
| --- | --- |
| `transport` | `streamable_http` 或 `stdio`；带 `frontend` 的包必须是 `streamable_http` |
| `runtime.argv` | 非空字符串数组；只允许占位符 `${runtime.python}`、`${runtime.config}`、`${package.root}`、`${service.data}`、`${service.port}`；不允许 shell 字符串 |
| `runtime.endpoint_path` | 可省略，默认 `/mcp`；`/` 开头的简单路径 |
| `runtime.environment` | 字符串或 `{from_env: NAME}`；秘密值只引用变量名 |
| `health.path` | 可省略，默认 `/health`；平台用包内控制凭证探测 |
| `health.expected_tools` | 工具名数组，不得重复；用于协议/业务验证 |
| `health.probe_tool` | 只读探针；必须属于 `expected_tools` |
| `health.*_seconds` | 正数；启动、间隔、超时 |
| `data.dir` | 相对状态根的目录名，不含 `/` 或 `..`；默认等于 `id` |
| `data.retention` | `keep` 或 `purge-allowed`；`keep` 时卸载也不删数据 |
| `operations.restart_safe` | 布尔；是否允许监督器自动重启 |
| `operations.session_sharing` | `safe` 或 `isolated`；共享 stdio 会话必须为 `safe` |
| `operations.drain_timeout_seconds` | 正数 |

`${service.port}` 可选用：新包由平台分配私有回环端口；知识库迁移期沿用现有 `service.json` 自报端口，D2 收敛到统一端口注入。`endpoint_path` 和 `health.path` 由平台网关转发，不直接暴露到非回环地址。

### 4.4 `frontend`

| 字段 | 规则 |
| --- | --- |
| `mount` | `spa-module`（可信第一方，编译进主 SPA）或 `static-iframe`（第三方/隔离，独立静态包 + sandbox iframe） |
| `route` | 可见页面路由，如 `/knowledge`；不得占用平台保留路由 `/chat`、`/cron`、`/dashboard`、`/services`、`/login`、`/workbench`、`/admin`、`/api`、`/mcp-ui`；全局唯一 |
| `navigation.label` | 侧栏显示名，1–32 字符 |
| `navigation.icon` | lucide 图标名（小写连字符）；未知图标回退通用图标，不阻止安装 |
| `navigation.order` | 整数 0–999，越小越靠前；缺省 100 |
| `entry` | `spa-module` 必填；包内相对路径，构建时存在 |
| `assets` | `static-iframe` 必填；包内构建产物目录，构建前可不存在 |
| `requires` | 页面所需 API 权限，必须是 `permissions.api` 的子集 |
| `standalone` | `spa-module` 必须为 false（复用主应用壳）；`static-iframe` 必须为 true（包内自带完整页面） |

`spa-module` 与 `static-iframe` 的构建、挂载、登录态和安全边界见 [MCP_FRONTEND.md](MCP_FRONTEND.md)。清单选择挂载方式，不允许第一方使用未声明的“特例”。

### 4.5 `installation`

| 字段 | 规则 |
| --- | --- |
| `default` | `installed`（升级时自动视为已安装，现有三个 MCP 用此项）或 `available`（需显式安装） |
| `steps` | 可选数组，元素为 `{argv, timeout_seconds}`；占位符同 4.3，只允许安装期使用的 `${runtime.python}`、`${runtime.config}`、`${package.root}`、`${service.data}` |
| `upgrade` | `restart`（停进程→换版本→启动）、`drain-restart`（排空在途调用）或 `manual`（只提示，不自动升级） |
| `uninstall` | `keep-data`（默认）或 `purge-allowed`（允许显式 `--purge-data`，仍需二次确认） |
| `requires_agent_base` | 可选简单版本约束；不满足时拒绝安装并给出当前版本 |

安装步骤失败保留已生成的数据和日志，不自动重试；未知结果必须显式检查后重装。重复 `install` 使用同一版本/摘要时幂等，返回已有记录。

### 4.6 `permissions`

| 字段 | 规则 |
| --- | --- |
| `anonymous` | v1 必须为 false；匿名访问留到有独立鉴权模型时再加 |
| `session` | `same-origin-cookie`（第一方，同源 HttpOnly 会话）或 `capability-token`（第三方 iframe，短期能力票据） |
| `api` | 页面/管理 API 权限名数组，如 `knowledge.read`、`file.write`、`quota.manage`；与现有治理权限同名 |
| `service_tools` | MCP 工具名数组；是能力声明，不是授权；实际调用仍按现有绑定/grant 校验 |

前端永远不直接访问数据根或数据库，只经同源 HTTP API；服务凭证、SQLite 文件、blob 目录不得进入浏览器。第三方包不得读取主应用 cookie 或本地存储。

## 5. 安装状态与生命周期

状态文件：`<state_root>/mcp/installations.json`（默认 `.agent-base/mcp/installations.json`），结构：

```json
{
  "schema_version": 1,
  "revision": 3,
  "packages": {
    "knowledge": {
      "installed": true,
      "enabled": true,
      "version": "1.0.0",
      "manifest_digest": "…",
      "source": "legacy",
      "updated_at": 1764000000
    }
  }
}
```

写入使用临时文件 + `os.replace` 并在锁内完成；读取失败时按“缺省状态”处理并保留坏文件，不静默清空。状态条目只记录包级事实，不记录 PID、端口或授权。

**缺省推导（兼容迁移的关键）**：状态中没有条目时，`installation.default: installed` 的包视为“已安装 + 已启用”，来源记为 `default`；`available` 的包视为未安装。因此现有知识库/文件中转站无需先跑迁移命令，升级后即保持可用；`install` 只需补写记录。若记录的 `version`/`manifest_digest` 与当前清单不同，状态标记 `stale: true`，`enabled` 仍保持原值，由显式 upgrade 决定何时切换。

| 操作 | 前置 | 效果 |
| --- | --- | --- |
| `install <id>` | 包清单有效 | 写安装记录；执行 `installation.steps`；初始化数据根；默认同时启用（`--no-enable` 可保持停用）；不改变授权 |
| `enable <id>` | 已安装 | 允许启动服务、注入 Agent、显示导航/页面；已在运行则幂等 |
| `disable <id>` | 已安装 | 停止服务、撤下导航/页面、停止注入；写完状态后再返回；数据保留 |
| `uninstall <id>` | 任意 | 停服务、移除安装记录/导航；默认保留数据；`purge-allowed` 才接受 `--purge-data` |
| `upgrade <id>` | 已安装 | 校验新版本/摘要，按 `installation.upgrade` 停旧起新；失败保留旧记录和日志 |

**已安装的旧 MCP**：D3 首次启动时，若数据根/旧服务标记存在且状态文件缺条目，只补写 `{installed: true, enabled: true, source: "legacy"}`，不移动数据、不改 URL、不改服务名。状态文件删除后可回退到缺省推导，等同迁移前行为。

**重复安装与并发**：同一 `id`、版本和摘要的重复 `install` 返回已有记录；不同摘要视为升级，必须显式。安装/启停/升级共用同一文件锁，跨进程最多一个写者。

## 6. 服务生命周期

- 只在“已安装 + 已启用”时启动；`disable`/`uninstall` 停止进程并移除 `service.json`，但保留数据目录和日志末尾；
- 平台按 `runtime.argv` 展开占位符，注入私有回环端口（如使用 `${service.port}`），以独立进程组启动，日志写 `<data>/<id>/service.log`；
- 健康检查三层：进程存活 → `health.path`（包内控制凭证）→ MCP 协议 `initialize`/`tools/list` 与 `probe_tool`；任一层失败不标记为可用；
- 启动失败不自动重试，保留日志和状态；不健康的活进程不静默替换，需显式重启；
- `serve`/`worker` 启动时只拉起已启用的包；Web 重启不重启已停用的包；
- 同一包最多一个活动实例；跨进程/跨入口（CLI、Web、worker）共享同一数据根和锁；
- 数据根默认沿用现有位置（知识库 `data/knowledge`、文件枢纽 `data/file-hub`），迁移期不得双写。

D1 只定义接口与状态；进程管理在 D2（知识库试点）实现，D3 推广到文件枢纽并接入 `serve`/`worker`。

## 7. 前端挂载与安装后的可见效果

清单中的 `frontend` 是唯一的导航/路由声明来源；主应用不再硬编码页面列表。页面/导航门控与 `enabled` 一致，判定在服务端完成，不能只靠前端隐藏。

| 状态 | 服务 | Agent 注入 | 导航/页面 | 数据 |
| --- | --- | --- | --- | --- |
| 未安装 | 不启动 | 不注入 | 不显示；直接访问路由 404 | 不创建 |
| 已安装 + 停用 | 停止 | 不注入 | 不显示；路由 404 | 保留 |
| 已安装 + 启用 | 运行 | 按既有绑定注入 | 显示；路由可访问 | 保留 |
| 升级中 | 按策略排空/重启 | 暂不可用或按旧版本 | 可显示维护态 | 保留 |

## 8. 权限与安全边界

- **授权不搬家**：Agent 工具授权继续由 `ServiceCatalog` 绑定、知识 grant、文件枢纽 scope 管理；包清单只声明“需要什么能力”，不直接授予。
- **会话**：第一方前端使用同源 HttpOnly cookie（`agent_base_session`），沿用 `web_auth`/`AuthShell` 的登录与工作区校验；第三方使用短期能力票据，见 MCP_FRONTEND。
- **权限名**：`permissions.api` 使用现有治理权限名（`file.read` 等）或新增的包内只读/写权限；服务端按路由逐项校验，前端隐藏按钮不作为安全边界。
- **凭证**：服务控制凭证由平台生成，放 `<data>/service-token`（0600），只用于健康探测和平台→服务调用；不进入前端、日志、清单或资源目录。
- **审计**：沿用现有审计表/命名空间；安装、启停、卸载、权限变更都记录 actor 和结果，不记录正文/凭证。迁移前后同一业务动作的审计字段保持一致。
- **文件系统**：清单中的 `entry`、`assets`、`steps.argv` 路径必须落在包内；`data.dir` 只能落在状态根下；拒绝绝对路径和 `..`。
- **第三方**：默认 `capability-token` + iframe sandbox，禁止同源 cookie、直接读库和任意网络出口；具体边界见 MCP_FRONTEND。

## 9. 与现有清单和对象的关系

| 现有对象 | 关系 |
| --- | --- |
| `agent.yaml` / `Installation.md`（`schema_version` 1/2/3，`kind: agent`） | 描述 Agent 仓库，不变；第一方 MCP 包不占用该清单 |
| `kind: mcp-service`（`schema_version: 3`） | 描述可被共享发布/绑定的服务包；第一方包在 D3 之后可选择把 `service` 段映射为一次内置部署，但包状态与发布/绑定仍是两个对象 |
| `ServiceCatalog` / `ServiceStore` | 继续管理共享部署、发布、显式绑定和权限；包清单的 `service_tools` 只是能力声明 |
| 治理 RBAC（`governance/`） | 继续管理账号、域、角色、权限；`permissions.api` 复用其权限名 |
| `MCPDiscovery` | 继续输出 MCP 端点清单；D3 起 `agent-base mcp list` 增加 `packages` 段，`/api/mcp` 仍以 `servers` 为主 |

**为什么不再用一份 v3 `mcp-service` 清单**：v3 描述“共享服务部署”，缺少前端、包级启停、包目录与导入边界；本清单在其上补包层，字段命名（`service`/`runtime`/`health`/`operations`/`installation.steps`）尽量与 v3 保持一致，便于 D3 把 `service` 段交给同一套进程/健康实现。转换规则在 D2 评审时以实际代码为准。

## 10. 命令与加载器原型（D1 已落地）

D1 只提供只读入口，`install/enable/disable` 在 D3 接线，避免出现“命令成功但服务没接上”的半成品：

```sh
agent-base mcp packages            # 列出包清单、安装/启用状态和数据目录
agent-base mcp validate [path]     # 校验单个包或 first_party 根目录，只读
agent-base mcp list                # 保留现有端点清单；D3 起追加 packages 摘要
```

解析器/加载器位于 `src/agent_base/mcp_packages.py`：

- `load_package(path)`：解析并校验一个 `mcp.yaml`；
- `discover(root)`：扫描 `first_party/*/mcp.yaml`，跳过下划线目录，保证 id/路由唯一；
- `PackageRegistry`：合并清单与安装状态，输出包摘要、导航项和前端入口；
- `Installations`：`install/enable/disable/uninstall/upgrade` 的状态语义，D3 接服务生命周期；
- `expand_runtime`：占位符展开。

D3 定稿命令名（评审确认）：

```sh
agent-base mcp install <id> [--version <v>]
agent-base mcp enable <id>
agent-base mcp disable <id>
agent-base mcp uninstall <id> [--purge-data]
```

`mcp list` 继续输出 `servers`，并追加 `packages`；`mcp packages` 作为只显示包的稳定子命令保留。

## 11. D1 评审确认点

1. **根目录命名**：采用 `first_party/`（Python 可导入）而非 `first-party/`；子目录用 `file_hub`，`id` 仍为 `file-hub`。
2. **前端挂载**：第一方 `spa-module`、第三方 `static-iframe`，是否接受这一分层；详见 MCP_FRONTEND。
3. **命令命名**：`agent-base mcp install/enable/disable/uninstall` 与保留的 `mcp list`/`mcp config` 共存是否可接受。
4. **安装状态文件**：`data/mcp/installations.json` + 缺省推导（旧三个自动视为已安装）是否作为唯一迁移机制。

确认后进入 D2：只迁移知识库，跑通“包目录 + 清单 + 服务启停 + 前端挂载 + URL/数据/契约不变”。
