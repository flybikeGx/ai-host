> 历史审查：以下结论绑定文中提交基线。之后的账号登录接入见 [网页指南](WebChat.md)，不要把历史发现当作当前复验结果。

# 多用户场景正交清单与角色化审查

> 审查基线：`88662cf`（2026-11-18，gitee/main；含治理层 3 个提交）。
> 审查范围：每个页面、HTTP 入口、MCP 入口与 CLI，逐项检查「身份 → 角色/授权 → 域/数据范围 → 资源归属 → 并发控制 → 审计」六个正交维度。
> 文档性质：as-built 审查清单，不是设计提案；结论以当前代码为准，不修改实现。
> 图例：✅ 已按用户/角色或已有并发保护；⚠️ 仅单 token、条件成立或存在明显限制；❌ 无用户维度、可横向越权或会串扰。

## 0. 一句话结论

**目前只有治理控制面 `/api/v1` 做到了「每个请求携带用户身份」，其中一部分路由按 RBAC 校验；承载页面的 `/api/*` 仍是单一共享 token，执行链路上的身份是平台 Agent（`repo:<hash>` / `admin`）或 Run，不是自然人用户。** 因此页面和功能点基本没有按角色展开；多个用户调用同一个 Agent 时，工作区和 dsh 会话不会互相串写，但业务数据、授权范围、取消/停止、知识投稿和审计归属会互相影响，且存在明确的横向越权和吞吐瓶颈。

## 1. 判定口径

| 维度 | 问题 | 本清单的判定 |
| --- | --- | --- |
| 身份 | 请求能否说明「是谁」 | 无身份 / 共享 token / JWT 用户 / 平台 Agent 身份 / Run 身份 |
| 角色 | 谁被允许做什么 | 无 / token 全权 / RBAC（全局/域/组）/ 绑定白名单 |
| 域与数据范围 | 只能看到哪一份数据 | 无 / installation / deployment / knowledge namespace / governance domain |
| 归属 | 谁能读、改、取消、删除 | 无 owner / conversation / run / work / installation |
| 并发 | 同时发生时会怎样 | 无保护 / 队列 / 互斥锁 / 串行化 / 幂等键 / 排空 |
| 审计 | 能否回答「谁做了什么」 | 无 / run 事件 / 平台审计 / 治理审计（user_id + trace_id） |

「正交」在这里指：身份、授权、租户、归属、并发、审计六个维度各自独立、逐层收紧；任一维度不能靠另一个维度“顺带”保证（例如用目录隔离代替用户授权，或用全局队列代替资源归属）。

## 2. 身份链现状

```text
浏览器页面 / 外部 MCP 客户端
  ├─ /api/*（页面数据面）      共享 AGENT_BASE_WEB_TOKEN（可空）──→ 无用户身份
  ├─ /api/v1/*（治理控制面）   Bearer JWT(user) + x-domain-id ──→ RBAC(用户/域/组)
  └─ MCP / 执行面              安装 token / per-run grant / knowledge grant
                               ──→ 平台 Agent 身份（admin / repo:<repo_id>）或 Run
```

三个关键事实：

1. **`/api/v1` 之外没有用户身份**。`/api/chat/targets`、`/api/conversations`、`/api/dashboard`、`/api/services`、`/api/works`、`/api/runs` 等页面数据接口只挂 `dependencies=api`，即一个可选的共享 Bearer token（见 `src/agent_base/web.py:165-337`）；知识库接口另走一次性 ticket/管理员会话（见 4.4）。
2. **用户身份不传播到执行面**。dsh 通过 `repo_server` 的静态安装 token + `X-Agent-Run` 调用业务工具；per-run 共享服务 grant 的 definition 只有 `agent_id / deployment_id / alias / tools / binding_revision / scope / pid / run_id / turn_id`，没有用户（会话 grant 连 pid/run/turn 也没有，见 `src/agent_base/service_resolver.py:124-132`、`src/agent_base/mcp_discovery.py:344-346`）；知识库 grant 的 actor 是 `repo:<repo_id>` 或 `admin`，不是用户（见 `src/agent_base/knowledge.py:86-90`）。
3. **治理网关/注册中心是另一条独立服务面**。`/api/v1/gateway/*` 和 `/api/v1/registry/*` 按用户做 RBAC 和审计，但它不经过 agent-base 的 Run/对话/共享部署链路；治理面与执行面目前只共享进程和数据目录，不共享身份（见 `src/agent_base/web.py:122-123`、`src/agent_base/governance/web.py:113-120`）。

因此「同事已经让每个 request 说明自己的身份和角色」需要限定为：**`/api/v1` 的请求说明了；页面和 Agent 执行请求没有。**

## 3. 页面/功能点总览

| 页面/入口 | 主要接口 | 当前身份 | 当前授权 | 数据范围 | 归属/审计 | 并发保护 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/chat` 对话 | `/api/conversations*`、`/api/chat/targets` | 共享 token | 无角色，全量 | 全部会话/全部 Agent | 无 owner；事件无用户 | 每会话 1 轮活跃、全局 32 活跃轮次、repo 轮次全局 claim 2 | ❌ |
| `/dashboard` 仪表盘 | `/api/dashboard`、`/api/runs/{id}/report` | 共享 token | 无角色，全量只读 | 全部进程/工作区/运行 | 无 owner | 只读快照 | ❌ |
| `/services` 共享服务 | `/api/services*` | 共享 token | 无角色，全量管理 | 全部 deployment/binding | 绑定主体是 Agent，不是用户 | 部署锁、stdio 串行、v3 排空 | ❌ |
| `/knowledge` 知识库 | `/api/knowledge/*` | 管理 ticket/session 或共享 token | 单管理员，无角色 | 全部 namespace | 投稿 actor 是 repo/admin | SQLite WAL；无用户锁 | ❌ |
| `/cron` 定时任务 | `/api/schedules*` | 共享 token | 无角色，全量 | 全部计划/运行 | 无 owner | 调度器 0.5s 轮询、trigger 幂等 | ❌ |
| `/workbench` 工作台 | `/api/works*`、`/api/repos*`、`/api/runs*` | 共享 token | 无角色，全量 | 全部工作/安装/运行 | 无 owner | 队列/全局并发；stop 无在途检查 | ❌ |
| 治理控制面 `/api/v1` | auth/me/domains/users/roles/audit | JWT 用户 | RBAC（部分路由仅登录） | 全局或 `x-domain-id` | user_id + trace_id | SQLite WAL；无请求限流 | ⚠️ |
| 治理注册中心/网关 | `/api/v1/registry/*`、`/api/v1/gateway/*` | JWT 用户 | RBAC + 对象/域检查不完整 | 注册表全局 | 用户审计 | 每次调用新建 MCP 会话/进程 | ⚠️ |
| CLI / Admin MCP | `agent-base admin/control/...` | 本机 OS 用户 | 本机全权 | 本机全部数据 | 无平台用户 | 本机单用户假设 | ❌ |
| repo 命名空间 MCP | Streamable HTTP + `X-Agent-Run` | 安装 token | 安装级静态 token | 单个 installation | Run 事件，无用户 | 上游 stdio 会话按 server 串行 | ⚠️ |
| 共享部署网关 | `/mcp` + grant | per-agent grant | 绑定工具/项目范围 | 单个 deployment | grant 无用户 | stdio 串行、HTTP 并发、in-flight 排空 | ⚠️ |
| 知识库 MCP | Streamable HTTP + grant | 知识 grant | 读/投稿 namespace 范围 | namespace | actor=repo/admin | SQLite WAL，写事务 | ❌ |
| MCP 发现 | `/api/mcp*`、`/.well-known/mcp.json` | 共享 token / 公开 | 无角色 | 全部 MCP | 无审计 | 只读 | ❌ |

### 3.1 页面 × 角色（应然 vs 现状）

> 应然列按 `BUILTIN_ROLES` 的权限名推导，用于发现“页面动作没有对应权限”的错位；当前实现没有执行这张矩阵——**除 `/api/v1` 外，页面仍是同一 token 全权**。

| 页面/功能点 | viewer | developer | domain_admin | auditor | platform_admin | 现状 |
| --- | --- | --- | --- | --- | --- | --- |
| 对话：查看历史 | ✅ `agent.read` | ✅ | ✅ | ✅ | ✅ | 同一 token |
| 对话：发起 repo 对话 | ❌ 无 `agent.run` | ✅ | ✅ | ❌ | ✅ | 同一 token |
| 对话：Admin 对话 | ❌ | ❌ | ❌ | ❌ | ✅（建议仅平台管理员） | 同一 token |
| 仪表盘 | ✅ `resource.read` | ✅ | ✅ | ✅ | ✅ | 同一 token |
| 服务：浏览 | ✅ `service.read` | ✅ | ✅ | ✅ | ✅ | 同一 token |
| 服务：发布/绑定/启停 | ❌ | ❌ | ✅ `service.manage` | ❌ | ✅ | 同一 token |
| 知识：浏览 | ✅ `file.read` | ✅ | ✅ | ✅ | ✅ | 同一 token/公开读 |
| 知识：投稿 | ❌ 无 `task.write` | ✅ | ✅ | ❌ | ✅ | 同一 token |
| 知识：审批 | ❌ | ❌ | ❌ | ❌ | ✅（当前单管理员） | 管理 ticket |
| 定时：查看 | ✅ `task.read` | ✅ | ✅ | ✅ | ✅ | 同一 token |
| 定时：创建/触发 | ❌ | ✅ `task.write`+`task.run` | ✅ | ❌ | ✅ | 同一 token |
| 工作台/Repo 管理 | ❌ | ❌ | ⚠️ 仅 `service.manage`，Repo 安装无对应权限 | ❌ | ✅ | 同一 token |
| 用户/角色/域 | ❌ | ❌ | ⚠️ 本域 `user.manage`/`role.manage`，无 `domain.manage` | ❌ | ✅ | 同一 token |
| 审计 | ❌ | ❌ | ✅ 本域 `audit.read` | ✅ 跨域 | ✅ | 同一 token |
| 注册中心/网关 | ✅ 读 | ✅ 读+`tool.execute` | ✅ | ✅ 读 | ✅ | `/api/v1` 已按 RBAC；页面无入口 |

结论：**页面与权限模型没有对齐**。要么给页面补上权限映射（并统一走 JWT），要么明确这些页面只属于“平台管理员/本机管理员”，不向普通角色开放。

## 4. 逐页面/功能点审查

页面 HTML（`/chat`、`/dashboard`、`/services`、`/knowledge`、`/cron`）本身不做身份校验，真正的数据访问发生在页面里的 `fetch('/api/...')`；因此“页面能打开”不等于“能读取数据”。

### 4.1 `/chat` 对话

- **身份/角色**：无用户。任何持有共享 token（未配置 token 时本机任意进程）的调用者都能列出全部会话、读取历史、发消息、停止生成、重命名/归档和下载产物（`src/agent_base/web.py:173-230`）。`/api/chat/targets` 返回全部可用 Agent，没有按角色过滤。
- **归属**：`conversations` 与 `conversation_turns` 表没有 `user_id`（`src/agent_base/store.py:52-67`）；`list()` 返回最近 200 个会话，不区分创建者。取消/停止只校验会话存在，不校验调用者。
- **并发**：同一会话有唯一活跃轮次约束（`conversations.py:179`），全局活跃轮次上限 32（`conversations.py:181`）；repo 对话每轮创建 Run，并受全局 `max_concurrent=2` 的 FIFO claim 限制（`conversations.py:291`、`store.py:146-158`）。**Admin 对话没有 Run，因此不占用该并发额度**，理论上可同时跑满 32 轮。
- **结论**：❌ 无身份、无归属；同一会话被两个用户共享时，两人可轮流发消息、互相停止、看到同一份历史。工作区按会话隔离，不会串写。

### 4.2 `/dashboard` 仪表盘

- **身份/角色**：共享 token；只读。
- **数据范围**：返回全平台 dsh 实例、工作区、MCP/网关、队列与异常（`/api/dashboard`）。没有按用户/域过滤；任何 token 持有者可看到其他用户的任务名、工作区路径和磁盘占用。
- **结论**：❌ 单用户只读面板；不构成多用户可见性边界。

### 4.3 `/services` 共享服务

- **身份/角色**：共享 token，接口全部是 `dependencies=api`（`src/agent_base/service_web.py:117-190`）。没有“谁能发布/绑定/停止”的角色判定。
- **归属**：绑定主体是 `agent_id`（平台 Agent 身份），API 直接接收 `agent_id`（`src/agent_base/service_catalog.py:183-190`）；没有用户作为授权主体。任何 token 持有者可以给任意 Agent 绑定/解绑、发布/撤销、启停/维护、看日志。
- **并发**：部署有安装锁和代际检查；v3 stop 默认排空；stdio 后端 `session_lock` 串行（`deployment_gateway.py:402`），HTTP 代理并发。多用户共享同一部署时，一个用户解除绑定会立即让其他用户的新调用失效（`revoke_grants` 按 deployment+agent 撤销）。
- **结论**：❌ 平台级管理页；不是“按用户/域授权”的页面。

### 4.4 `/knowledge` 知识库

- **身份/角色**：读接口默认对未认证浏览器开放；设置 `AGENT_BASE_WEB_TOKEN` 时改为要求管理员会话。写接口要求一次性 admin ticket 建立的 12 小时 HttpOnly 会话，或该共享 token（`knowledge_web.py:36-58`）。没有用户/角色，只有“是否管理员”。
- **归属**：绑定表 `bindings(actor TEXT PRIMARY KEY)`，actor 仅允许 `admin|repo:<hash>|agent:...`（`knowledge.py:86`、`knowledge.py:167-170`）；投稿的 `actor` 是 repo/admin，`source` 记录 run/turn/install（`knowledge.py:350-359`）。同一 repo 的所有用户共享同一个 actor，因此无法区分“谁写的”，也无法按用户隔离草稿。
- **本机边界**：任何能执行 CLI 的本机 OS 用户都可以运行 `agent-base open` 取得一次性管理连接；ticket 不绑定 OS 用户，也不区分平台用户。
- **并发**：SQLite WAL、`BEGIN IMMEDIATE` 写事务（`knowledge.py:102-115`）；并发写会等待 10 秒后可能报锁冲突，没有用户级重试/排队。
- **结论**：❌ 单管理员模型；跨用户草稿可见、审批与审计无法归因到人。

### 4.5 `/cron` 定时任务

- **身份/角色**：共享 token；`/api/schedules*` 无角色判定（`src/agent_base/web.py:284-311`）。
- **归属**：`works` 表没有 owner（`store.py:37-40`）；任何 token 持有者可创建/暂停/恢复/触发任意计划、查看任意运行报告。
- **并发**：调度器 0.5 秒轮询、trigger 按 `(work_id,key)` 幂等；全局并发与 repo 对话共享 `max_concurrent`。一个用户可注册大量计划并占满队列（队列上限 100）。
- **结论**：❌ 无归属；会与其他用户争抢全局执行额度。

### 4.6 `/workbench` 工作台（works / repos / runs）

- **身份/角色**：共享 token；`/api/works*`、`/api/repos*`、`/api/runs*` 全部无角色（`src/agent_base/web.py:235-337`）。
- **归属**：`works`、`repo_installations`、`runs` 均无 owner 字段；任意 token 持有者可安装/停止任意 repo、取消任意 Run、读取任意报告/事件。
- **并发**：`repo stop` 直接 `kill_tree`（`repositories.py:244-249`），不检查该安装是否有在途对话/工具调用，会打断其他用户；`repo install` 新 commit 生成新 installation，不覆盖旧版本，但旧安装仍可被任意人停止。
- **结论**：❌ 平台运维台；多用户场景下是高风险入口。

### 4.7 治理控制面 `/api/v1`

- **身份**：JWT（`sub=user_id`，`jti` 仅用于 trace），`control.secret` 文件 0600；人员（`user_type=person`）token TTL 20 分钟；非 person 的服务账号 30 天（`governance/service.py:126-133`）。
- **授权**：`permission(perm)` 从 `x-domain-id` 取域，调用 `Governance.authorize`；有效权限 = 角色并集 ∪ 直授 allow − deny（deny 优先，`rbac.py`）。域作用域靠 SQL `domain_id IS NULL OR domain_id=?` 收窄（`store.py:470-496`）。
- **不够角色化的地方**：
  - `GET /api/v1/me` 只返回 `effective_permissions(sub, None)`，即**全局**权限；域内角色不会出现在 `me` 里（`governance/web.py:162-169`），前端无法据此渲染“本域可做什么”。
  - `GET /api/v1/domains` 只要求登录，返回全部域（`web.py:175-177`）；`GET /api/v1/roles`、`/api/v1/groups` 也只要求登录（`web.py:209-220`）。
  - `GET /api/v1/users` 要求 `user.manage`，但 `list_users()` 返回全平台用户，不按调用者域过滤（`web.py:192-194`）；`PATCH /users/{id}` 也不校验目标用户所属域。
  - `x-domain-id` 由客户端提供，只参与权限 SQL 收窄，不校验调用者是否是该域成员；membership 与角色授权是两套数据，`user_domains()` 实际未被授权逻辑使用（`rbac.py:25-29`）。
  - 停用用户不会让已签发 JWT 立即失效：`current_user` 只验签和过期，不查用户状态；也没有 jti 撤销表（`governance/web.py:122-128`、`service.py:135-136`）。
- **并发**：每个请求新建 SQLite 连接（timeout 30s），审计写同库；没有请求级限流；登录失败计数是“读-改-写”，并发尝试可能绕过 5 次锁定。
- **结论**：⚠️ 有用户身份和 RBAC，但对象级域校验、撤销语义、前端可见性和登录并发仍需补齐；它还没有接管页面请求。

### 4.8 治理注册中心与中央网关

- **身份/授权**：JWT + RBAC；`gateway.call` 按目标服务域校验 `tool.execute`、带 `x-domain-id` 时额外做越域比对、校验工具目录；`relay` 校验 `service.read`/`tool.execute` 并剥离用户 token、注入服务凭据（`gateway.py:37-124`）。
- **对象级域检查不完整**：`GET /registry/services/{id}`、`PATCH`、`DELETE`、`/tools`、`/check`、`/server-json` 只做 `permission()`（按请求头域）而不校验目标 `service.domain_id`；持有 A 域 `service.read` 的用户可在 B 域对象上读/改（`governance/web.py:278-307`）。
- **并发/资源**：`ServiceClient.call_tool()` 每次调用都新建一个 MCP 会话；embedded 模式即每次 `stdio_client` 启动一个子进程（`registry.py:72-97`）。N 个用户并发调用 = N 个进程，没有连接池/并发上限；`relay` 也没有速率限制。
- **与执行层的关系**：这是独立的“治理服务目录”，与 agent-base 的共享部署/每安装网关不是同一套对象；`docs/ARCHITECTURE.md` §13 仍把“网关、注册中心”列为愿景，和代码现状不一致。
- **结论**：⚠️ 是真正的用户级 RBAC 入口，但只覆盖治理面自身；尚未接管 Agent 执行流量。

### 4.9 CLI 与 Admin MCP

- **身份**：本机 OS 用户；`agent-base admin` 和管理 MCP（stdio）拥有全部管理工具，没有平台用户/角色概念。
- **行为**：可以安装/运行/取消/停止任意 repo、服务、知识库，读取任意运行目录；`agent-base control init` 也能创建平台管理员。两者都只依赖本机 OS 权限，这是单用户本机信任边界。
- **结论**：❌ 多用户场景下不能把本机 shell/MCP 暴露给不同用户；Admin MCP 也不是按角色裁剪的工具集。

### 4.10 MCP 执行面（repo / 共享部署 / 知识库 / 发现）

- **repo 命名空间**：安装 token 是静态 Bearer，`X-Agent-Run` 只用于工作区路由和 Run 生命周期校验（`repo_server.py:86-100`）；token 不区分用户，任何拿到 token 的调用者可在该安装的任意活跃 Run 工作区执行工具。stdio 上游按 server 串行（`repo_server.py:122-131`）。
- **共享部署**：per-run grant 定义无用户；网关只校验 grant 哈希、绑定存活性、工具白名单、项目范围（`deployment_gateway.py`）。stdio 会话串行，HTTP 并发；`in_flight` 用于排空。解绑/撤发布立即撤销该 Agent 的全部 grant，影响所有用户。
- **知识库**：grant 的 actor 是 repo/admin；`knowledge_submission_list` 只按 actor 过滤，因此同一 repo 的用户共享草稿与投稿列表。
- **发现 API**：`/api/mcp` 需共享 token；`POST /api/mcp/grants` 任何 token 持有者都能为任意有绑定的 Agent 签发 TTL 会话授权（`mcp_discovery.py:341-351`），没有用户/角色校验；`/.well-known/mcp.json` 无认证，暴露当前端点（仅 loopback）。
- **结论**：❌（repo/知识库）与 ⚠️（共享部署/发现）：执行面身份是“Agent + Run”，不是“人”。

## 5. 重点：多个用户调用同一个 Agent 的并发分析

### 5.1 调用链

```text
用户 A ─┐
        ├─ /api/conversations/{chat}/messages → 每轮一个 Run
用户 B ─┘        │
                 ├─ conversations/<chat_id>/workspace   （每会话独立）
                 ├─ conversations/<chat_id>/dsh-home    （每会话独立 native session）
                 └─ 全局队列 claim(max_concurrent=2) → dsh 子进程
                          │
                          └─ repo_server（每 installation 一个常驻网关）
                               ├─ X-Agent-Run → Run 工作区
                               └─ 共享 stdio 上游会话 + per-server lock
```

### 5.2 共享资源与隔离矩阵

| 共享对象 | 隔离/保护机制 | 多用户风险 | 判定 |
| --- | --- | --- | --- |
| 安装源码、skills、env | 固定安装版本，只读挂载 | 无（同一版本共享，升级不自动跟随） | ✅ |
| 会话工作区 | `conversations/<chat_id>/workspace` | 不同会话隔离；同一会话被多人共享时无 owner | ⚠️ |
| dsh 原生 session | 每会话独立 `dsh-home`，一轮一个进程 | 同一会话仍可被他人续写；无用户身份 | ⚠️ |
| 全局执行队列 | `claim(max_concurrent=2)` FIFO | 一个用户可占满 2 个额度；无按用户配额/公平性 | ⚠️ |
| Admin 对话 | 不创建 Run，不占 `max_concurrent` | 最多 32 个活跃轮次同时跑 dsh，可能压垮本机 | ⚠️ |
| repo 上游 stdio MCP | 每 server 一个 `asyncio.Lock` 串行 | 长调用阻塞其他用户；无按用户超时/公平性 | ⚠️ |
| 部署 stdio 网关 | `session_lock` 串行；`in_flight` 排空 | 同上；停止/升级会中断在途调用 | ⚠️ |
| 部署 HTTP 上游 | 并发代理 | 上游限流/凭据共享；无用户级配额 | ⚠️ |
| 业务数据目录 | deployment/installation 级共享 | **无用户分区**：订阅、工单、缓存互相可见/覆盖 | ❌ |
| 知识库 | namespace + actor(repo/admin) | 同一 repo 的用户共享草稿、投稿、审计 actor | ❌ |
| 服务绑定/grant | agent 级（`agent_id`） | 所有用户共享工具白名单/项目范围；解绑影响所有人 | ❌ |
| Run/工作/会话归属 | 无 owner 字段 | 任何人可读/取消/停止/下载/归档 | ❌ |
| 审计 | Run 事件 + 治理审计 | 执行侧没有 user_id；trace_id 不进入 dsh/repo_server | ⚠️ |
| 资源配额 | `max_agents`/`storage_quota_mb` 仅存储 | 未执行；一个域可无限建实例/占磁盘 | ❌ |

### 5.3 逐场景结论

1. **A、B 各自开一个新对话，调用同一个 repo Agent**
   - 工作区、dsh-home、native session 都是各自独立的；repo_server 通过 `X-Agent-Run` 找到各自 Run 的工作区，不会把 A 的文件写进 B 的目录。
   - 两者共用安装的固定 skills、依赖和常驻 MCP；当并发调用同一个 stdio 上游时，调用被 per-server lock 串行化，A 的长工具调用会让 B 排队；单次调用受 Run deadline 和 server timeout 双重限制。
   - 业务数据（例如共享部署的数据目录、外部工单/订阅）没有用户维度，A 和 B 看到的是同一份状态；工具调用没有“调用者”参数可以区分人。
   - 全局默认只有 2 个 repo Run 额度（可配 1–32）；A、B 及 cron 任务一起竞争，第三个会排队。没有 per-user 配额或公平调度。
   - **结论：不会串写工作区/会话，会串业务数据、共享授权和吞吐额度。**

2. **A、B 打开同一个对话（同一 chat_id/链接）**
   - `conversation_one_active` 唯一索引保证同一会话同时只有一轮 `queued/running`；两人同时发送时后到者收到“本对话还有一轮正在处理”。
   - 但两人都能读到完整历史、都能停止当前轮次、都能重命名/归档、都能下载同一批产物；没有“这是谁的对话”的检查。
   - **结论：不会并发写坏数据，但会互相打断和互相看到内容。**

3. **A、B 使用同一个共享 MCP 部署（v3）**
   - 授权主体是 Agent 身份而非用户：绑定决定工具白名单和 `scope.projects`，两个用户通过同一 Agent 调用时拿到同一份授权。
   - stdio 后端串行，HTTP 后端并发；`in_flight` 只用于安全排空，不做用户配额；外部服务凭据由网关共享注入。
   - 一个用户解绑或管理员撤发布，会让另一个用户的新调用立即失效；已经通过 grant 校验的在途调用可能继续到返回（这是有意的即时撤销，但没有“只撤销某用户”的粒度）。
   - **结论：并发本身有保护，授权粒度是 Agent 级，不是用户级。**

4. **A、B 同时向知识库投稿**
   - 两者的 grant actor 都是同一个 `repo:<hash>`；`knowledge_submission_list` 会返回彼此的投稿，审批人只看到“repo 来源”，无法按用户归因。
   - SQLite WAL + `BEGIN IMMEDIATE` 保证写事务串行；高并发时可能出现锁等待，没有用户级重试/排队。
   - **结论：不丢数据，但草稿互相可见、责任无法归属到人。**

5. **一个用户取消/停止，另一个用户正在执行**
   - `/api/runs/{id}/cancel`、`/api/conversations/{id}/stop`、`/api/repos/{id}/stop`、`/api/services/{id}/stop` 都不校验归属；任何 token 持有者都能打断别人。
   - `repo stop` 直接杀安装服务，正在进行的工具调用会失败；v3 服务 stop 有排空，但发起者仍然可以是任意人。
   - **结论：这是最直接的跨用户干扰面。**

### 5.4 并发结论

- **不会发生**：A 的工作区被 B 的 dsh 写坏；同一 conversation 同时跑两轮；同一 stdio 会话被两个请求交错写帧。这些已有目录隔离、唯一索引、FIFO claim、per-server/per-session lock 保护。
- **会发生**：共享业务状态串数据；Agent 级授权被所有用户共享；任意用户取消/停止/下载/读取他人资源；共享 stdio 成为吞吐瓶颈；一个用户占满全局 2 个执行额度；知识库投稿无法按人归属；执行侧审计没有用户。
- **判断**：当前实现适合“可信团队、单机、单用户”的并发（同一人开多个对话/任务），**不适合“多个自然人共用同一个 Agent”的多租户场景**。这与 `docs/ARCHITECTURE.md` §3 不变量 9、§18.4“多用户执行强隔离暂缓”的口径一致。

## 6. 缺口与风险清单

| ID | 优先级 | 问题 | 证据 | 影响 | 建议 |
| --- | --- | --- | --- | --- | --- |
| MU-1 | P0 | 页面数据面 `/api/*` 只有一个共享 token，没有用户身份 | `web.py:165-337`；前端 `api.js:1-2` 只传一个 token | 任何 token 持有者全权操作全部页面 | 让页面走 `/api/v1` JWT（或反向代理统一鉴权），token 仅作本机引导 |
| MU-2 | P0 | 会话/Run/工作/安装/知识草稿无 owner，读/改/取消/下载不校验归属 | `store.py:23-67`（runs/conversations/works/repo_installations 无 user 列）；`web.py:173-230,316-339` | 横向越权、互相打断 | 增加 `owner_user_id`/`domain_id`；list/detail/cancel/download 统一校验 |
| MU-3 | P0 | 服务绑定/知识绑定/grant 的主体是平台 Agent，不是用户 | `service_catalog.py:183-190`；`service_resolver.py:124-132`；`knowledge.py:86` | 同一 Agent 的所有用户共享工具、项目范围和知识读写；无法按人减权 | 在 grant/binding 中引入 user/subject 维度，或要求每用户独立 Agent 身份 |
| MU-4 | P0 | 任意 token 持有者可取消/停止/重装他人的 Run、repo、部署 | `web.py:337-339`；`repositories.py:244-249`；`service_install.py:628-646` | 跨用户中断；在途任务失败 | 归属检查 + 在途调用检查 + 允许列表 |
| MU-5 | P0 | 知识投稿 actor=repo/admin，同一 repo 用户共享草稿与列表 | `knowledge.py:86-90,350-359,386` | 草稿互见、审批无法归因、无法按人追责 | submission 增加 user_id；列表按 user 过滤 |
| MU-6 | P0 | 执行侧审计没有用户身份；trace_id 不进入 dsh/repo_server/部署网关 | `service_resolver.py:124-132`；`repo_server.py:86-100`；`deployment_gateway.py` grant 校验 | 无法回答“哪个用户调用了哪个工具” | 把 user_id/trace_id 注入 Run spec、grant 和调用头，落审计 |
| MU-7 | P1 | `/api/v1` 的对象级域校验不完整（registry 详情/改/删；用户列表/更新） | `governance/web.py:192-207,278-307`；`store.py:470-496` | 域管理员跨域读/改对象；用户列表全量泄露 | 对目标对象校验 `domain_id`；按调用者域过滤 list |
| MU-8 | P1 | `/api/v1/me` 只返回全局权限；前端没有登录/角色 UI | `governance/web.py:162-169`；`frontend/src/main.jsx:54` 仅 token 输入 | 无法按角色渲染页面/隐藏按钮 | `me` 返回各域有效权限；前端接入 JWT 和角色门控 |
| MU-9 | P1 | `x-domain-id` 是客户端自由头；membership 不参与授权；停用用户不立即失效 | `governance/web.py:130-136`；`rbac.py:25-29`；`service.py:135-136` | 域边界依赖授权记录而非成员关系；禁用/撤权有窗口 | 校验 membership 或显式“域访问”授权；token 引入用户状态/版本校验与撤销 |
| MU-10 | P1 | 配额/限流未实现；`max_agents`/`storage_quota_mb` 只存不用 | `governance/store.py:75`；无其他引用 | 一个用户/域可占满磁盘、进程、队列 | 在创建实例、上传、触发处执行配额；网关加 per-user 速率限制 |
| MU-11 | P1 | 治理注册中心/网关与执行层是两套服务面 | `web.py:122-123`；`service_resolver.py` 不引用 governance | 治理 RBAC 无法约束真正跑 Agent 的 MCP 调用 | 明确边界，或把执行 MCP 统一经治理网关（需要协议/凭据/审计改造） |
| MU-12 | P2 | 注册中心每次调用新建 MCP 会话/进程，无池化/上限 | `governance/registry.py:72-97` | 多用户并发时进程数/连接数线性增长 | 连接池、并发上限、超时与熔断 |
| MU-13 | P2 | SQLite 多写者争用（`control.sqlite`/`knowledge.sqlite`） | `governance/store.py:178-188`；`knowledge.py:102-115` | 高并发写时锁等待/超时 | 写队列或单写者线程；明确 busy 重试策略 |
| MU-14 | P2 | 登录失败计数“读-改-写”非原子；mock token 仅进程内 | `service.py:58-74`；`mocks/oss.py` 模块级 `_tokens` | 并发爆破/多进程 mock 不一致 | 原子更新/限流；mock 仅测试用，明确不用于生产 |

## 7. 新功能正交检查清单

页面或功能合入前，逐项回答；任一“否”都应在文档中写明边界。

### 7.1 身份与授权

- [ ] 请求身份从哪来：JWT、API key、共享 token、Agent token 还是本机 OS 用户？
- [ ] 身份在哪个边界校验：路由依赖、服务层、网关还是数据库？
- [ ] 未配置认证时是 fail-open 还是 fail-closed？
- [ ] 角色判定是默认拒绝还是默认允许？deny 是否优先？
- [ ] 对象级授权是否校验（不只是“有某个权限”，还要校验目标对象属于调用者域）？
- [ ] 停用/降权/删除用户后，已签发 token 是否立即失效？

### 7.2 域、归属与数据范围

- [ ] 列表查询是否强制按 `domain_id`/`owner_user_id` 过滤？
- [ ] 详情/修改/删除/取消/下载是否校验对象归属？
- [ ] 跨域访问是显式允许（平台管理员）还是默认拒绝？
- [ ] 共享资源（安装、部署、知识 namespace）是否有明确的“共享主体”（Agent/域/用户）？
- [ ] 业务数据能否按用户分区？不能时是否明确只支持单用户？

### 7.3 并发、幂等与恢复

- [ ] 同一资源被两个请求同时修改时，靠锁、版本号、唯一约束还是队列？
- [ ] 并发上限是多少？按全局、按用户还是按资源？
- [ ] 一个用户占满额度时，其他用户是否会饿死？
- [ ] 长调用/慢上游是否会阻塞其他用户？超时如何设置？
- [ ] 取消/停止/升级/重装时，是否有在途调用检查与排空？
- [ ] 幂等键的唯一范围是什么（会话、工作、Run、用户）？
- [ ] 进程/文件/数据库写入失败时，能否安全重试而不产生重复副作用？

### 7.4 审计与可观测

- [ ] 审计是否记录 user_id、domain_id、trace_id、对象、动作、决策（allow/deny）？
- [ ] 执行侧（dsh、repo_server、部署网关）能否拿到同一个 trace_id？
- [ ] 审计是否包含足够的拒绝原因，便于追责和排障？
- [ ] 敏感参数是否脱敏？凭据是否绝不落库？

### 7.5 页面评审模板

| 页面/功能 | 身份来源 | 角色判定 | 域/数据范围 | 归属校验 | 并发保护 | 审计字段 | 越权测试 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 | 见下 |

最小越权测试集：

1. 用户 A 创建资源，用户 B 用同一 URL 读取/修改/删除/取消 → 期望 403/404。
2. 域 A 的用户访问域 B 的对象 → 期望 403/404。
3. 两个用户同时操作同一资源 → 期望串行、冲突或明确的 409，而不是互相覆盖。
4. 管理员降权/停用后，旧 token 立即失去相应能力。
5. 一个用户占满并发额度时，另一个用户仍能得到公平调度或明确错误。

## 8. 建议落地顺序

1. **先明确边界（今天）**：在 README/ARCHITECTURE 明确“当前执行面是单用户/可信团队”；不要把 `/api/*` 或 Admin MCP 直接暴露给多个自然人；治理层只作为独立控制面试点。
2. **P0 统一入口**：页面数据面改为 JWT（或由治理层反向代理 `/api/*`），共享 token 仅保留本机引导；给 conversations/runs/works/repo_installations 增加 owner/domain，并在 list/detail/cancel/download/stop 上校验。
3. **P0 用户级 Agent 授权**：为服务绑定/知识绑定/grant 增加 user 维度，或在“每用户一个 Agent 身份”的前提下生成 grant；执行侧审计注入 user_id/trace_id。
4. **P1 前端角色化**：登录、`me` 返回各域权限、按权限隐藏/禁用页面动作；对象级域校验；停用/降权即时生效。
5. **P1 配额与公平性**：按用户/域限制并发、磁盘、实例数；网关限流；避免一个用户占满全局 2 个额度。
6. **P2 融合治理网关与执行面**：决定“治理网关是否成为唯一 MCP 入口”；若否，明确两套面的边界和各自审计口径。

## 9. 验证与证据

本次审查在拉取后的工作区执行（未修改代码）：

```sh
NO_PROXY= no_proxy= .venv/bin/pytest -q tests
# 268 passed, 1 warning in 220.89s
```

- 新增治理层测试 45 项（governance 24、registry 14、OSS 4、gateway e2e 3）全部通过。
- 本机 shell 的 `no_proxy` 含方括号 `[::1]` 时，2 个 OSS 测试会因 httpx 代理解析失败；清空代理变量后通过，属于环境问题，不是代码缺陷。
- **没有多用户并发/越权回归**：当前测试未覆盖“两个用户读同一会话”“A 取消 B 的 Run”“同一 Agent 两用户并发调用”“停用后旧 token”等场景；建议按 §7.5 的测试集补齐。

## 附录 A：归属字段现状

| 对象 | 表/字段 | 用户/域维度 | 现状 |
| --- | --- | --- | --- |
| 会话 | `conversations`、`conversation_turns` | 无 `user_id`/`domain_id` | 任意 token 可读/改/停止 |
| 运行 | `runs` | 无 `user_id`/`domain_id` | 任意 token 可读/取消/下载报告 |
| 工作 | `works` | 无 owner | 任意 token 可创建/触发/暂停 |
| 仓库安装 | `repo_installations` | 无 owner；身份是 `repo:<hash>` | 任意 token 可安装/停止 |
| 服务绑定 | `service_bindings` | `agent_id`，无 user | 同一 Agent 的所有用户共享 |
| 服务 grant | `service_grants` definition | `agent_id`/run/turn，无 user | 撤销按 Agent/deployment |
| 知识绑定 | `bindings(actor)` | `repo:<hash>`/`admin` | 同一 repo 用户共享 |
| 知识投稿 | `submissions.actor/source` | 无 user | 草稿互见、无法归因 |
| 治理用户/角色/域 | `users/user_roles/user_domains/...` | 有 `user_id`/`domain_id` | 仅治理面使用 |
| 治理审计 | `audit_log` | 有 `user_id/domain_id/trace_id` | 仅治理面写入 |

## 附录 B：术语

- **用户身份**：`/api/v1` JWT 中的 `sub`，对应 `users.id`；权限在 `control.sqlite` 中按角色/直授/域/组求值。
- **平台 Agent 身份**：`agent_identities` 中的 `admin` 或 `repo:<repo_id>`；服务绑定、知识绑定、执行 grant 的主体。
- **Run 身份**：一次执行 `runs.id`；repo 命名空间用 `X-Agent-Run` 做工作区路由和生命周期校验。
- **共享 token**：`AGENT_BASE_WEB_TOKEN`，可选；它不是用户，无法区分调用者。
- **域**：治理层 `domains.id`；执行层没有对应列，`workspace_id ↔ domain_id` 目前只是设计映射。
