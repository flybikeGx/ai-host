# 网页对话台

启动现有 Web 服务后访问 <http://127.0.0.1:8765/chat>。原工作调度和 Repo 资源页面仍在首页，两个页面互相有入口。

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
- 桌面与手机布局；Enter 发送、Shift+Enter 换行，中文输入法确认候选字时不会误发送。Cmd/Ctrl+K 新建对话。

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

消息与 API 使用原有 Bearer 认证、Host 和跨 Origin 写请求限制。若配置 AGENT_BASE_WEB_TOKEN，在对话台“连接设置”填写；Token 只保存在当前页面内存，不写入 localStorage。当前仍是本机可信用户管理界面，没有增加多用户账号系统。

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

浏览器测试在 frontend 目录执行 `npx playwright test`。macOS 优先使用已安装 Google Chrome；其他机器需要先 `npx playwright install chromium`。测试启动独立端口 8879，使用专用状态目录和 fake CLI，不调用模型，也不停止真实 repo 服务。

真实验收在已有模型凭据、三个示例 repo 及运行中的 serve 环境执行：

```sh
.venv/bin/python scripts/validate_chat.py
```

已验证 Admin 原生会话记忆、管理 MCP、三个 repo 的实际技能/工具、财务连续追问、消息去重、主动断开事件连接后任务继续，以及既有 MCP 服务未重启。记录和本机截图见 [VALIDATION.md](VALIDATION.md)。

当前网页连续会话使用 dsh；CodeAgent 的原生会话续接仍需内网适配。文件上传、图片输入、编辑分支和对话删除不在本版范围内；归档保留完整服务端记录和会话目录。
