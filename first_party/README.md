# 第一方 MCP 包

本目录容纳第一方 MCP 的完整交付单元：后端、MCP server、人类前端、清单、测试和文档同属一个包。规范见 [docs/MCP_PACKAGE.md](../docs/MCP_PACKAGE.md)，前端挂载见 [docs/MCP_FRONTEND.md](../docs/MCP_FRONTEND.md)，迁移计划见 [docs/FIRST_PARTY_MCP_REFACTOR.md](../docs/FIRST_PARTY_MCP_REFACTOR.md)。

```text
first_party/
  _shared/       跨包共享，不发布为 MCP
  _template/     新包骨架，以下划线开头，不参与运行时发现
  knowledge/     id: knowledge
  file_hub/      id: file-hub
  info_board/    id: info-board（AIHO-32）
```

约定：

- 根目录名为 `first_party`（可导入的 Python 顶层包），不是 `first-party`；包的 `id` 保留连字符，目录名使用下划线形式。
- 一个 MCP 一个子目录；包内文件不散落回 `src/agent_base/` 或 `frontend/src/`。
- 包之间不互相 import；共享代码只进 `_shared`。
- 每个包根目录一个 `mcp.yaml`；以下划线开头的目录不会被 `agent-base mcp packages` 发现。
- 迁移使用 `git mv` 保留历史，遵循 [MCP_PACKAGE.md](../docs/MCP_PACKAGE.md) 的安装/停用/数据边界。

D1 只落地骨架、模板和清单解析器；`knowledge/`、`file_hub/` 在 D2/D3 迁移前只有占位说明，没有可加载的清单。
