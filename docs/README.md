# 文档目录

项目入口见 [项目 README](../README.md)，环境准备见 [安装与启动](../Installation.md)，Admin Agent 的运行指令见根 [AGENTS.md](../AGENTS.md)。根目录保留这三个入口，其余说明集中在本目录。

除明确说明外，文档中的命令均在仓库根目录执行；代码片段中的文件路径也相对于仓库根目录。

## 使用与接入

| 文档 | 内容 |
| --- | --- |
| [Repo 安装与资源](RepoInstallation.md) | 清单、skills、常驻 MCP、资源命名空间和示例 |
| [共享 MCP 托管与看护](ManagedServices.md) | v3 服务包、安装/发布/绑定、监督器、看护 Agent、管理页和知识库接管 |
| [模型 provider 配置](Providers.md) | 内部网关路由、模型目录、凭据引用与失败行为 |
| [Repo 运行契约](RepoContract.md) | entrypoint、任务参数、结构化汇报和产物验证 |
| [本地 Agent 导出指南](export-guild.md) | 将已有本地 Agent 整理为可安装 repo |
| [网页对话](WebChat.md) | 多轮会话、流式事件、取消和接口 |
| [工作调度](Scheduling.md) | CLI、Web、cron 触发和运行生命周期 |
| [定时任务页面](SchedulingUI.md) | 一次性计划、周期计划和运行报告 |
| [运行仪表盘](Dashboard.md) | Agent、MCP、工作区和系统指标 |
| [项目知识库](Knowledge.md) | 项目绑定、检索、投稿、审批和版本 |
| [CodeAgent 迁移指南](CODEAGENT_MIGRATION.md) | 内网环境准备、适配边界和验收路线 |
| [Git 与离线交接](GIT_GUIDE.md) | 双远端、离线 bundle 和源码交接 |

## 设计与验证

| 文档 | 内容与状态 |
| --- | --- |
| [项目设计与 TODO](DESIGN.md) | 当前实现、架构和后续任务 |
| [第二阶段：MCP 托管、发布与看护](PHASE_2_MANAGED_MCP_DESIGN.md) | 已实施（本地验收完成）；设计背景、公共契约与实现差异 |
| [项目设计书草稿](PROJECT_DESIGN_DRAFT.md) | 立项与汇报材料，包含拟建设能力 |
| [知识库设计存档](KNOWLEDGE_BASE_DESIGN.md) | 首版设计及尚未实现的扩展 |
| [用户隔离设计草案](USER_ISOLATION_DESIGN.md) | 暂缓实施，当前仍为单用户实现 |
| [验收记录](VALIDATION.md) | 自动回归、真实 CLI 验证和已知限制 |

界面截图保存在 [assets/](assets/)。
