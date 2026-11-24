# 使用指南

首次使用先读 [安装与启动](../Installation.md)，日常限制与排障查 [常用规格与问题](Specifications.md)。命令默认在仓库根目录执行。

| 你的目标 | 指南 |
| --- | --- |
| 接入自己的模型网关 | [Provider 配置](Providers.md) |
| 把本地 Agent 整理成仓库 | [导出指南](export-guild.md) |
| 安装仓库、复用技能与 MCP | [Repo 安装](RepoInstallation.md) |
| 定义后台任务与报告 | [运行契约](RepoContract.md) |
| 聊天、登录与下载产物 | [网页对话](WebChat.md) |
| 用页面安排任务 | [定时页面](SchedulingUI.md) |
| 用 CLI/API 注册工作 | [工作调度](Scheduling.md) |
| 安装、发布与授权共享 MCP | [共享服务](ManagedServices.md) |
| 管理正式知识与审批投稿 | [知识库](Knowledge.md) |
| 发布与浏览 Markdown 信息 | [信息台](InfoBoard.md) |
| 理解指标与未知值 | [仪表盘](Dashboard.md) |
| 迁入企业内网 | [CodeAgent 迁移](CODEAGENT_MIGRATION.md) |
| 同步 Git 与离线交接 | [Git 指南](GIT_GUIDE.md) |

## 维护资料

- [架构与维护约定](ARCHITECTURE.md)：产品边界、关键决策和待办方向。
- [验收记录](VALIDATION.md)：保留带日期的测试证据，不代表当前再次执行通过。
- [历史多用户审查](MULTI_USER_AUDIT.md)：特定提交的审查结果，现行登录规则以网页指南和源码为准。
- [治理设计草案](../GOVERNANCE_DESIGN.md)：历史提案，不能作为已实现能力清单。

## 人类指南与 AI 索引的分工

指南说明如何使用、可配置什么、遇到问题怎么办。公共规格和权限边界必须对人可见，不能只写进 AOCI。AOCI 记录文件职责、关系、接口与容易误改的实现约束；AGENTS 保留 Agent 执行规则。修改代码后同步受影响索引，避免再在指南维护逐文件模块地图。

文档更新以代码和已核对的验收证据为依据。新增能力先明确边界；只更新相关指南与规格，不复制整套背景。截图位于 [assets](assets/)。
