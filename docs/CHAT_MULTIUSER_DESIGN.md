# Chat 多用户接入契约（C0 冻结版）

> 状态：**已评审确认（2026-11-19，glisxx）**。权限表、错误码判定顺序、域规则与迁移 DDL 已冻结，C1–C7 实现阶段不得改动；确认记录与子任务索引见 §16。
> 版本：v1.1（2026-11-19；v1.0 首版 + 评审确认、子任务索引与实施注记）
> 基线：`d625d4a`（`88662cf` + `docs/MULTI_USER_AUDIT.md`）
> 对应任务：AIHO-13（C0 契约冻结）
> 依据：AIHO-11 评论《chat 接入用户系统：设计方案与任务编排（待审查）》、《多用户场景正交清单与角色化审查》（`docs/MULTI_USER_AUDIT.md`）、《治理层设计》（`GOVERNANCE_DESIGN.md`）与现有 `/api/v1` 实现。

本文只冻结契约，不描述实现步骤。所有「必须 / 不得 / 应」为规范性要求；「现状」仅用于说明差距，不构成实现限制。

## 1. 范围与术语

### 1.1 本期范围

`/chat` 页面与它的全部数据面：

| 方法 | 路径 | 现状位置 |
| --- | --- | --- |
| GET | `/chat`（HTML） | `src/agent_base/web.py:160` |
| GET | `/api/chat/targets` | `web.py:166` |
| GET | `/api/conversations` | `web.py:174` |
| POST | `/api/conversations` | `web.py:178` |
| GET | `/api/conversations/{id}` | `web.py:182` |
| GET | `/api/conversations/{id}/events` | `web.py:207` |
| POST | `/api/conversations/{id}/messages` | `web.py:199` |
| POST | `/api/conversations/{id}/stop` | `web.py:203` |
| PATCH | `/api/conversations/{id}` | `web.py:195` |
| GET | `/api/conversations/{id}/files` | `web.py:186` |
| GET | `/api/conversations/{id}/download` | `web.py:190` |

以及治理面为支撑本期新增/扩展的两个端点：`POST /api/v1/auth/refresh`、`GET /api/v1/me`（响应只做加法）。

### 1.2 非目标

- MCP / 服务绑定 / 知识库的 grant 用户化（`service_resolver`、`deployment_gateway`、`knowledge` 的主体模型不动）。
- dashboard / workbench / services / knowledge / cron 页面的身份接入；`/api/*` 其余路由保持现状。
- 每用户独立实例的物理隔离；会话协作者 / 共享 / 移交（见 D5）。
- 会话删除、导出、批量迁移域。

### 1.3 术语

| 术语 | 含义 |
| --- | --- |
| 身份（Identity） | 一次请求解析出的用户身份与授权上下文；业务层只认它，不读 header / 环境变量 |
| 页面面 | `/api/chat/*` 与 `/api/conversations*`，即 chat 数据面 |
| 控制面 | `/api/v1/*`，治理层既有接口 |
| 域（domain） | 治理层 `domains` 表的一行；本期用它隔离「本域 Agent」与会话 |
| 可域 | 用户可选择的域集合，非管理员 = 有效 membership，平台管理员 = 全部启用域 |
| 归属（owner） | `conversations.owner_user_id`；本期会话私有，无协作者 |
| 对象 | 会被按 id 访问的会话 / Agent / 产物文件 |
| 引导模式 | `[web] auth = "hybrid"` 下，本机无 JWT 时以本机管理员（或匿名引导）身份访问的模式 |
| 降级 | 实现不得以「缺少某列/某配置」为由放开校验；一律按 fail-closed 处理 |

## 2. 现状与差距（基线证据）

| 维度 | 现状 | 位置 |
| --- | --- | --- |
| 认证 | 只比对可空的 `AGENT_BASE_WEB_TOKEN`，token 为空直接放行 | `web.py:96`、`web.py:142-146` |
| 授权 | `agent.read` / `task.read` / `file.read` 等权限已定义，chat 路由无一处校验 | `governance/store.py:16-25` |
| 域 | `/api/*` 不看域；`x-domain-id` 只在 `/api/v1` 被当作客户端自由头 | `governance/web.py:130-137` |
| 归属 | `conversations` 无 owner / domain 列 | `store.py:52-58` |
| 可见性 | `targets()` 返回全部安装；`list()` 返回全库 200 条 | `conversations.py:36-47`、`conversations.py:93-106` |
| 对象校验 | `get` / `snapshot` / `files` / `artifact` / `rename` / `send` / `cancel` 只校验存在 | `conversations.py:73-161`、`:248` |
| 生命周期 | token 只验签，不复查用户状态；20 分钟 TTL 且无刷新端点 | `governance/auth.py:47-52`、`governance/web.py:122-128` |
| 审计 | 只有 `/api/v1` 动作带 `user_id` / `trace_id`；页面动作无审计 | `governance/store.py:596-602` |
| 并发 | 全局 32 活跃轮次，无每用户计量 | `conversations.py:181` |

## 3. 身份契约

### 3.1 `Identity`（新增 `src/agent_base/identity.py`）

```python
@dataclass(frozen=True)
class Identity:
    user_id: str | None          # None 仅两种：SYSTEM，或无 admin 用户时的匿名 BOOTSTRAP
    username: str | None
    user_type: str               # person | service | bootstrap | system
    platform_admin: bool
    home_domain_id: str | None
    domains: frozenset[str]      # 可域集合；SYSTEM 为全部域
    current_domain: str | None   # 页面面必非空；/api/v1 在无 header 时为 None
    permissions: frozenset[str]  # 当前域内的有效权限；current_domain=None 时为全局权限
    mode: str                    # jwt | token | bootstrap | system

    @property
    def is_bootstrap(self) -> bool:   # token / bootstrap 两种引导身份
        return self.mode in {"token", "bootstrap"}

    def can(self, permission: str) -> bool: ...
```

- 身份在**请求边缘解析一次**，作为 FastAPI 依赖注入；业务层方法必须显式接收 `identity`，不得读环境变量、header 或全局变量。
- `permissions` 由 `rbac.effective_permissions(user_id, current_domain)` 求值一次并缓存；不得在业务层重复查库或自行拼接角色判断。
- `platform_admin` 判定：存在一条 `is_active=1`、未过期、`domain_id IS NULL`、角色名为 `platform_admin` 的授权。引导模式恒为真。
- `domains`：非管理员 = `user_domains` 中 `status='active'` 的域；平台管理员 = 全部 `status='active'` 的域。
- 身份对象**不得**包含密码哈希、api key、token 原文。

### 3.2 解析顺序（冻结）

每个请求按以下顺序解析，任一步失败即终止：

1. 取 `Authorization: Bearer <token>`。
2. `hybrid` 模式下，若 bearer 与已配置的 `AGENT_BASE_WEB_TOKEN` 精确相等 → `BOOTSTRAP`（`mode="token"`）：取最早创建的 `active` `platform_admin` 用户作为 `user_id`（不存在则 `None`），直接进入第 6 步。
3. bearer 为空：
   - `hybrid` 且未配置 `AGENT_BASE_WEB_TOKEN` 且请求来自 loopback → `BOOTSTRAP`（`mode="bootstrap"`），进入第 6 步；
   - 其余情况 → 401（`token_missing`）。
4. bearer 非空 → `Governance.decode` 验签验期。失败 → 401（`token_invalid` / `token_expired`）；`jwt` 模式下携带共享 token 同样在此失败。
5. 以 claims 的 `sub` 重读用户记录。用户不存在、`status != 'active'`、被锁定 → 401（`user_disabled`）。**不得**只信 token 内容。
6. 计算 `platform_admin`、可域集合、`home_domain_id`。`BOOTSTRAP` 恒为平台管理员、可域为全部启用域。
7. 解析当前域（仅页面面与带 header 的 `/api/v1`，见 §7）。失败 → 400 / 403。
8. 计算当前域权限集。

`/api/v1` 使用同一 `identity` 依赖，但保留既有语义：无 `x-domain-id` 时 `current_domain=None`；header 给定但用户不在可域内时，仍按既有全局授权（`domain_id IS NULL` 的角色/直授）判定，不额外要求 membership。页面面必须要求 `current_domain ∈ domains`。

### 3.3 非用户身份

| 身份 | 产生条件 | `user_id` | 权限 | 归属过滤 | 审计 |
| --- | --- | --- | --- | --- | --- |
| `SYSTEM` | 后台线程、scheduler、CLI、内部调用 | `None` | 全部 | 不参与用户过滤 | 调用方原样记录 |
| `BOOTSTRAP` | `auth=hybrid` 且（bearer 等于共享 token，或无 bearer 且未配置共享 token） | 最早创建的 `active` `platform_admin` 用户 id；不存在则 `None` | 全部 | **不做 owner 过滤**（本机引导语义） | `user_id` 同上，`detail.mode` 记 `token` / `bootstrap` |

`SYSTEM` 只能由代码常量构造，**不得**从任何 HTTP 输入推导。`BOOTSTRAP` 仅在 `hybrid` 模式产生，`jwt` 模式下不存在。

### 3.4 认证模式（配置 `[web] auth`，冻结）

| 值 | 行为 |
| --- | --- |
| `hybrid`（默认） | bearer 与已配置的 `AGENT_BASE_WEB_TOKEN` 相等 → `BOOTSTRAP`；否则按 JWT 解析；无 bearer 时若配置了共享 token → 401，未配置且请求来自 loopback → `BOOTSTRAP`。启动时若未配置 token 且存在真实用户，打印一次「本机引导模式，未启用登录」告警。 |
| `jwt` | 只接受 JWT；共享 token 一律 401（fail-closed）。 |

- 两种模式下 `AGENT_BASE_WEB_TOKEN` 都**不得**映射为具名用户，也不得写入 `conversations.owner_user_id` 为 token 本身。
- `hybrid` 的引导路径只允许 loopback（`TrustedHostMiddleware` 已限制 localhost / 127.0.0.1 / [::1] / testserver）；不得因反代而把远程序列当成引导。
- 配置键：`web.auth`，环境变量覆盖 `AGENT_BASE_WEB_AUTH`（可选）。非法值 → 启动失败。

### 3.5 令牌刷新（新增，冻结）

`POST /api/v1/auth/refresh`

- 请求：`Authorization: Bearer <未过期 token>`，无 body。
- 成功 200：`{"token": "...", "expires_in": 1200, "user": {...}}`，与 `login` 同形。
- 失败：无 token / 签名错 / 已过期 / 用户停用 → 401；不做「过期后宽限」。
- 新 token 重新签发 `iat` / `exp` / `jti`；旧 token 在到期前仍有效（无状态令牌，本期不引入吊销列表；用户停用通过每请求复查用户状态立即生效）。
- 本期不设绝对会话上限；C7 文档必须写明「持续活跃可无限续期」这一限制。

### 3.6 `/me`（只做加法，冻结）

`GET /api/v1/me` 在现有字段上新增，不删除不重命名：

```json
{
  "user": {"id": "...", "username": "...", "display_name": "...", "status": "active", "home_domain_id": "..."},
  "roles": [],
  "platform_admin": false,
  "home_domain_id": "...",
  "domains": [
    {"id": "...", "code": "default", "name": "默认域", "status": "active", "joined_at": 0.0,
     "permissions": ["agent.read", "agent.run", "file.read", "task.read", "task.write"]}
  ],
  "effective_permissions": []
}
```

- `domains[].permissions` 是该域内排序后的有效权限，前端门控**只认它**。
- `effective_permissions` 保留现有语义（`current_domain=None` 的全局权限），不用于页面门控。
- 用户停用 → 401。

## 4. 功能 × 维度冻结表

下表是「9 个功能 × 8 个用户系统维度」的冻结结论，实现按行验收，不得减少列。

| 功能 \ 维度 | A 认证 | B 授权 | C 域 | D 归属 | E 可见性 | F 生命周期 | G 审计 | H 并发 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **1. 进入页面/登录** | 登录页 + JWT；401 回登录 | `/me` 返回本域权限集 | 域下拉；服务端校验 membership | — | — | 停用/过期 → 401 | 登录成功/失败已有；不新增 | 失败计数已有 |
| **2. Agent 列表** | 同 1 | `agent.read` | 仅本域安装；Admin Agent 仅平台管理员 | `repo_installations.domain_id` | `targets()` 按域过滤 | 安装停用即从列表消失 | 不审计（只读） | — |
| **3. 新建对话** | 必须登录 | `agent.run` | 写入 `domain_id=当前域` | `owner_user_id=我` | 目标必须在 `targets()` 内，否则 404 | — | `chat.create` | 每用户轮次上限 |
| **4. 会话列表** | 同 1 | `task.read` | 默认跨可域；`domain_id` 可筛选 | `WHERE owner_user_id=我` | 只返回我的；管理员需 `scope=all` | 归档语义不变 | `scope=all` 时 `chat.scope_all` | 200 条/人（沿用） |
| **5. 详情/事件流** | 同 1 | `task.read` | 对象域必须可域 | owner 校验 | 非本人/非本域 → 404 | 停用即断流 | 管理员访问 `chat.scope_all` | 每用户 SSE 上限 |
| **6. 发消息** | 同 1 | `task.write` | 对象域校验 | 只能向我的会话发；run spec 带 `user_id`/`domain_id` | 非本人 404 | 归档会话 400（沿用） | `chat.send` + `trace_id=turn_id` | 每用户轮次上限 + 全局 32 兜底 |
| **7. 停止回复** | 同 1 | `agent.cancel` | 对象域校验 | owner 校验 | 非本人 404 | — | `chat.stop` + `trace_id=turn_id` | — |
| **8. 重命名/归档** | 同 1 | `task.write` | 对象域校验 | owner 校验 | 非本人 404 | 归档/恢复语义不变 | `chat.rename` / `chat.archive` | — |
| **9. 产物列表/下载** | 同 1 | `file.read` | 对象域校验 | owner 校验 | 非本人 404 | 归档后仍可下载（D4） | 列表不审计；下载 `chat.download` | 单文件 64 MiB 沿用 |

## 5. 权限表（冻结）

### 5.1 路由 → 权限 → 数据约束

| 方法 | 路由 | 权限 | 域约束 | 对象约束 | 越权语义 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/chat/targets` | `agent.read` | 只返回当前域可见 Agent | — | 403 |
| GET | `/api/conversations` | `task.read` | `owner_user_id=我` 且域 ∈ 可域；`domain_id` 参数只能取可域 | — | 403 |
| POST | `/api/conversations` | `agent.run` | 目标 Agent 必须属于当前域 | 目标必须在 `targets()` 可见集合内 | 403 / 404 |
| GET | `/api/conversations/{id}` | `task.read` | 对象域 ∈ 可域 | `owner_user_id=我`（或只读 `scope=all`） | 403 / 404 |
| GET | `/api/conversations/{id}/events` | `task.read` | 同上 | 同上 | 403 / 404 |
| POST | `/api/conversations/{id}/messages` | `task.write` | 同上 | owner；无管理员旁路 | 403 / 404 |
| POST | `/api/conversations/{id}/stop` | `agent.cancel` | 同上 | owner；无管理员旁路 | 403 / 404 |
| PATCH | `/api/conversations/{id}` | `task.write` | 同上 | owner；无管理员旁路 | 403 / 404 |
| GET | `/api/conversations/{id}/files` | `file.read` | 同上 | owner（或只读 `scope=all`） | 403 / 404 |
| GET | `/api/conversations/{id}/download` | `file.read` | 同上 | owner（或只读 `scope=all`） | 403 / 404 |

- **端点级权限不足 = 403**；**对象不存在 / 不属于我 / 对象域不可见 = 404**。两者不得混用。
- `scope=all` 只对平台管理员开放，且只用于上表标注「只读 `scope=all`」的 4 条读路由与列表；写路由不接受 `scope`，管理员对他人会话的写操作一律 404。
- `scope` 取值仅 `own`（默认）/ `all`；其他值 400。
- `snapshot` 响应中的 `files` 字段只在身份具备 `file.read` 时返回内容，否则为空数组，避免 `task.read` 越权看到产物清单。

### 5.2 角色矩阵（由内置角色权限推导，冻结）

| 能力 | viewer | developer | domain_admin | auditor | platform_admin | service 账号 |
| --- | --- | --- | --- | --- | --- | --- |
| 看 Agent 列表 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ 403 |
| 看自己的会话列表/详情/事件 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ 403 |
| 看自己的产物列表/下载 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ 403 |
| 新建对话 | ❌ 403 | ✅ | ✅ | ❌ 403 | ✅ | ❌ 403 |
| 发消息 | ❌ 403 | ✅ | ✅ | ❌ 403 | ✅ | ❌ 403 |
| 停止回复 | ❌ 403 | ✅ | ✅ | ❌ 403 | ✅ | ❌ 403 |
| 重命名/归档 | ❌ 403 | ✅ | ✅ | ❌ 403 | ✅ | ❌ 403 |
| 使用 Admin Agent | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| `scope=all` 读他人会话 | ❌ 403 | ❌ 403 | ❌ 403 | ❌ 403 | ✅ + 审计 | ❌ 403 |

说明：

- 矩阵是 `BUILTIN_ROLES` 的推导结果（`governance/store.py:27-67`），不是第二套授权逻辑；实现只校验权限名。
- `service` 账号（`user_type='service'`）在页面面一律 403（D7），即使其角色含 `agent.read`。
- 自定义角色按权限名同样生效；矩阵只约束内置角色。
- viewer 能看「自己的」会话，但无 `agent.run` 因而无法新建；其历史会话来自被降权前创建的数据，仍可读。

### 5.3 统一归属收口 `owned()`（冻结）

所有按 id 访问会话的路径必须先经过唯一一个方法，禁止路由各自判断：

```python
def owned(self, chat_id: str, identity: Identity, *,
          scope_all: bool = False) -> dict:
    row = self.get(chat_id)                      # 不存在 → 404
    if row["owner_user_id"] is None and not identity.is_bootstrap:
        raise NotFound()                          # NULL 不表示公共可见（D3）
    if scope_all:
        if not identity.platform_admin and not identity.is_bootstrap:
            raise Forbidden()
        return row                               # 只读路径；调用方负责审计
    if identity.is_bootstrap:
        return row
    if row["owner_user_id"] != identity.user_id:
        raise NotFound()                          # 防枚举：不区分「不存在」和「别人的」
    if row["domain_id"] not in identity.domains and row["domain_id"] is not None:
        raise NotFound()
    return row
```

- 调用点固定为：`snapshot`、`events`、`files`、`artifact`、`send`、`cancel`、`rename`、SSE 打开时。新增按 id 的 chat 路由必须复用。
- `send` / `cancel` / `rename` 不得传 `scope_all=True`；传入即视为实现错误，由代码审查拦截。
- 列表侧只允许一处拼 SQL：非 `scope=all` 恒带 `owner_user_id=?`；`scope=all` 必须显式、只读、写审计。

### 5.4 必须避免的五种写法（冻结）

1. 只在路由里 `if row.owner != me`，漏掉 SSE、下载、stop、files。
2. 相信前端传来的 `domain_id` / `owner_user_id`（今天的 `x-domain-id` 就是坑）。
3. 管理员旁路写成 `WHERE owner=? OR ?` 或把 `platform_admin` 短路塞进归属判断。
4. 把 JWT 或页面 token 放进 `localStorage`。
5. 迁移时把 `owner_user_id IS NULL` 当成「公共可见」。

## 6. 错误语义（冻结）

### 6.1 判定顺序

对同一请求，错误按以下优先级产生；实现必须把认证、域、权限、归属做成 FastAPI 依赖，保证在业务函数之前失败：

1. 认证失败 → **401**
2. 域解析失败 → **400**（无法确定域）或 **403**（域不可用）
3. 端点权限不足 → **403**
4. 对象不存在 / 非本人 / 对象域不可见 / 目标 Agent 不可见 → **404**
5. 状态冲突与配额 → **409** / **429**
6. 业务参数与取值校验 → **400**
7. 框架层请求体 schema 错误 → **422**（沿用 FastAPI 默认，不做业务语义解释）

### 6.2 状态码表

| 状态 | 触发条件 | `code` |
| --- | --- | --- |
| 401 | 缺少/格式错误/签名错误/过期的 token；用户不存在、停用、锁定；`jwt` 模式下携带共享 token | `token_missing` / `token_invalid` / `token_expired` / `user_disabled` |
| 403 | 当前域权限不足；页面面域不存在/已停用/未加入；`scope=all` 非平台管理员；service 账号访问页面面 | `permission_denied` / `domain_forbidden` / `scope_denied` / `service_account_forbidden` |
| 404 | 会话不存在；非本人；`owner_user_id IS NULL` 且非引导身份；对象域不可见；目标 Agent 不可见；产物文件不存在 | `conversation_not_found` / `agent_not_found` / `artifact_not_found` |
| 400 | 页面面多域用户未带域头；`scope` 非法；归档会话发消息；`client_key` 冲突；标题/消息/路径取值非法；超过 64 MiB；目标 Agent 可见但已停用 | `domain_required` / `invalid_scope` / `conversation_archived` / `invalid_request` |
| 409 | 会话已有一轮在跑（`ConversationBusy`）；全局 32 轮已满 | `conversation_busy` |
| 429 | 每用户活跃轮次超限；每用户 SSE 连接超限 | `user_turn_quota` / `user_stream_quota` |
| 422 | 请求体结构不符合 schema | 框架默认 |

- **404 不得泄露对象是否存在**：不同原因使用同一文案（「对话不存在」）。403 可以说明缺哪个权限。
- 401 响应不得区分「用户不存在」与「密码错误」；本期沿用现有文案。
- 页面面的错误响应体统一为 `{"detail": "<中文可读消息>", "code": "<稳定码>"}`；`code` 供前端分支，`detail` 供人读。404 的 `code` 只允许上表三个值，不得出现内部 id。`/api/v1` 既有错误保持 `detail` 形状，新增的 refresh/me 错误可附带 `code`。
- 现有 `ValueError` → 400 的全局处理器保留；新增的 403/404/429 必须用独立异常类型，不得靠字符串匹配。

### 6.3 SSE

- 打开时的认证/权限/归属失败返回普通 HTTP 401/403/404，不建立流。
- 流建立后不再改变 HTTP 状态；身份失效（停用、移出域）或权限撤销时，服务端必须在 30 秒内发送 `event: error` 帧（`data: {"code": "unauthorized"}`）并关闭连接。
- 流内错误不得携带堆栈、路径、token。
- `Last-Event-ID` / `after` 语义不变。

### 6.4 前端动作（冻结）

| 状态 | 前端动作 |
| --- | --- |
| 401 | 清空内存 token，跳登录页；不自动重试 |
| 403 | 显示「无权限」提示；隐藏对应按钮；不重试 |
| 404 | 视为已删除：从列表移除并提示；不重试 |
| 409 | 提示当前轮次进行中；保留输入框内容 |
| 429 | 提示达到个人上限；按钮冷却，不自动重试 |
| 400 | 就地展示 `detail`；不跳转 |
| 422 | 展示通用「请求格式不正确」；不跳转 |

## 7. 域规则（冻结）

### 7.1 `x-domain-id` 只是选择器

- 它只表达「本次请求想用哪个域」，**不是**授权依据。
- 服务端每次请求都必须用数据库重新确认：域存在、`status='active'`、用户在可域内。
- 前端不得缓存域权限后跳过服务端校验；服务端不得因为「前端已经选过」而放行。

### 7.2 解析规则

| 情形 | 结果 |
| --- | --- |
| 页面面；header 存在且 ∈ 可域 | 使用该域 |
| 页面面；header 存在但域不存在 / 停用 / 不在可域 | 403 `domain_forbidden` |
| 页面面；header 缺失；`home_domain_id` ∈ 可域 | 使用 `home_domain_id` |
| 页面面；header 缺失；`home_domain_id` 不可用但可域只有一个 | 使用该唯一域 |
| 页面面；header 缺失；可域为空 | 403 `domain_forbidden` |
| 页面面；header 缺失；可域多个且无法确定 | 400 `domain_required` |
| 页面面；`BOOTSTRAP` | 使用 header（若有效）否则 `default` 域；永不 400 |
| `/api/v1`；header 缺失 | `current_domain=None`（既有全局语义） |
| `/api/v1`；header 存在但域不存在 | 403 `domain_forbidden` |

- header 值只接受**域 id**（与现有 `/api/v1` 一致），不接受域 code；前端从 `/me` 拿 id。
- `default` 域定义：`domains.code='default'`；不存在时取最早创建的 `active` 域。

### 7.3 对象域

- 会话的域在创建时写入，**不可变**；本期不提供改域 API。
- 对象访问要求「对象域 ∈ 可域」，否则 404（不是 403，避免暴露对象存在）。
- 平台管理员的可域是全部启用域，因此对象域约束对管理员自动成立；但访问**他人**会话仍需显式 `scope=all`（只读）。

### 7.4 `targets()` 可见性（冻结）

对每个 target：

1. Admin Agent（`id="admin"`）：仅 `platform_admin`（含 BOOTSTRAP）可见；对其他人完全不出现在列表里。
2. repo Agent：`repo_installations.status == "ready"`、`domain_id == current_domain`（NULL 视为 `default` 域）、且当前用户有 `agent.read`。
3. `available` 仍表示 `enabled`；不可用但可见的 Agent 保留在列表里并置 `available=false`（列表不隐藏，创建时才 400）。
4. 列表顺序、字段、`defaults` 形状不变。

### 7.5 写入时的域

- `POST /api/conversations`：`conversations.domain_id = current_domain`，`owner_user_id = identity.user_id`。
- repo 对话每轮创建的 `runs.spec`：`user_id` / `domain_id` 取会话的 owner / domain，与 `conversation_id` 一起写入。
- `POST /api/repos/install`（本期外的唯一最小改动）：写入 `repo_installations.domain_id`。有页面身份时取 `current_domain`；无身份（BOOTSTRAP / 老代码 / CLI）取 `default` 域（D10）。除此之外不改造 repos 路由的鉴权。
- 存量 `repo_installations.domain_id IS NULL` 由迁移回填，不按请求临时推断。

### 7.6 跨域禁止项（冻结）

- 不得用「用户是平台管理员」替代对象域校验（管理员可域更宽，但对象访问仍要过 `owned()`）。
- 不得支持「一次请求访问多个域」；批量跨域查询不在本期。
- 不得把域写进 token 作为唯一依据；token 只带身份，不带权限与域集合。
- 不得让 `SYSTEM` 身份通过 HTTP 产生。

## 8. 数据模型与迁移（冻结）

### 8.1 DDL

```sql
ALTER TABLE conversations ADD COLUMN owner_user_id TEXT;
ALTER TABLE conversations ADD COLUMN domain_id TEXT;
CREATE INDEX IF NOT EXISTS conversations_owner
  ON conversations(owner_user_id, archived, updated);
CREATE INDEX IF NOT EXISTS conversations_domain
  ON conversations(domain_id, archived, updated);

ALTER TABLE repo_installations ADD COLUMN domain_id TEXT;
```

- 不加外键、不加 `NOT NULL`（SQLite `ADD COLUMN` 限制 + 存量行兼容）；约束由应用层保证。
- `conversation_turns` / `conversation_events` **不加**用户列；归属由 `conversations` 推导。
- `runs` 表结构不变；用户信息只进 `runs.spec`（§8.7）。
- 索引名如上，实现不得改名，便于后续迁移与排障对齐。

### 8.2 NULL 语义（冻结）

| 列 | NULL 表示 | 可见性 |
| --- | --- | --- |
| `conversations.owner_user_id` | 引导模式创建的会话，或迁移时尚无 bootstrap admin | 普通用户不可见；BOOTSTRAP 可见可写；平台管理员 `scope=all` 只读可见 |
| `conversations.domain_id` | 迁移尚未回填或引导期无默认域 | 视为 `default` 域；C3 起读路径不得依赖 NULL |
| `repo_installations.domain_id` | 迁移尚未回填 | 视为 `default` 域 |

**`NULL` 永远不等于「公共可见」**。

### 8.3 回填（每次启动执行，幂等）

1. 取 bootstrap admin：`users.username='admin'` 且 `status='active'`。不存在则跳过 owner 回填并告警，不阻断启动。
2. 取默认域：`domains.code='default'`，否则最早创建的 `active` 域；都没有则跳过域回填并告警。
3. `UPDATE conversations SET owner_user_id=? WHERE owner_user_id IS NULL`（仅当 bootstrap admin 存在）。
4. `UPDATE conversations SET domain_id=? WHERE domain_id IS NULL`。
5. `UPDATE repo_installations SET domain_id=? WHERE domain_id IS NULL`。
6. 仅当本次实际更新行数 > 0 时写一条 `chat.migrate` 审计，`detail` 记录三张表的更新行数、bootstrap admin id、默认域 id。

### 8.4 执行顺序与幂等

- 迁移必须在应用开始接受请求**之前**完成；实现位置为启动钩子（`create_app` 内、路由可用前），由 C2 提供独立函数。
- 列检测用 `PRAGMA table_info`；已存在则跳过 `ALTER`，但仍执行回填（回填本身以 `IS NULL` 为条件，重复执行为 0 行）。
- 迁移依赖 `ControlStore` 已完成种子（默认域、bootstrap admin）；实现必须在 `register_governance` 之后、路由可用之前执行。控制面无法读取时按本节最后一条让启动失败，不得跳过回填继续服务。
- 迁移失败（磁盘满、锁超时）必须让启动失败，**不得**以降级模式继续服务。

### 8.5 备份与回滚

- 首次检测到列缺失时，在 `runs.sqlite` 同目录生成一次性备份 `runs.sqlite.pre-chat-multiuser`；文件已存在则不覆盖。
- 备份用 SQLite backup API（或等价的在线备份），不做裸文件复制。
- 回滚策略：列是纯增量，旧版本代码可继续读写（忽略新列），**不删除任何数据**。回滚后旧代码新建的会话 `owner_user_id IS NULL`，再次升级时按 §8.3 回填给 bootstrap admin，并写审计。
- 不得在迁移中删除/重建 `conversations`、`conversation_turns`、`conversation_events`。

### 8.6 新写入

| 场景 | `owner_user_id` | `domain_id` |
| --- | --- | --- |
| JWT 用户新建会话 | `identity.user_id` | `identity.current_domain` |
| BOOTSTRAP 新建会话（有 admin） | bootstrap admin id | `default` 或 header 选中的有效域 |
| BOOTSTRAP 新建会话（无 admin） | `NULL` | `default` 或 header 选中的有效域 |
| 迁移存量会话 | bootstrap admin id（无则 NULL） | `default` 域 |
| 新安装 repo | — | 页面身份的 `current_domain`，否则 `default` |

### 8.7 `runs.spec`

repo 对话每轮创建 Run 时，`spec` 必须包含：

```json
{"kind": "conversation", "conversation_id": "...", "installation_id": "...",
 "user_id": "<会话 owner>", "domain_id": "<会话 domain>", "runtime": {}, "max_concurrent": "<config.queue.max_concurrent>"}
```

- 只做注入，不改变 MCP / 服务绑定 / 知识库的主体模型（本期非目标）。
- 非 chat 的 Run 不要求这两个键；读取方必须容忍缺失。
- admin 对话不创建 Run，但仍然计入每用户轮次（§9）。

## 9. 并发与配额（冻结）

| 项 | 默认 | 配置键 | 语义 |
| --- | --- | --- | --- |
| 每用户活跃轮次 | 8 | `web.per_user_max_turns` | 该用户名下 `status IN ('queued','running')` 的 turn 数；超限 429 `user_turn_quota` |
| 全局活跃轮次 | 32（沿用） | — | 现有硬编码兜底；超限 409 `conversation_busy` |
| 单会话活跃轮次 | 1（沿用） | — | 数据库 partial unique index；超限 409 |
| 每用户 SSE 连接 | 4 | `web.per_user_max_streams` | 同进程计数；超限 429 `user_stream_quota` |
| 列表返回条数 | 200（沿用） | — | 每用户 |
| 单文件下载 | 64 MiB（沿用） | — | 无变化 |

- 每用户计数按 `conversations.owner_user_id` 聚合；`BOOTSTRAP`/匿名引导按一个桶计。
- 检查与插入必须在同一 `BEGIN IMMEDIATE` 事务内，避免并发绕过。
- 配置值必须是 1–32 的整数；非法 → 启动失败。`0` 不表示「不限制」。
- SSE 计数是进程内实现；本期 `agent-base serve` 单进程假设写入 C7 文档；多 worker 部署需改为数据库/共享存储，不在本期。

## 10. 审计（冻结）

| 动作 | 触发 | `trace_id` | `detail`（不得含正文） |
| --- | --- | --- | --- |
| `chat.create` | 新建会话成功 | `null` | `conversation_id`、`target`、`target_kind`、`title_auto`、`model`、`effort` |
| `chat.send` | 消息受理成功 | `turn_id` | `conversation_id`、`turn_id` |
| `chat.stop` | 停止请求 | 当前活跃 `turn_id` | `conversation_id`、`turn_id`、`requested` |
| `chat.rename` | 改名成功 | `null` | `conversation_id`、`fields`（只记字段名，不记标题值） |
| `chat.archive` | 归档/恢复成功 | `null` | `conversation_id`、`archived` |
| `chat.download` | 下载成功 | `null` | `conversation_id`、`path`、`size` |
| `chat.scope_all` | 平台管理员 `scope=all` 读 | `null` | `path`、`conversation_id`（如有）、`count`（列表） |
| `chat.migrate` | 回填实际更新 > 0 行 | `null` | 见 §8.3 |

- `level` 固定为 `"chat"`；`domain_id` 取会话域（`chat.migrate` 取默认域）；`user_id` 取 `identity.user_id`，匿名引导为 `null`，并在 `detail.mode` 标注 `token` / `bootstrap`。
- **禁止**写入 `user_text`、`assistant_text`、工具入参/结果、产物内容、token、密码。标题值也不写。
- `GET /api/v1/audit?trace_id=<turn_id>` 必须能串起该轮的 `chat.send` 与 `chat.stop`；`?user_id=` 必须能列出某人的全部 chat 动作。
- 审计写失败不得阻断业务（与现有 §gateway 行为一致），但必须记录本地错误日志；C5 测试需覆盖「审计写入失败不影响主流程」。

## 11. 前端契约（C6 输入，冻结）

- 登录页：用户名/密码；成功后 token 只放**内存 + `sessionStorage`**，禁止 `localStorage`。
- 401 统一跳登录；登录后回到原路径。
- 域选择：顶部下拉，选项来自 `/me` 的 `domains`；选中值写入 `sessionStorage`，请求带 `x-domain-id`；服务端拒绝时清空选择并刷新 `/me`。
- 按 `/me` 的 `domains[].permissions` 门控按钮：
  - 无 `agent.run`：隐藏「新对话」与 Agent 列表的发起入口；
  - 无 `task.write`：输入框只读、隐藏发送/重命名/归档；
  - 无 `agent.cancel`：隐藏停止；
  - 无 `file.read`：隐藏产物面板与下载；
  - 非平台管理员：不显示 Admin Agent，不显示 `scope=all` 入口。
- 刷新：在 token 剩余有效期约 20% 时调用 refresh；同一时刻只允许一个刷新请求（single-flight）；刷新失败按 401 处理。
- 共享 token 输入框降级为「本机引导」入口，仅 `hybrid` 模式显示；`jwt` 模式隐藏。
- 前端不得自行推断权限或域，只消费 `/me` 与服务端错误码。

## 12. 配置（冻结）

```toml
[web]
auth = "hybrid"              # hybrid | jwt
per_user_max_turns = 8       # 1–32
per_user_max_streams = 4     # 1–32
```

- 环境变量覆盖：`AGENT_BASE_WEB_AUTH`、`AGENT_BASE_WEB_PER_USER_TURNS`、`AGENT_BASE_WEB_PER_USER_STREAMS`（可选，便于测试）。
- 现有 `AGENT_BASE_WEB_TOKEN` 仍是环境变量，不写入配置文件。
- 未提供 `[web]` 段时使用上述默认值，保证老配置可直接启动。

### 12.1 部署前提（C7 文档）

现有 `TrustedHostMiddleware` 只允许 `localhost` / `127.0.0.1` / `[::1]` / `testserver`，且服务默认绑定 loopback。让其他主机的浏览器访问需要反向代理或调整 `allowed_hosts`，并在 HTTPS 下传输 token；这部分不在 C1–C6 的代码范围内，由 C7 在部署文档中说明，不得为了「先能用」而放宽 Host 校验。

## 13. 冻结决策清单

| 编号 | 决策 | 冻结值 | 与设计评论的关系 |
| --- | --- | --- | --- |
| D1 | 多域用户如何选当前域 | `x-domain-id`（域 id）+ 服务端 membership 校验；页面面必须能确定域，多域无选择 → 400 | 采纳 L1 推荐值，并明确「页面面必须确定域」 |
| D2 | 平台管理员能否看他人会话 | 默认不能；显式 `scope=all` 只读 + 每次审计；无写旁路 | 采纳 L2 推荐值，并收紧为「只读 + 无写旁路」 |
| D3 | 存量会话归谁 | 回填 bootstrap admin；无 admin 时保持 NULL，NULL 对普通用户不可见 | 采纳 L3 推荐值 |
| D4 | 归档后能否下载产物 | 能，保持现状 | 采纳 L4 推荐值 |
| D5 | 是否允许协作者 | 本期不允许；后续加 `conversation_members` 表 | 采纳 L5 推荐值 |
| D6 | Admin Agent 的可用范围 | 仅 `platform_admin`（含 BOOTSTRAP）；对普通角色完全不出现 | C0 新增：Admin Agent 以本机管理员身份执行，不能按普通域权限发放 |
| D7 | 服务账号能否进页面面 | 不能；`user_type='service'` 在 chat 路由一律 403 | C0 新增：服务账号是机器主体，页面按人计量与审计 |
| D8 | 双模默认值 | 默认 `hybrid`；`jwt` 为 fail-closed 严格模式 | 采纳设计 §3.5，并明确默认值 |
| D9 | 每用户配额默认值 | 8 轮 / 4 条 SSE，配置可调 1–32 | 采纳设计 §3.6 推荐值 |
| D10 | `repo_installations.domain_id` 如何写入 | 安装时取当前身份域；无身份取 `default` 域；仅改这一处写入，不改造 repos 鉴权 | C0 新增：否则多域下 targets 永远为空 |

评审若修改任一冻结值，必须同时修改本表、受影响的权限/迁移章节并升版本；实现阶段不得私自偏离。

## 14. 测试矩阵与阶段验收

| 阶段 | 测试文件 | 必须覆盖 |
| --- | --- | --- |
| C1 身份底座 | `tests/test_web_auth.py`（新增）、`tests/test_governance.py`（扩展） | 无 token 401、过期 401、停用 401、`jwt` 模式拒共享 token、`hybrid` 本机引导可用、refresh 换新、`/me` 返回域权限、既有 token 测试全绿 |
| C2 数据模型 + 迁移 | `tests/test_chat_migration.py`（新增） | 三列与两个索引存在、NULL 回填给 admin、默认域回填、重复执行 0 行且不重复审计、备份文件生成、无 admin/无域时告警不阻断、`repo_installations` 回填 |
| C3 读路径归属 | `tests/test_chat_authz.py`（新增，读部分） | A 与 B 互相看不到 list/snapshot/events/files/download（404）、admin `scope=all` 可见、非 admin `scope=all` 403、目标 Agent 跨域不可见、SSE 同样 404 |
| C4 写路径鉴权 | `tests/test_chat_authz.py`（写部分）、`tests/test_chat_quota.py`（新增） | B 对 A 的 send/stop/rename/archive 全 404、viewer 发消息 403、owner 正常、每用户 8 轮 429、全局 32 兜底 409、SSE 4 条 429 |
| C5 审计与 trace | `tests/test_chat_audit.py`（新增） | 6 个业务动作 + `chat.scope_all` / `chat.migrate` 落库、`audit?trace_id=turn_id` 串联、`detail` 不含正文/标题、审计写失败不影响主流程 |
| C6 前端 | `frontend/tests/auth.spec.js`（新增） | 401 回登录、403/404 展示、按钮按 `/me` 权限显隐、域切换带 header、token 不落 localStorage、refresh single-flight |
| C7 回归与文档 | 全量 `pytest` + `frontend` 测试 | 全绿；`docs/WebChat.md`、`docs/ARCHITECTURE.md`、`docs/GOVERNANCE_DESIGN.md` 更新；迁移演练与手工走查记录 |

每个阶段只提交自己范围内的改动；C1–C5 不修改前端，C6 不修改后端契约。

## 15. 评审清单

- [ ] 笛卡尔积的 9 个功能是否有遗漏（尤其：自动标题、会话信息面板、工具调用展示是否要单独权限）。本期判断：自动标题随 `chat.create` / `chat.send` 授权，不单列；会话信息面板只展示本人会话元数据，随 `task.read`；工具调用展示随 `task.read`，不单列。
- [ ] 8 个维度是否有遗漏（共享/移交、导出、删除会话）。本期判断：均为非目标，D5 明确。
- [ ] 权限映射是否认可：`agent.read` / `agent.run` / `task.read` / `task.write` / `agent.cancel` / `file.read`。
- [ ] 错误语义 404 vs 403 是否认可（§6）。
- [ ] 「会话私有、无协作者」是否认可（D5）。
- [x] 7 个任务的切分与顺序是否认可；C1+C2+C3 作为首个切片是否可以。——**已确认（glisxx，2026-11-19）**：按 7 个任务切分并放入任务看板，见 §16。
- [x] D1–D10 是否认可，尤其 D2（管理员只读旁路）、D6（Admin Agent 仅平台管理员）、D7（服务账号禁入页面）。——**D2 / D6 / D7 已由 glisxx 明确确认（2026-11-19）**；其余决策为推荐默认值，按冻结值执行，确需调整时按 §13 末尾流程升版本。
- [ ] 是否接受「本期完全不碰 MCP / 服务 / 知识 grant」。
- [ ] 是否接受 `hybrid` 为默认认证模式（本机引导不回归，但本机无 token 时仍是全权）。
- [ ] 是否接受「无状态刷新、无绝对会话上限」这一过渡限制。

## 16. 子任务索引与评审记录

### 16.1 评审确认（2026-11-19，glisxx）

- 7 个任务的切分与顺序确认；全部子任务以 `backlog` 入板（AIHO-11 任务看板），上一阶段关闭后再提升下一阶段为 `todo`。
- `docs/CHAT_MULTIUSER_DESIGN.md` 冻结内容（权限表、错误码判定顺序、域规则、迁移 DDL）不改动。
- D2 / D6 / D7 明确确认：管理员默认不看他人会话，`scope=all` 只读且每次审计、无写旁路；Admin Agent 仅平台管理员可见；service 账号禁入 chat 页面面。

### 16.2 子任务（按阶段）

| 阶段 | 编号 | Issue | 交付 | 初始状态 |
| --- | --- | --- | --- | --- |
| 1 | C1 身份底座 | AIHO-14 | `identity.py`、双模认证、`/me` 域权限、refresh | backlog |
| 2 | C2 数据模型 + 迁移 | AIHO-15 | 三列 + 两索引、回填、备份、幂等、安装域写入 | backlog |
| 3 | C3 读路径归属 | AIHO-16 | `targets()`、`list()`、`owned()`、SSE、只读 `scope=all` | backlog |
| 4 | C4 写路径鉴权 | AIHO-17 | create / send / stop / rename、每用户轮次与 SSE 配额、`runs.spec` | backlog |
| 5 | C5 审计与 trace | AIHO-18 | 8 个 `chat.*` 动作、`trace_id=turn_id`、审计失败不阻断 | backlog |
| 6 | C6 前端 | AIHO-19 | 登录门控、域选择、权限显隐、错误处理 | backlog |
| 7 | C7 回归与文档 | AIHO-20 | 全量测试、迁移演练、手工走查、部署文档 | backlog |

- 阶段是依赖波次，不是并行许可：每阶段只提升下一个待办任务；`AIHO-11` 作为父任务负责在阶段关闭后提升下一阶段。
- C6 按依赖可在 C1 之后提前；当前排在 C5 之后，便于前端一次对齐最终后端契约。如需提前，单独提升 AIHO-19 并说明理由。

### 16.3 实施前提校正（v1.1，不改变冻结项）

C0 冻结（commit `cec82a5`）之后，主干又合入了 HttpOnly cookie 会话与登录页：`src/agent_base/web_auth.py`（`/api/auth/login|session|logout`）、`frontend/src/AuthShell.jsx`、`frontend/src/api.js`（commit `8e825f5`）。因此：

- C1 的 `Identity` 必须同时接受 `Authorization: Bearer`（`governance/auth.py`）与 `agent_base_session` cookie，两者都按 §3.2 复查用户状态；
- C6 在既有登录页上扩展，不新建第二套登录页；前端不接触 token；
- §3.5 的 refresh 端点保留给 Bearer 客户端，cookie 会话的续期方式（滑动或过期重登）由 C1 明确并在交付说明中记录；
- 本条只影响身份传输与前端起点，**不改变**权限表、错误码判定顺序、域规则与迁移 DDL。

## 17. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.0 | 2026-11-19 | 首版：冻结身份、权限、错误、域、迁移、并发、审计与前端契约；新增 D6–D10 |
| v1.1 | 2026-11-19 | 评审确认（D2 / D6 / D7、7 任务切分）；新增 §16 子任务索引与 cookie 会话实施注记；冻结项未改 |
