# 网页对话台

2026-11-10 更新：新对话统一从 AGENTS 列表发起，最近对话新增独立的 Agent 筛选；Cmd/Ctrl+K 定位 Agent 列表。左侧“定时任务”进入独立 `/cron` 页面。最新交互与计划说明见 [SchedulingUI.md](SchedulingUI.md)。

启动现有 Web 服务后访问 <http://127.0.0.1:8765/chat>。原工作调度和 Repo 资源页面已迁到 `/workbench`，首页 `/` 跳转仪表盘；页面之间互相有入口。

```sh
.venv/bin/agent-base serve --port 8765
```

左侧选择 Admin Agent 或任一已安装 repo，再输入消息。Admin 使用根 AGENTS.md 和管理 MCP；repo Agent 使用所绑定安装版本的 AGENTS、skills 和常驻 MCP。支持多个独立对话，不把不同 repo 的上下文混在一起。

## 已提供的交互

- Agent 列表与历史对话；新安装的 Agent 可手动刷新或等待列表更新。
- 连续多轮对话、服务端历史保存、刷新恢复、对话改名及归档/恢复。
- 创建对话时指定 model 和推理强度；已有对话保留创建时的设置，需要切换时新建对话。
- CLI 原生文本、执行状态和工具事件实时展示；工具参数与返回值可展开。
- Markdown、代码块、表格和回复复制。原生 reasoning/thinking 不展示为聊天内容。
- 独立停止按钮；切换会话、关闭标签页或断线只关闭查看连接，不取消生成。
- 会话信息面板展示 namespace、skills、MCP 和原生 session ID。
- repo 会话产物列表与下载，文件限于本会话 artifacts；Markdown 中的相对 artifacts 链接可直接下载。
- 桌面与手机布局；Enter 发送、Shift+Enter 换行，中文输入法确认候选字时不会误发送。Cmd/Ctrl+K 定位 Agent 列表，从列表选择后发起新对话。

当前 dsh 发布版以文本块发送 headless 输出；界面按真实事件更新，不模拟逐字输出。

## 调研依据和选型

本次先使用 byted-web-search 检索，再核对项目官方文档，采用以下实践：

| 实践 | 参考 | 本项目的实现 |
| --- | --- | --- |
| 分离 UI、运行状态、后端 Agent 和持久化 | [assistant-ui architecture](https://www.assistant-ui.com/docs/architecture) | React 负责渲染，Python 管理会话，dsh 负责模型和工具循环 |
| 对话列表与消息历史分别管理 | [assistant-ui threads](https://www.assistant-ui.com/docs/runtimes/concepts/threads) | conversations 元数据、conversation_turns 消息轮次分别保存 |
| 消息持久化与服务端身份 | [AI SDK message persistence](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-message-persistence) | SQLite 生成会话/轮次 ID；用户每次消息带 client_key 去重 |
| 刷新恢复和显式取消分开 | [AI SDK resume streams](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams) | 后台独立进程持续生成；SSE 订阅按事件 seq 恢复，停止走专用 POST |
| 流断开后重放已有事件并继续追踪 | [assistant-ui resumable streams](https://www.assistant-ui.com/docs/guides/resumable-streams) | conversation_events 为持久事件日志，GET 支持 after/Last-Event-ID |
| Markdown 默认安全渲染 | [react-markdown](https://github.com/remarkjs/react-markdown) | 不启用原始 HTML，外部图片不自动加载，工具结果作为文本展示 |

本项目没有引入 Assistant Cloud、AI SDK 的模型调用或其他模型 API。界面借鉴这些项目的会话与交互设计，用 React + 本地事件适配器接入已有 CLI。单机 SQLite 已承担持久化，不再额外部署 Redis 或云端会话服务。

## 会话和运行如何关联

```text
浏览器 POST 消息（client_key）
  → SQLite 创建一轮 queued 消息
  → 启动独立 Python 对话监督进程
  → 为 repo 对话领取全局执行位置
  → 绑定已有安装和本对话工作区
  → dsh --session-id <本对话已保存的原生 ID>
  → NDJSON 原生事件转为持久化 UI 事件
  → 浏览器通过 SSE 读取事件
```

第一次调用由 CLI 创建 native session，后续沿用同一 DSH_HOME、工作区与 session ID。Python 不把网页全部历史重新塞进 prompt，任务内上下文和压缩仍由 CLI 管理。不同对话拥有不同的目录和原生 session。

repo 对话为每轮创建 Run，复用已有调度的 FIFO 和全局并发限制，使 repo MCP 能根据 Run 找到正确的工作区。Admin 对话不占用仓库执行位置，避免仓库任务已占满时管理员无法查询或管理它们。同一个对话一次只允许一个活动轮次，由数据库约束和会话锁保证；不同对话可并发。

持久目录：

```text
.agent-base/conversations/<conversation-id>/
  workspace/               多轮共享的会话工作区
    .agent/resources.json  本 repo 的资源清单
    .agent/skills/         链接到原有固定安装
    artifacts/             本会话文件
  dsh-home/                原生 CLI 会话存储
  turns/<turn-id>/
    prompt.txt
    runtime.patch.yml
    runtime.jsonl
    runtime.stderr.log
    supervisor.log
```

repo skills 不重新安装，MCP 不随每轮启动或退出；对话超时、取消、归档不会停止这些常驻资源。管理员通过管理工具显式 stop repo 服务仍会影响共享它的运行。

每次消息有单独执行时限，排队不计入执行时间。页面关闭后监督进程继续；监督进程异常消失则记录 interrupted，并清理已知 CLI 进程，不自动重发消息。失败或取消保留已收到的文本，后续消息仍可在原会话继续。

网页对话的 succeeded 表示本轮 CLI 正常返回有效输出；它不自动执行 repo 默认任务或 completion checks。repo 对话 Run 标记 `verification=conversation_only`。需要固定产物和独立验证的工作，应通过 admin 的 submit_run 或原工作调度接口执行，其检查逻辑保持不变。

## API

| 接口 | 用途 |
| --- | --- |
| `GET /api/chat/targets` | Admin、所有 ready 安装版本、资源摘要、默认 model/effort |
| `POST /api/conversations` | 创建对话，body 为 target、可选 title/model/effort |
| `GET /api/conversations?archived=false` | 最近最多 200 个对话元数据 |
| `GET /api/conversations/{id}` | 快照：最近 100 轮、最近 5000 条事件、当前 cursor 和文件 |
| `PATCH /api/conversations/{id}` | 修改 title 或 archived；活动轮次结束前不归档 |
| `POST /api/conversations/{id}/messages` | body 为 text 和 client_key，返回 202 和轮次 |
| `GET /api/conversations/{id}/events?after=N` | SSE 持久事件流；也接受 Last-Event-ID |
| `POST /api/conversations/{id}/stop` | 请求停止活动轮次；等待终态确认已经停止 |
| `GET /api/conversations/{id}/files` | 当前会话最多 100 个产物文件 |
| `GET /api/conversations/{id}/download?path=artifacts/report.json` | 下载本会话产物，单文件不超过 64 MiB |

同一 client_key 重试返回原轮次；同 key 不同文本报错。新 key 遇到当前对话仍忙返回 409，前端等待结束或停止后再发。重新订阅 SSE 只读已有事件，不产生模型请求。浏览器使用 fetch 读取 SSE，因此管理 Token 可以放在 Authorization header，不进入 URL。

消息与 API 保留 Host、跨 Origin 写请求限制和原管理 Bearer 认证；账号登录后也可以使用同源浏览器会话，无需反复填写 Token。原“连接设置”继续支持已配置的 `AGENT_BASE_WEB_TOKEN`，只保存在页面内存。

## 账号登录与会话

访问 `/login`，使用管理员已经分配的账号和密码登录；页面没有注册入口。登录成功后回到原来的页面，刷新、导航、SSE 和下载沿用同一个会话；侧栏显示当前用户并提供退出。账号未获全局管理权限时只展示本人账号信息，不挂载全局工作台。

尚无账号时，由管理员在本机运行 `agent-base control init` 初始化管理员；此命令仅首次创建时在管理员自己的终端输出初始密码，登录页不提供默认密码或自动创建管理员。也可通过已有 `AGENT_BASE_ADMIN_PASSWORD` 启动引导。其他账号仍由有权管理员通过治理接口创建。

浏览器 Cookie 为 HttpOnly、SameSite=Lax，HTTPS 下带 Secure；会话随机值只以哈希存入 `control.sqlite.browser_sessions`。会话有效期为 20 分钟，无静默刷新；到期返回登录页。退出会删除服务端会话，复制的旧 Cookie 也不可继续使用；不把密码、平台令牌或管理 Token 写入 localStorage/sessionStorage。

没有治理账号时保留原本机工作台行为。一旦存在治理账号，匿名调用旧工作台 API 被拒绝；仅具备全部全局管理权限的用户可访问，原显式管理 Bearer token 保持兼容。账号停用或权限减少影响后续请求。此过渡接入不代表旧工作台已经具备按用户/域隔离的数据模型。

当前企业 OSS 后端支持“已有 OSS token → 回查工号 → 映射平台账号”，还没有浏览器 SSO 跳转、回调及统一退出流程。登录页展示该能力状态，不要求用户粘贴 OSS token，也不伪造企业登录按钮。浏览器退出只撤销当前平台会话，不注销企业身份提供方会话或既有治理 API 令牌。

浏览器接口：`GET /api/auth/session` 查询身份与能力，`POST /api/auth/login` 账号登录，`POST /api/auth/logout` 服务端退出；响应禁用缓存。原 `/api/v1/auth/login`、`auth/api-key` 和 `auth/oss` 保持现有契约。

## 多用户权限与隔离

chat 页面与 `/api/chat/*`、`/api/conversations*` 已接入治理层用户体系。以下行为是冻结契约的实现，权限表、错误码判定顺序、域规则与迁移 DDL 见 [CHAT_MULTIUSER_DESIGN.md](CHAT_MULTIUSER_DESIGN.md)。

### 身份与域

- 页面身份来自同源 HttpOnly Cookie，或显式 `Authorization: Bearer <治理令牌>`；两者都按用户 id 重新读取账号状态，停用/锁定用户的下一次请求即 401（`user_disabled`）。
- `AGENT_BASE_WEB_TOKEN` 仅在本机 `hybrid` 模式映射为引导身份（BOOTSTRAP，最早创建的 active 平台管理员）；`jwt` 模式下共享 token 一律 401。没有任何治理账号时保留本机匿名引导。
- `user_type='service'` 的服务账号在 chat 页面面一律 403（`service_account_forbidden`），即使其角色包含相应权限。
- 顶部域下拉的选项来自 `GET /api/v1/me` 的 `domains[]`；选中值写 `sessionStorage` 并作为 `x-domain-id` 发送。该 header 只是选择器：服务端每次都校验 membership 与域状态，不接受前端推断。域不存在/未加入返回 403 `domain_forbidden`，多域用户未选择返回 400 `domain_required`，前端清空选择并重读 `/me`。

### 可见范围

- Agent 列表只返回当前域内 `status=ready` 的安装版本；Admin Agent 只对平台管理员出现，其他角色的列表里完全没有它。
- 会话列表默认只返回本人的会话（200 条/人），可跨本人已加入的域；`domain_id` 参数只能在本人的可域内取值。
- 按 id 访问会话、事件流、产物清单、下载，以及发消息、停止、改名、归档，全部经过同一归属校验：不存在、非本人、对象域不可见、以及 `owner_user_id IS NULL` 的存量会话，对普通用户一律 404（文案统一「对话不存在」，不泄露对象是否存在）。
- **NULL owner 语义（已决）**：迁移时没有 bootstrap admin 的存量会话保持 `owner_user_id IS NULL`。该状态对所有人都是 404，包括平台管理员的 `scope=all` 只读旁路；不做额外回填（glisxx 2026-11-20 选 A，采用 §5.3 冻结代码的顺序，详见设计文档 §16.4）。`domain_id IS NULL` 视为 `default` 域，读路径不依赖 NULL。

### 角色可见行为

| 行为 | viewer | developer / domain_admin | platform_admin | service 账号 |
| --- | --- | --- | --- | --- |
| 查看 Agent 列表、本域 | 可用 | 可用 | 可用（含 Admin Agent） | 403 |
| 查看/下载本人会话与产物 | 可用（有 `file.read` 时显示产物） | 可用 | 可用 | 403 |
| 新建对话、发消息、停止、改名/归档 | 403，按钮隐藏、输入框只读 | 可用 | 可用 | 403 |
| 只读查看他人会话（`scope=all`） | 403 | 403 | 可用，只读，每次落 `chat.scope_all` 审计 | 403 |
| 写他人会话 | 404 | 404 | 404（无写旁路） | 403 |

- 前端只按 `/me` 的 `domains[].permissions` 与 `platform_admin` 显隐入口，并在服务端拒绝时按 §6.4 处理；不缓存权限、不据角色自行推断。
- `scope=all` 只接受 `own` / `all` 两个值（其他值 400）；只用于列表与 4 条读路由，写路由不接受 `scope`，管理员对他人会话的写操作与缺失对象同为 404。
- `snapshot` 的 `files` 字段只在身份具备 `file.read` 时返回内容，否则为空数组。

### 并发、SSE 与审计

- 每用户默认 8 个活跃轮次（`web.per_user_max_turns`，1–32）与 4 条 SSE 连接（`web.per_user_max_streams`，1–32）；超限 429（`user_turn_quota` / `user_stream_quota`）。全局 32 轮仍是 409 `conversation_busy` 兜底，单会话同时 1 轮。检查与插入在同一 `BEGIN IMMEDIATE` 事务内。
- 流打开时的认证/权限/归属失败返回普通 401/403/404，不建立流；流建立后账号停用或权限被撤销，服务端在 30 秒内发送 `event: error` 与 `{"code":"unauthorized"}` 并关闭，不携带堆栈、路径或 token。
- 8 个 `chat.*` 审计动作（`chat.create` / `chat.send` / `chat.stop` / `chat.rename` / `chat.archive` / `chat.download` / `chat.scope_all` / `chat.migrate`）写入 `audit_log`，`level="chat"`，`trace_id` 对 send/stop 为本轮 `turn_id`；`detail` 不含消息正文、标题值、工具入参/结果、产物内容、token。`GET /api/v1/audit?trace_id=<turn_id>` 可串起同一轮的发送与停止，`?user_id=` 可列某人的 chat 动作。审计写失败只记录本地错误日志，不影响主流程。

### 错误码与前端动作

`{"detail": "<中文消息>", "code": "<稳定码>"}`：401 回登录（区分 `token_missing` / `token_invalid` / `token_expired` / `user_disabled`）；403 显示「无权限」并隐藏入口（`permission_denied` / `domain_forbidden` / `scope_denied` / `service_account_forbidden`）；404 视为已删除，从列表移除（`conversation_not_found` / `agent_not_found` / `artifact_not_found`）；409 提示当前轮次进行中并保留输入；429 提示达到个人上限并冷却；400 就地展示 `detail`；422 显示「请求格式不正确」。

## 部署前提

- 服务默认绑定 loopback，`TrustedHostMiddleware` 只接受 `localhost` / `127.0.0.1` / `[::1]` / `testserver`。这是安全边界，**不得为了「先能用」放宽 Host 校验**。
- 让其他主机的浏览器访问需要在自己前面加反向代理，并由代理终止 HTTPS：把请求转发到 `127.0.0.1` 上的服务，保持 `Host` 为上面允许的值（或在自己维护的代理配置里显式改 `allowed_hosts`），Cookie 的 `Secure` 依赖 HTTPS。浏览器到代理之间必须使用 HTTPS，否则会话 Cookie 等于明文传输。
- 反向代理不得把 `X-Forwarded-For` 当作身份来源；本机引导判定只看真实客户端地址，不信任转发头。

## 过渡限制

- 每用户 SSE 连接计数在进程内实现。`agent-base serve` 当前按**单进程**部署；多 worker / 多进程部署会各自计数，不能作为全局配额，本期不支持。
- 浏览器 Cookie 会话是 20 分钟**绝对到期**，没有滑动续期；到期必须重新登录。Bearer 客户端可调用 `POST /api/v1/auth/refresh` 换新令牌（无状态、不做过期宽限），持续活跃可一直续期，因此**没有绝对会话时长上限**。这两条是 C1 已明确、C7 记录的过渡限制。
- 单文件下载上限沿用 64 MiB；列表每用户 200 条。

## 前端构建与测试


生产静态文件已经构建到 Python 包的 static/chat 中，运行时不访问 CDN。复制项目后直接启动 serve 即可。

修改前端源码后，在 frontend 目录执行：

```sh
npm ci
npm run build
```

Python 测试在项目目录执行：

```sh
.venv/bin/pytest -q
```

浏览器测试在 frontend 目录执行 `npx playwright test`，共 31 条（登录、权限显隐、域选择、聊天、仪表盘、知识库、服务、定时页）。该套件已纳入默认门禁：`python3 scripts/ci.py full` 在构建后运行 `browser-ui` 阶段执行它，不再需要手工单独跑。macOS 优先使用已安装 Google Chrome；其他机器需要先 `npx playwright install chromium`。测试启动独立端口 8879，使用专用状态目录和 fake CLI，不调用模型，也不停止真实 repo 服务。

真实进程 E2E（`tests/e2e/*.spec.js`，登录、隔离、配额、审计、前端门控）由 `full` 的 `real-e2e` 阶段执行。`python3 scripts/ci.py contracts` 现在只做**必过集覆盖断言**：确认 §14 的 C1–C7 每条契约仍有具名测试、且这些套件仍被 `full` 执行；空套件或改名会直接失败，不再单独运行一套「未实现契约」。详见 [LocalCI.md](LocalCI.md)。


真实验收在已有模型凭据、三个示例 repo 及运行中的 serve 环境执行：

```sh
.venv/bin/python scripts/validate_chat.py
```

已验证 Admin 原生会话记忆、管理 MCP、三个 repo 的实际技能/工具、财务连续追问、消息去重、主动断开事件连接后任务继续，以及既有 MCP 服务未重启。记录和本机截图见 [VALIDATION.md](VALIDATION.md)。

当前网页连续会话使用 dsh；CodeAgent 的原生会话续接仍需内网适配。文件上传、图片输入、编辑分支和对话删除不在本版范围内；归档保留完整服务端记录和会话目录。
