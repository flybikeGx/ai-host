# 知识库包（迁移占位）

目标：`id: knowledge`，`first_party/knowledge/` 内包含后端、MCP server、人类 API、前端、测试和文档；`/knowledge`、`/api/knowledge/*`、工具名、服务名、数据根 `.agent-base/knowledge` 保持不变。

D1 不移动业务代码。当前实现仍在 `src/agent_base/knowledge*.py` 与 `frontend/src/KnowledgePage.jsx`。D2 按 [docs/FIRST_PARTY_MCP_REFACTOR.md](../../docs/FIRST_PARTY_MCP_REFACTOR.md) 用 `git mv` 迁入本目录，并新增 `mcp.yaml`（`installation.default: installed`）。

本目录在 D2 之前没有 `mcp.yaml`，`agent-base mcp packages` 会把它列为未加载的占位目录。
