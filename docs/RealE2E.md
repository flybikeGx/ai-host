# 真实进程 E2E 基线（AIHO-22）

AIHO-23 更新：以下为原任务历史记录。当前初始化、Chromium 下载、命令及报告位置以 [LocalCI.md](LocalCI.md) 为准；不再使用系统 Chrome。当前分支已补入同一 C1/C2 基线。

本任务只增加测试，不实现 C3–C7。已确认 AIHO-16 run
`01a1f2f9-a7eb-7000-b84d-2409d01bcd18` completed，暂停评论确认没有功能改动。
当前分支起点 `2e799ad` 与 C1 前置 `2b70c98` 的内容相同；将已验收的
`d14fca6`（C1）和 `10bfb2a`（C2）分别 cherry-pick 为 `e4ff8b4`、`c13fee1`，
保留当前分支和原起点，未包含 C3 部分实现。

## 从干净环境运行

需要 Python >=3.11、uv、Node（本次 v26.4.0 / npm 12.0.2）、npm，以及 Playwright 浏览器。
本次使用 macOS 已安装 Google Chrome；其他环境先在 frontend 执行
`npx playwright install chromium`。Python/npm 分别用 uv.lock/package-lock.json 固定；
本次 Python 3.13.14、Google Chrome 158.0.8127.37。系统 Chrome 不锁版本，跨环境复核应记录浏览器版本。无需产品配置、真实账号或模型凭据。

```sh
bash scripts/e2e.sh
# 后续契约独立运行，当前应退出 1；不把此命令当作现状基线的绿灯
uv run python scripts/test_real_e2e.py --contracts
# 查看报告（先解压证据包，或直接在本地运行后打开）
cd frontend
npx playwright show-report playwright-report/real
```

每次运行使用新 mkdtemp、SQLite、配置和 OS 分配的 loopback 端口；
不复用运行中的服务。先启动真实 uvicorn，再加载 npm 构建的 React 前端。
不使用 page.route、假 API、TestClient。后端关闭周期性 scheduler/dashboard 工作线程，
chat.send 本身仍通过实际 supervisor 子进程调度，写真实 turn/event/产物。
唯一模型替身为现有 tests/fixtures/fake_dsh.py，不访问模型网络，管理 MCP/知识服务
不被调用来执行真实管理任务。测试账号初始化及过期夹具直接操作独立数据库；没有公开 fixture HTTP 后门。

runner 不继承产品环境变量或凭据；CLI 只收到测试配置。所有新建用户均为测试假数据。
临时数据结束后删除；测试取消自己创建的活动 turn，等待终态，再停止该配置下的知识服务与后端。
没有按进程名杀进程。HTML/trace/截图及进程日志保留在 frontend 的 ignored 输出目录。
端口选择在 bind 和子进程接管之间有很小的竞争窗口；冲突会使就绪检查失败，不会复用外部服务。

## 本次实际结果（2026-11-19）

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| bash scripts/e2e.sh | 0 | 6 browser/HTTP 基线通过；迁移进程验证通过；MU-14 characterization 通过 |
| uv run python scripts/test_real_e2e.py --contracts | 1 | 22 项中 21 失败、1 通过，无 skip/xfail |
| uv run pytest tests/test_web_auth.py tests/test_chat_migration.py -q | 0 | 21 passed，1 项既有 httpx/Starlette 弃用警告 |
| git diff --check | 0 | 无空白错误 |

首次迁移失败夹具使用重命名表，因 Store 会自动重建而无效，已纠正。
最终使用测试 SQLite BEFORE UPDATE trigger 主动令 migration 存储写入失败；
真实进程非零退出且 HTTP 不可用，日志包含 E2E migration storage failure。
此为故障注入，非产品迁移失败。前端构建只有 bundle size 提示，未阻断运行。

## 覆盖映射与边界

| C0 矩阵 / 任务 | 可执行覆盖 | 本次结果 / 限制 |
| --- | --- | --- |
| 登录 AIHO-12 / C1 AIHO-14 | 浏览器登录、HttpOnly/SameSite cookie、刷新恢复、me 域元数据、退出旧 cookie 重放401、DB过期401、显式坏Bearer401 | 必须通过基线，4 passed；不回避失败 |
| C2 AIHO-15 | 旧形状真实库→真实启动→备份旧列与数据→owner/domain回填→真实第二次启动→数据/备份不变、迁移audit仍1条→故障启动退出/不可用 | runner 必须通过；旧库含3个受控用户会话，启动后显式分配测试归属 |
| 当前 chat 行为 | HTTP 创建/发送→真实执行器→SSE事件→下载内容、rename、stop、archive、同会话409；浏览器发送及刷新历史 | 2 passed；HTTP辅助路径明确没有声称均为按钮操作 |
| C3 AIHO-16 域/Agent可见性 | viewer catalog200、developer catalog隐藏admin、developer列表200、同域peer对象404、跨域对象/sse/files/download404、admin默认不可见他人 | 当前失败；非管理员仍被全局工作台403提前挡住，尚未走到对象过滤断言，不能宣称已证明资源隔离 |
| C3 管理员只读范围 | scope=all 下载真实文件字节 | 1 passed，但当前路由忽略scope；单独通过不能证明访问控制正确 |
| C4 AIHO-17 写授权 | 跨域 send/stop/rename/archive404；scope=all admin写403；service403；缺失对象404 | 失败，当前为提前403、admin写200、service200、缺失对象400等 |
| C4 并发 | 9个实际发送，第9个429；同时保持5条真实HTTP SSE，第5条429 | 失败：当前第9轮202、第5条流200；finally停止turn/abort连接 |
| C5 AIHO-18 | scope_all审计；实际send→stop后按turn_id查询audit包含两条动作且无正文 | 失败，缺失chat审计/trace；不是模型自报验证 |
| C6 AIHO-19 | developer真实登录后打开/chat并看到输入框 | 失败，当前进入账号首页 |
| C7 AIHO-20 | 本文映射、报告与基线复跑入口 | 后续逐阶段把对应契约用例加入必须通过门禁；不能以此次基线通过视作C7完成 |
| MU-14 | 两个实际mock进程A/B：A签发→A200、B401；A退出→新进程401 | characterization通过，明确记录进程内token状态；未改变持久化或多进程生产策略 |

尚未覆盖的组合（不能按本次结果宣称通过）：真实付费/外部模型、OSS生产SSO、
两个安装Agent跨域筛选、用户禁用/角色撤销的浏览器交互、完整错误优先级笛卡尔积、
全局32轮409、跨进程SSE额度、网络掉线重连/断电、审计写失败降级、64MiB下载边界。
它们属于后续 C3–C7 扩展验收；既有单元测试不替代上述未覆盖的浏览器链路。

contract 文件内每项都是普通断言，标题对应后续任务，失败trace可重放。
没有 expected-fail 将结果染绿：基线和契约分别退出，恢复开发须由本任务验收确认，
之后每阶段先复现其契约失败，再实现并把对应测试纳入必须通过集合。
C3–C7 状态保持暂停，本任务没有push、PR、合并、部署或付费调用。

## 证据包

附件包含 baseline/index.html、contracts/index.html（及相对引用的trace数据）、
两份Playwright JSON、故障截图、进程日志与summary.json。
trace里只有隔离环境生成的测试会话，不含启动环境凭据。API-only用例没有截图，
浏览器失败用例有截图。报告本身提供逐条断言、实际值和代码位置。

## AIHO-16 C3 读路径门禁（2026-11-20）

C3 已实现的读路径契约从 `contracts` 迁入必须通过集合：`scripts/test_real_e2e.py`
默认同时运行 `auth` 与 `chat_authz`，`--contracts` 只保留 C4–C6 未实现契约。
`tests/e2e/chat_authz.spec.js` 在真实进程上覆盖：A/B 归属隔离（list/snapshot/events/
files/download 全 404 且文案一致）、两个安装 Agent 的跨域筛选、Admin Agent 仅平台管理员可见、
NULL owner 对普通用户与默认管理员读 404、`file.read` 缺失时 snapshot 的 `files` 收敛、
非管理员 `scope=all` 403、管理员 `scope=all` 只读并落 `chat.scope_all` 审计、
service 账号读接口 403，以及打开中的 SSE 在账号停用后 30 秒内收到
`event: error` + `{"code":"unauthorized"}` 并关闭。

阶段限制（C4 落地后移除）：C3 按任务边界不修改 create，因此新建会话仍是
`owner_user_id IS NULL`。`auth.spec.js` 在创建后调用 `backfillOwners()`，按 C2 迁移的
回填规则（bootstrap admin + default 域）补齐归属，再验证发送/SSE/历史；这不代表
C4 的创建授权已实现，contracts 中的跨域写、管理员写旁路与每用户配额仍为失败。
