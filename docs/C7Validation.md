# C7 验证记录：回归、迁移演练与三角色走查（AIHO-20）

> 日期：2026-11-20。仅单台 arm64 Mac 实测，不代表其他成员或架构已验证。验证过程不 push、不合并、不部署，不修改全局配置。

## 环境

| 项 | 值 |
| --- | --- |
| 系统 / 架构 | macOS 26.6.2 / arm64 |
| 入口 Python | 3.15.2（`python3`）；uv 环境 Python 3.13.14 |
| Node / npm | 26.4.0 / 12.0.2 |
| Playwright / Chromium | 1.64.1 / Google Chrome for Testing 156.0.8078.4（`browser-version`、`chromium-version` 阶段） |
| Git / uv | 2.54.0 / 0.11.26 |

依赖按 `uv.lock` / `package-lock.json` 冻结安装（`python3 scripts/ci.py bootstrap`，exit 0）。测试只用本机 `tests/fixtures/fake_dsh.py` 确定性执行器，不调用模型、不访问外部服务。

## 1. 默认门禁的两处收口

C6 遗留的两个门禁缺口在本次修复：

1. **前端套件进入 `full`**：`scripts/ci.py full` 在 `build` 之后新增 `browser-ui` 阶段，执行 `frontend/tests` 的 31 条 Playwright（真实 Chromium + fixture 后端）。C1–C6 新增/扩展的测试现在都在默认套件里，不再需要手工命令。
2. **契约模式不再是空套件**：`tests/e2e/contracts.spec.js` 删除后 `ci.py contracts` 会是 0 测试。现在 `contracts` 改为**必过集覆盖断言**（`scripts/contracts.py`）：
   - 对 `docs/CHAT_MULTIUSER_DESIGN.md` §14 的 C1–C7 逐条断言仍有具名 pytest / 真实 E2E / `frontend/tests` 用例；
   - 断言 `full` 仍执行 `unit`、`browser-ui`、`real-e2e`、`mock-restart`（读 `scripts/ci.py` 与 `scripts/test_real_e2e.py`）；
   - 断言 pytest 收集数 ≥ 320、真实 E2E ≥ 31、`frontend/tests` ≥ 31，空套件/改名/删套件直接 exit 1；
   - `tests/test_contracts_coverage.py` 自检「空套件不能通过」「从 full 移除 browser-ui 会失败」。
   - `scripts/test_real_e2e.py` 移除 `--contracts` 分支与 `--pass-with-no-tests`，契约行为只由 `full` 执行。

## 2. 全量回归

命令与结果（交付评论给出对应的最终 commit 与报告目录；报告含 `report.json`、各阶段日志与 Playwright JSON/HTML）：

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python3 scripts/ci.py full` | 0 | `unit` 329 passed / 3 skipped；`build`；`browser-ui` 31 passed（37.8s）；`browser-version`；`chromium-version`；`real-e2e` 35 passed（9.1s）；`mock-restart` MU-14 characterization PASS |
| `python3 scripts/ci.py contracts` | 0 | C1–C7 全部 `covered`，`contracts.json` 写入报告目录 |

- 3 个 skip 是既有测试对本地可选 `.runtime/dsh` 的依赖（与 AIHO-23 基线相同），不是本次新增的跳过；1 条 Starlette/httpx 弃用警告为既有。
- 测试与 §14 矩阵的逐项对应见 `scripts/contracts.py` 的登记表；C1–C5 在 pytest，C6 在 `frontend/tests` 与 `tests/e2e/chat_ui.spec.js`，C7 为全量回归＋本记录。
- 对照：C6 交付时 `frontend/tests` 31 条只能手工跑（`npx playwright test`），`contracts` 为 0 测试；本次两者都已成为默认门禁的一部分。

## 3. 迁移演练

### 数据来源

使用本机真实工作区数据库的**副本**，不改动原库：

```sh
# 源：/Users/flybike/code/ai-host/agent-base/.agent-base/{runs.sqlite,control.sqlite}
# 用 SQLite backup API 从 immutable 只读连接复制到演练目录
python3 - <<'PY'
import sqlite3
from pathlib import Path
src = Path('/Users/flybike/code/ai-host/agent-base/.agent-base')
out = Path('.c7-drill2/data')
for name in ('runs.sqlite', 'control.sqlite'):
    a = sqlite3.connect(f'file:{src/name}?immutable=1', uri=True)
    b = sqlite3.connect(out/name)
    a.backup(b); a.close(); b.close()
PY
```

### 演练命令

```sh
.venv/bin/python scripts/c7_migration_drill.py --data .c7-drill2/data --work .c7-drill2/work
# exit 0；完整 JSON 见附件 c7-migration-drill.log
```

脚本用生产启动路径（`create_app` 的启动钩子 + 真实 uvicorn）跑两次启动，再用 C2 之前的代码（`git archive 79ccc88`）读迁移后的副本。

### 结果

| 项 | 结果 |
| --- | --- |
| 迁移前形状 | `conversations` 6 行、`conversation_turns` 8 行、`conversation_events` 189 行、`runs` 14 行、`repo_installations` 4 行；`conversations` 无 `owner_user_id` / `domain_id` |
| 备份文件 | `data/runs.sqlite.pre-chat-multiuser`，446,464 字节，sha256 `9282f61584d219d8aae1b0ec8bfea3c5e58ab9b3a55f9640127a1d2313712da4`，保留旧列与旧数据 |
| 首次启动耗时 | 0.311 s（进程启动到 `/api/auth/session` 200） |
| 迁移结果 | 新增 `owner_user_id` / `domain_id` 列与 `conversations_owner` / `conversations_domain` 索引；6 个会话 owner 全部回填为 bootstrap admin `5733093c…`；6 个会话与 4 个安装的 `domain_id` 回填为默认域 `18e8da3c…`；`chat.migrate` 审计 1 条，`detail={"conversations_owner":6,"conversations_domain":6,"repo_installations_domain":4,...}` |
| 行数与内容 | 6/8/189/14/4 行数不变；会话、轮次、事件的排序内容 sha256 与迁移前完全一致（无删除、无改写） |
| 二次启动 | 0.295 s；`chat.migrate` 审计仍为 1 条（回填 0 行、不重复审计）；owner/domain 与内容摘要不变；备份文件 sha256 不变 |
| 回滚旧代码 | 用 `79ccc88`（C2 之前）的 `Conversations` 读迁移后的副本：exit 0，`OLD_CODE_OK 6 8`，新增列仍在、旧代码忽略它们且可继续读；副本内容摘要不变 |
| 结论 | 列是纯增量；备份为一次性、用 SQLite backup API 生成且不覆盖；旧代码可读；**没有删除任何数据** |

## 4. 三种角色手工走查

```sh
.venv/bin/python scripts/c7_walkthrough.py
# exit 0，46 项检查全部 PASS；完整输出见附件 c7-walkthrough.log
```

走查在隔离临时目录启动真实 uvicorn + 真实 SQLite + 真实 HTTP/SSE，账号为测试夹具（viewer / developer / platform_admin / service），执行器为 `fake_dsh.py`。原工作区与真实账号未被访问。

| 角色 | 走查内容与结论 |
| --- | --- |
| viewer | 登录；`/me` 域权限无 `agent.run` / `task.write`；Agent 列表只有本域的 Default Agent、无 Admin Agent；会话列表只有本人的 `viewer-chat`；新建 403 `permission_denied`；看他人会话 404；下载本人产物 200 |
| developer | 登录；列表不含 peer 会话；Agent 列表无 Admin Agent / 其他域 Agent；未知域 403 `domain_forbidden`；新建会话 201 且写入当前域；改名/归档 200；发消息 202 → 真实轮次 succeeded → SSE 回放 → 产物下载 200；对他人会话写 404；同会话第二次发送 409；停止 200 → cancelled |
| platform_admin | 登录；`/me` `platform_admin=true`；Admin Agent 可见；默认列表仍是本人（`scope=own`）；`scope=all` 列出他人会话、下载他人产物 200；对他人会话 PATCH / 停止 404（无写旁路）；切换 `x-domain-id` 后 Agent 目录只剩目标域的安装（Admin Agent 仍对平台管理员可见） |
| service 账号 | 可登录，但 `/api/chat/targets` 403 `service_account_forbidden` |
| NULL owner | 把存量会话 owner 置 NULL 后：普通用户 404，平台管理员 `scope=all` 也 404（决策 A） |
| 审计 | `chat.create` / `chat.send` / `chat.stop` / `chat.download` / `chat.rename` / `chat.archive` / `chat.scope_all` / `chat.migrate` 全部落库；`scope=all` 每次访问 1 条；`detail` 不含消息正文；`chat.send` 带 `turn_id` trace |

## 5. 未覆盖与已知限制

- 仅一台 arm64 Mac（macOS 26.6.2）。Intel Mac、macOS 13、Python 3.11、Node 22、Windows 及其他架构未实测。
- `browser-ui` 会拉起 Chromium：需要可写用户目录 / 正常桌面会话；受限沙箱中会以环境错误失败，不是产品断言失败（本次在本机正常会话执行）。
- 走查使用确定性 fake 执行器，不是真实模型；未覆盖真实付费模型、OSS 浏览器 SSO、真实企业内网。
- 未覆盖多 worker 下的 SSE 配额（当前设计为单进程计数）、跨主机反向代理 + HTTPS 部署、网络中断/断电、64 MiB 下载边界、全局 32 轮 409 的浏览器交互（单元/真实 E2E 已覆盖服务端语义）。
- chat 页面面之外（dashboard / workbench / services / knowledge / cron、MCP/服务/知识 grant）的按用户/域隔离不在本期范围。
- 走查的角色流程走真实 HTTP；浏览器按钮级显隐由 `full` 的 31 条 Playwright 覆盖，两者不是同一份证据。

## 复跑入口

```sh
python3 scripts/ci.py bootstrap     # 首次，需要网络
python3 scripts/ci.py full          # 全量门禁（含 browser-ui / real-e2e / mock-restart）
python3 scripts/ci.py contracts     # 必过集覆盖断言
.venv/bin/python scripts/c7_migration_drill.py --data <复制好的数据目录> --work <临时目录>
.venv/bin/python scripts/c7_walkthrough.py
```

`full` 与 `contracts` 的报告、日志、Playwright JSON/HTML 都在 `.ci-reports/<时间-随机标识>/`；该目录不入库，附件形式随交付评论保存。
