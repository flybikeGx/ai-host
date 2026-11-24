# Agent Base

把 Git 仓库安装成可对话、可调度的 Agent。Python 管理安装、进程、任务和交付验证；CLI 运行时负责模型推理与工具循环。

## 开始使用

需要 Python 3.11+、uv、Git；默认 dsh 运行时还需要 Node.js 24.9+ 与 npm。

```sh
python3 scripts/bootstrap.py
.venv/bin/agent-base admin
```

在启动环境配置模型凭据，再向 Admin 描述工作，例如：

> 安装并运行 <Git 仓库地址>，分析 input 中的数据，生成报告，等待完成后给我产物。

完整安装、已有副本升级与 Windows 用法见 [安装指南](Installation.md)。

网页使用：

```sh
.venv/bin/agent-base serve
```

打开 <http://127.0.0.1:8765/>。已有平台账号从 `/login` 登录；首次管理员在本机通过 `agent-base control init` 初始化。

## 选择工作方式

| 需要做什么 | 入口 | 得到什么 |
| --- | --- | --- |
| 安装、运行或管理仓库 | Admin 对话 | 安装信息、run_id、状态与产物 |
| 连续探索与追问 | `/chat` | 独立会话与原生 CLI 上下文 |
| 一次性或周期执行 | `/cron` | 固定安装版本的计划及运行报告 |
| 管理共享 MCP | `/services` | 部署、发布、授权与故障状态 |
| 维护项目知识 | `/knowledge` | 正式知识、投稿审批与版本 |
| 看资源与队列 | `/dashboard` | 本机只读运行快照 |

安装不会自动完成业务任务；网页普通对话不自动执行仓库入口。需要独立产物检查时，提交后台 Run。

## 常用文档

- [文档导航](docs/README.md)：按目标查找指南。
- [常用规格与问题](docs/Specifications.md)：版本选择、默认值、容量限制、结果含义与排障。
- [导出自己的 Agent](docs/export-guild.md)：准备角色、安装清单和无人值守入口。
- [运行契约](docs/RepoContract.md)：任务参数、最终报告与可信验证。

当前适用于可信仓库、单机执行。账号与 RBAC 已存在，执行数据仍是全局工作台；目录、venv、命名空间不能替代多租户安全隔离。CodeAgent 仅有基础 print 适配，完整连续会话与内网环境仍需现场验收。

## 文档维护

面向人的指南保留操作、示例、规格、风险和验收结论。文件职责、强依赖和实现约束由 [AOCI 索引](aoci.txt) 维护，修改实现时仍需核对源码。[架构与维护约定](docs/ARCHITECTURE.md) 保留系统边界和设计决策；历史测试证据见 [验收记录](docs/VALIDATION.md)。
