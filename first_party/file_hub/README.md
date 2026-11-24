# 文件中转站包（迁移占位）

目标：`id: file-hub`，`first_party/file_hub/` 内包含后端、MCP server、HTTP 数据面、人类 API、前端、测试和文档；`/files`、`/api/files/*`、`/data/*`、工具名、服务名、数据根 `.agent-base/file-hub` 保持不变。

**依赖**：FileHub 代码在 `agent/ds/aiho-30`、`agent/ds/aiho-31` 分支上，本任务起始提交尚未包含。D3 开始前必须先合并 AIHO-31（或等价的 gitee/main 合并），否则本目录保持占位、没有可迁移实现。

D3 按 [docs/FIRST_PARTY_MCP_REFACTOR.md](../../docs/FIRST_PARTY_MCP_REFACTOR.md) 用 `git mv` 迁入，并新增 `mcp.yaml`（`installation.default: installed`）。

本目录在 D3 之前没有 `mcp.yaml`，`agent-base mcp packages` 会把它列为未加载的占位目录。
