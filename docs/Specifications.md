# 常用规格与问题

本文汇总当前代码中的默认值、上限和操作边界。示例清单可以覆盖默认值；运行时配置、清单与任务时限需分别核对。完整请求格式在服务的 `/docs`，仓库报告格式见 [运行契约](RepoContract.md)。

## 版本与资源选择

| 场景 | 规格 |
| --- | --- |
| 老仓库兼容 | schema_version:1，每次 Run 安装资源 |
| Repo 长期安装 | schema_version:2，固定 skills、环境和常驻私有 MCP |
| 跨 Agent 共享服务 | schema_version:3，服务部署、发布、Agent 绑定分别管理 |
| 第一方 MCP 包 | `first_party/<id>/mcp.yaml`，schema_version:1、kind:mcp-package；目录名 = id 中的 `-` 换 `_` |
| 安装机器配置 | 根 agent.yaml 或 Installation.md 唯一 agent-base-install 块，不能双写 |
| 服务传输 | stdio、streamable_http；外部服务仅经本机接入层代理 |
| 安装版本 | 固定 commit，更新源码后需重新安装 |
| 定时目标 | 已安装且启用、提供 entrypoint.md 的 Repo，绑定固定安装版本 |
| 默认运行时 | dsh；CodeAgent 仅基础 print，任务并发要求为 1 |

## 默认值与容量

| 项目 | 当前值 | 使用时注意 |
| --- | --- | --- |
| Repo 执行并发 | 默认 2，可配 1–32 | Admin 对话不占 Repo 槽位 |
| 等待队列 | 100 个 queued Run | 满队列时触发不受理 |
| 对话活动/排队总数 | 32 轮 | 每个对话最多 1 个活动轮次 |
| runtime.timeout_seconds | 默认 1800，范围 1–86400 秒 | 排队不计时；实际 Run 受工作/清单时限约束 |
| 任务文本 | 最大 256 KiB | UTF-8 字节数 |
| AGENTS / entrypoint | 各最大 128 KiB | entrypoint 非空、UTF-8、普通文件 |
| 复制输入 | 最大 256 MiB、10000 条目 | 执行开始时复制；拒绝符号链接和特殊文件 |
| 触发 payload | JSON 对象，最大 64 KiB | 触发参数不是额外授权 |
| 单条聊天消息 | 最大 64 KiB | 每次消息有独立执行时限 |
| 最终 Agent JSON | 最大 256 KiB | 严格校验，拒绝重复字段与非有限数值 |
| 单个交付/下载文件 | 最大 64 MiB | 位于受管 artifacts/ 内 |
| 对话文件列表 | 最多 100 个 | 列表不是完整存储统计 |
| 浏览器账号会话 | 20 分钟 | 无静默刷新，退出服务端撤销 |
| 知识一次性管理链接 | 120 秒、使用一次 | 由 agent-base open 打开 |
| 知识正文 | 最大 128 KiB | 当前无附件/PDF 自动导入 |
| 知识工具响应 / 章节正文 | 最多 8000 / 6000 个字符 | 通过分页或游标继续 |
| 共享服务健康检查 | 默认间隔 30 秒、超时 5 秒 | 连续失败 3 次异常、成功 2 次恢复 |
| 服务启动宽限 | 默认 60 秒 | 示例/部署配置可能覆盖为 30 秒 |
| 自动恢复 | 默认 10 分钟最多 3 次 | 退避 30/60/120 秒；仍受维护、排空和授权约束 |
| 看护巡检 | 默认关闭，启用后默认间隔 300 秒 | 确定性监督不依赖看护模型可用 |
| 第一方 MCP 安装状态 | `data/mcp/installations.json` | 缺省条目按清单 `installation.default` 推导；停用/卸载默认保留数据根 |
| 包页面挂载 | 第一方 `spa-module`、第三方 `static-iframe` | 页面与导航由清单和启用状态决定；停用后路由 404 |

实现依据：[配置](../src/agent_base/config.py)、[队列和输入](../src/agent_base/scheduler.py)、[对话](../src/agent_base/conversations.py)、[报告](../src/agent_base/reports.py)、[服务清单](../src/agent_base/manifests.py)、[知识库](../src/agent_base/knowledge.py)。变更限制时同步此表。

## 状态与权限

| 容易混淆的问题 | 答案 |
| --- | --- |
| 安装成功等于任务完成？ | 不等于。prepared 只表示已准备，可 start 一次 |
| Agent 说 completed 就成功？ | 以 Run 状态和 verification 为准；自报检查不是独立验证 |
| 发布服务就能被 Agent 调用？ | 还需显式绑定；声明 permissions 只是申请 |
| 停 Agent 会停共享 MCP？ | 不会，共享部署独立管理；repo MCP 也不随任务结束停止 |
| 请求取消就停了吗？ | 查询到 cancelled 才确认停止 |
| 关闭网页/Admin 会取消后台任务？ | 不会；需要显式取消 |
| 失败或 interrupted 自动重试？ | 不会，先核对结果和副作用，再决定新任务 |
| 能让多个不可信用户共用吗？ | 当前不具备多租户强执行隔离；账号/RBAC 不能代替沙箱 |
| 外部服务能重启吗？ | 平台只管理本机路由，不管理远端进程 |
| 安装/启用 MCP 包就授权 Agent 吗？ | 不；工具授权仍以显式绑定或知识 grant 为准 |
| 修改绑定就收紧所有旧 grant？ | 不能这样假设；旧 grant 未比较绑定修订号，撤权需用解绑流程 |

## 常见故障

| 现象 | 优先检查 | 下一步 |
| --- | --- | --- |
| doctor 通过但任务失败 | doctor 只测本机配置 | 用短任务验证模型、网络与工具 |
| 任务一直排队 | serve/worker 是否常驻、并发是否已满 | 查看 dashboard 与 Run 状态 |
| 没有无人值守入口 | entrypoint、显式 task、task.default | 三者至少提供一个具体任务 |
| 调度时间不符合预期 | IANA 时区、下一次预览、夏令时 | [定时页面](SchedulingUI.md) |
| 报告被判失败 | 最终 JSON、产物路径、冻结检查日志 | [运行契约](RepoContract.md) |
| MCP 不可用 | 部署/安装状态、协议探测、脱敏日志 | [共享服务](ManagedServices.md) |
| 知识投稿审批冲突 | 基线版本与当前正式版不同 | 比较差异，人工合并后发布 |
| 登录后只有账号页 | 缺少全局工作台权限 | [登录说明](WebChat.md#账号登录与会话) |
| 指标显示未知或下限 | 初次采样、权限、归属或扫描上限 | [仪表盘](Dashboard.md) |

迁移知识库前先生成计划并核对停机影响。当前复制模式也可能短暂停止并恢复健康的旧实例以取得快照，不能承诺不停机；正式业务实例需要明确停机授权。就地接管另需显式授权。完整流程见 [共享服务](ManagedServices.md)。
