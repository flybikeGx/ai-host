# 第一方 MCP 目录化：迁移清单、兼容性与回滚

本文是 AIHO-33 的迁移执行计划。D1 只做设计冻结和解析器原型；D2 起按“知识库试点 → 文件枢纽 → 模板/第三方示例”逐阶段迁移，每阶段可独立评审、独立回滚。规范字段见 [MCP_PACKAGE.md](MCP_PACKAGE.md)，前端挂载见 [MCP_FRONTEND.md](MCP_FRONTEND.md)。

## 1. 迁移前现状

### 1.1 知识库（本分支已存在）

| 类别 | 文件 |
| --- | --- |
| 后端/存储 | `src/agent_base/knowledge.py`、`knowledge_migration.py` |
| MCP | `knowledge_mcp.py`、`knowledge_server.py` |
| 进程管理 | `knowledge_service.py` |
| 人类 API | `knowledge_web.py` |
| 前端 | `frontend/src/KnowledgePage.jsx`、`knowledge.css`、`frontend/tests/knowledge.spec.js` |
| 测试 | `tests/test_knowledge.py`、`tests/test_knowledge_migration.py` |
| 文档 | `docs/Knowledge.md` |
| 平台接线 | `web.py`（`/knowledge`、`register_knowledge`、lifespan 启动）、`cli.py`（`knowledge` 子命令）、`mcp_discovery.py`、`knowledge_service.attach` 调用方 |

现状特征：服务名 `knowledge`，工具 `knowledge_catalog/search/read/submit`，数据根 `.agent-base/knowledge`，控制凭证 `data/knowledge/service-token`，MCP 端点由子进程自报随机回环端口；页面在同一个 SPA 内硬编码。

### 1.2 文件中转站（不在本任务基线）

FileHub 的代码在 `agent/ds/aiho-30` 与 `agent/ds/aiho-31` 分支上（AIHO-30 服务、AIHO-31 页面），本任务起始提交 `d4182d4` 尚未包含它们。**D1/D2 不受影响；D3 开始前必须先合并 `agent/ds/aiho-31`（或等价的 gitee/main 合并）**，否则没有可迁移的实现。这是本任务的依赖风险，不是设计缺口。

| 类别 | 文件（AIHO-31 分支） |
| --- | --- |
| 后端/存储 | `src/agent_base/filehub.py`、`filehub_http.py` |
| MCP | `filehub_mcp.py`、`filehub_server.py` |
| 进程管理 | `filehub_service.py` |
| 人类 API | `filehub_web.py` |
| 前端 | `frontend/src/FilesPage.jsx`、`files.css`、`frontend/tests/files.spec.js` |
| 测试 | `tests/test_filehub.py`、`test_filehub_mcp.py`、`test_filehub_runtime.py`、`test_filehub_web.py` |
| 文档 | `docs/FileHub.md` |

现状特征：服务名 `file-hub`，工具 `file_spaces/list/stat/put/delete/...`，数据根 `.agent-base/file-hub`，同一进程同时提供 MCP 控制面和 `/data/...` 数据面，页面硬编码 `/files`。

### 1.3 信息台（尚未实现）

AIHO-32 按本规范新增 `first_party/info_board/`，`id: info-board`，路由 `/info`，`installation.default: available`，不修改主应用散落文件。

### 1.4 必须留在平台的通用代码

`web.py`（宿主、登录、通用 API）、`config.py`、`mcp_server.py`（Admin MCP）、`service_catalog.py`/`service_install.py`（共享服务部署）、`governance/`、`scheduler.py`、`repositories.py`、`mcp_discovery.py`、`processes.py`、`store.py`。包只通过清单和这些稳定接口接入。

## 2. 目标目录（最终形态）

```text
first_party/
  _shared/
    frontend/          # 壳、design token、Markdown、API client、测试 helper
    python/            # 清单校验、服务进程/健康探测公共实现
    tests/
  _template/
    mcp.yaml
    server.py
    frontend/src/index.jsx
    tests/
    docs/
  knowledge/
    mcp.yaml
    backend/
      __init__.py
      knowledge.py        # 原 knowledge.py
      migration.py        # 原 knowledge_migration.py
      mcp.py              # 原 knowledge_mcp.py
      server.py           # 原 knowledge_server.py
      service.py          # 原 knowledge_service.py（改为共享 runtime 适配器）
      web.py              # 原 knowledge_web.py
    frontend/
      src/index.jsx       # 由 KnowledgePage.jsx 改名/薄封装
      src/knowledge.css
    tests/
      test_knowledge.py
      test_knowledge_migration.py
      knowledge.spec.js
    docs/Knowledge.md
  file_hub/
    mcp.yaml
    backend/{filehub.py,filehub_http.py,mcp.py,server.py,service.py,web.py}
    frontend/src/{index.jsx,files.css}
    tests/{test_filehub.py,test_filehub_mcp.py,test_filehub_runtime.py,test_filehub_web.py,files.spec.js}
    docs/FileHub.md
  info_board/          # AIHO-32
```

模块命名规则：包内使用相对 import（`from .knowledge import Knowledge`），不再使用 `agent_base.knowledge`。平台侧改为 `from first_party.knowledge.backend import ...`。`first_party` 加入 `pyproject.toml` 构建目标，服务以 `python -m first_party.knowledge.backend.server` 启动。

## 3. 分阶段迁移

### D1（本次交付）

- 三份设计文档；
- `first_party/` 骨架、`_shared`/`_template` 说明与最小模板包；
- `src/agent_base/mcp_packages.py` 清单解析/加载器原型、安装状态语义；
- `agent-base mcp packages` / `mcp validate` 只读命令；
- 单元测试覆盖清单校验、状态缺省、导航/前端 registry 过滤；
- **不改任何 MCP 业务代码，不移动文件，不改 URL/数据/工具契约**。

### D2：知识库试点

1. `git mv` 上表文件到 `first_party/knowledge/`，只改 import 和路径；
2. 新增 `mcp.yaml`（`installation.default: installed`，数据目录 `knowledge` 不变）；
3. `KnowledgeService` 改为通用服务 runtime 的第一个适配器：平台仍生成 `service-token`、读取 `service.json`、探测 `/health` 和 MCP 工具；对外行为不变；
4. `web.py` 去掉硬编码 `/knowledge` 和 `register_knowledge`，改由清单注册路由并调用 `first_party.knowledge.backend.web.register`；
5. `cli.py knowledge status/start/stop/restart` 保留为兼容别名，内部走包状态；
6. 前端把 `KnowledgePage`/`knowledge.css` 搬到包内，`/knowledge` 页面与视觉不变；registry 生成脚本上线；
7. 测试：知识库单测、迁移测试、Playwright、安装/停用/启用（先只针对知识库）、URL/工具/数据对照。

验收门槛：知识库现有测试全过；停用后 `/knowledge` 404、服务停止、Agent 不再注入；启用/重启后数据、文档版本、投稿、审计都在；`mcp packages` 显示 `knowledge` 已安装且启用。

### D3：文件枢纽 + 按需安装

1. 先合并 AIHO-31 分支（或等价提交），确认 FileHub 基线测试通过；
2. 按 D2 同样的方式迁移 `filehub*` 到 `first_party/file_hub/`，`/files`、`/api/files/*`、`/data/*`、工具名、服务名、数据根 `.agent-base/file-hub` 全部不变；
3. 实现 `mcp install/enable/disable/uninstall` 的真实生命周期：服务启动/停止、Agent 注入门控、导航/页面 404、状态文件、文件锁、幂等；
4. `serve`/`worker` 启动时只拉起已启用包；`web.py` lifespan 不再无条件 `KnowledgeService.ensure`；
5. `mcp list` 增加 `packages` 段；`/api/mcp/packages` 提供导航数据；
6. 测试：停用/启用、重复安装、升级、数据保留、审计连续性、单包启用时其他包不可见、旧数据自动识别；
7. 全量回归（Python + Playwright）。

### D4：模板、第三方示例与收口

1. `agent-base mcp new <id>` 脚手架：复制 `_template`、替换 id/title/route、生成 `mcp.yaml` 和空测试；
2. `examples/first-party/hello/`（或 `_example/`）最小第三方包：一个工具 + 一个 `static-iframe` 页面，走同一清单和安装流程；
3. 验证：安装后可调用、导航可进入、停用后入口消失、数据保留、能力票据隔离；
4. 更新 `docs/README.md`、`docs/ARCHITECTURE.md`、`docs/ManagedServices.md`、`docs/Specifications.md`；
5. 干净 checkout 从安装到运行走一遍；`python3 scripts/ci.py full` exit 0（见 §7 环境说明）；
6. 交付 `in_review`，不合并 main、不部署。

## 4. URL、数据与契约兼容

| 对外项 | 迁移前 | 迁移后（D2/D3） | 验证方式 |
| --- | --- | --- | --- |
| 知识库页面 | `/knowledge` | 不变 | Playwright + 手动 |
| 知识库 API | `/api/knowledge/*` | 不变 | 现有 `test_knowledge*` |
| 知识 MCP 服务名 | `knowledge` | 不变 | `mcp_discovery` / MCP 客户端 |
| 知识工具名 | `knowledge_catalog/search/read/submit` | 不变 | 工具目录快照对比 |
| 知识数据根 | `data/knowledge` | 不变 | 迁移前后 SQLite 摘要 |
| 知识控制凭证 | `data/knowledge/service-token` | 不变（由平台生成/读取） | 健康探测 |
| 文件页面 | `/files` | 不变 | Playwright |
| 文件 API/数据面 | `/api/files/*`、`/data/*` | 不变 | 现有 `test_filehub*` |
| 文件 MCP 服务名/工具 | `file-hub`、`file_*` | 不变 | 工具目录对比 |
| 文件数据根 | `data/file-hub` | 不变 | 迁移前后摘要 |
| 信息台页面 | 无 | `/info`（新） | AIHO-32 |
| 管理命令 | `agent-base knowledge ...` | 保留为别名 | CLI 冒烟 |

**授权与审计**：知识 grant、ServiceCatalog 绑定、文件 scope、审计表/字段都不迁移。D2/D3 只换加载位置，`grant` 的 actor/source 记录保持可解析；旧绑定在迁移后继续有效。

**URL 不变**的硬约束：包清单的 `frontend.route` 必须等于现有路径；服务端代理/网关路径不变；不在迁移中改 MCP 端点的路径 `/mcp`、健康路径 `/health`、数据面路径。

## 5. 安装状态迁移

- 状态文件 `<state>/mcp/installations.json`；
- 首次读取时，`installation.default: installed` 的包（知识库、文件枢纽）即使没有记录也视为“已安装 + 已启用”，来源 `default/legacy`；
- 若检测到现有数据根/`service.json`/`disabled` 标记，只补写记录，不移动、不重命名、不删除数据；
- 已停用的旧服务（`disabled` 文件存在）迁移为 `installed: true, enabled: false`；
- 删除状态文件恢复到缺省推导，等同迁移前行为；
- 信息台 `default: available`，不会被自动安装；
- 升级记录版本 + 清单摘要；摘要不一致时标记 `stale`，由显式升级切换，不静默换版本。

## 6. 回滚方案

| 阶段 | 回滚动作 | 数据影响 |
| --- | --- | --- |
| D1 | 回滚本次提交；解析器未被任何运行路径引用 | 无 |
| D2 | `git revert` D2 提交；删除 `mcp/installations.json`；恢复 `web.py` 硬编码路由 | 数据根未移动，知识数据/审计保留 |
| D3 | 回滚文件枢纽迁移与安装命令；保留 `.agent-base/file-hub`；旧 `FileHubService` 按原路径启动 | 上传会话按 TTL 过期，已提交文件保留 |
| D4 | 回滚脚手架/示例；不影响已安装的三个包 | 示例数据可删 |

不保留“新旧两套加载路径”长期共存：迁移期每个包一个提交，回滚以提交为单位；状态文件是新增文件，旧代码忽略它。服务/端口/数据目录不变使回滚不需要数据迁移。

## 7. 测试矩阵与环境

| 层 | D1 | D2 | D3 | D4 |
| --- | --- | --- | --- | --- |
| 清单解析/校验 | 单元测试（未知字段、路由、占位符、路径越界） | 知识库真实清单 | 文件枢纽真实清单 | 模板/第三方清单 |
| 安装状态 | 缺省推导、停用/启用、幂等 | 知识库记录写入 | 双包独立门控、升级、卸载 | 示例安装/卸载 |
| 服务生命周期 | — | 启动/停止/健康/重启 | 同左 + 不启动未启用包 | 示例包 |
| URL/API/工具/数据 | — | 与迁移前逐项对照 | 同左 | 示例 |
| 前端 | registry 单元测试 | `/knowledge` Playwright、导航门控 | `/files`、停用 404 | 第三方 iframe/票据 |
| 安全 | 路径/清单校验 | 同源会话、CSP | 能力票据、sandbox | 同左 + 越权 |
| 全量回归 | `pytest` | `pytest` + Playwright | 同左 | `scripts/ci.py full` |

**当前基线说明**：本任务起始提交 `d4182d4` 没有 `scripts/ci.py`（该脚本在 `gitee/main` 与 AIHO-31 分支上），因此 D1 用 `uv run --frozen pytest` 和前端构建校验；D2 合并 gitee/main 后再跑 `scripts/ci.py full`。验证平台是单台 arm64 Mac；Windows 原生生命周期、真实企业代理/证书、真实大文件 10 GiB 不在本期验证范围，如实记录。

## 8. 风险与未决项

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| 基线缺少 AIHO-31 代码 | D3 无法开始 | 先合并 `agent/ds/aiho-31` 或等 gitee/main 合并；D1/D2 不受影响 |
| 基线缺少 `scripts/ci.py` | 无法执行任务书要求的全量 CI | D1 用 pytest/构建；D2 同步 gitee/main 后补跑 |
| 模块移动改 import 面大 | 回归风险 | 每包单独提交、只改 import；保留兼容别名；逐项对照 URL/工具/数据 |
| 前端抽壳 | 视觉回归 | D2 先搬后改，`style.css` token 化不改变计算样式；Playwright 截图/交互对照 |
| 知识库兼容 grant | 旧绑定失效 | D2 不迁移授权表和 actor 记录；迁移后旧 grant 继续解析 |
| 服务自报端口 vs 平台注入端口 | 迁移期行为差异 | 知识库 D2 保留自报端口适配器；新包才用 `${service.port}` |
| 第三方隔离 | 同源 iframe 可读 cookie | 强制 `capability-token` + 无 `allow-same-origin`；见 MCP_FRONTEND |

## 9. 评审通过后的第一步

D2 只做知识库：先提交“移动 + import + 清单 + 服务适配器”，再提交“前端 registry + 导航门控”，最后补测试与对照记录。文件枢纽和信息台在 D2 验收通过后再动，避免一次性大爆炸。
