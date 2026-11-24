# `_shared`：第一方 MCP 的共享实现

本目录只放跨包共享、且不属于某个 MCP 业务域的实现。包之间不得互相 import；需要复用的代码先进入这里。

D1 只冻结职责，不搬运代码。D2 起按 [docs/MCP_FRONTEND.md](../../docs/MCP_FRONTEND.md) 和 [docs/FIRST_PARTY_MCP_REFACTOR.md](../../docs/FIRST_PARTY_MCP_REFACTOR.md) 从现有前端/后端抽取：

| 目录 | 内容 | 来源（D2 抽取） |
| --- | --- | --- |
| `frontend/` | 设计 token、基础样式、壳组件、Markdown 渲染、API client、Playwright helper | `frontend/src/style.css`、`auth.css`、`api.js`、`main.jsx` 的 Markdown 配置 |
| `python/` | 清单校验、服务进程/健康探测公共实现、状态文件读写 | `mcp_packages.py` 的稳定性接口，`knowledge_service`/`filehub_service` 的公共部分 |
| `tests/` | 共享测试夹具（登录、导航、临时状态根） | `tests/conftest.py`、`frontend/tests` helper |

约束：

- 不复制业务页面/业务存储；`_shared` 不依赖任何一个具体包。
- 前端共享代码不是 npm 包，第一阶段用相对路径 import；不发布到公共 registry。
- 后端共享代码不替包做授权决策；权限仍由平台 API 和现有绑定/grant 校验。
