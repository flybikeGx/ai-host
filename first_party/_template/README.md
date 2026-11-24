# 模板包

`first_party/_template` 是新建第一方 MCP 包的骨架，目录名以下划线开头，不参与运行时发现。

使用方式（D4 提供 `agent-base mcp new <id>` 脚手架后自动完成）：

1. 复制本目录为 `first_party/<id 的 - 换成 _>/`；
2. 修改 `mcp.yaml` 的 `id`、`title`、`version`、`contract`、`frontend.route`、`permissions`；
3. 在 `server.py` 中替换工具实现，保留 `/health` 和 `/mcp`；
4. 在 `frontend/src/index.jsx` 中替换页面；
5. 运行 `agent-base mcp validate first_party/<id>`，再补测试和文档。

当前模板只提供 `echo` 工具，用于验证清单、导航和安装流程；它默认 `installation.default: available`，不会被自动安装。
