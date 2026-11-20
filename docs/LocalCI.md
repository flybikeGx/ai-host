# Mac 本地 CI

统一入口要求 macOS 13+、Python 3.11+、Git、uv、Node 22+ / npm。支持目标为 Apple Silicon 与 Intel；本次实测架构和限制见任务证据，未实测的组合不保证通过。不自动安装系统工具。请按各工具官方安装说明主动安装，检查 PATH，然后执行：

```sh
python3 scripts/ci.py bootstrap
python3 scripts/ci.py quick
python3 scripts/ci.py full
python3 scripts/ci.py contracts
```

bootstrap 使用 uv.lock / package-lock.json 冻结安装及项目 `.ci-browsers` Chromium；需要网络下载 Python/包/浏览器。依赖与浏览器就绪后的检查可离线运行，测试只用本机 fake CLI，无真实账号或付费模型。

- `quick`：全部 pytest + 前端构建。
- `full`：quick 之后依次运行 `browser-ui`（`frontend/tests` 的 31 条 Playwright，真实浏览器 + fixture 后端）、浏览器版本检查、`real-e2e`（`tests/e2e/*.spec.js`，真实 uvicorn + SQLite + Chromium，含 C3–C6 契约与迁移/重启/故障启动校验）、`mock-restart`（进程内 token 状态补测）。C1–C6 新增/扩展的测试都在默认套件里，不靠手工命令。
- `contracts`：**必过集覆盖断言**，不执行测试。C1–C6 的契约已全部迁入 `full`；该模式检查 `docs/CHAT_MULTIUSER_DESIGN.md` §14 的 C1–C7 每行仍有具名必过测试，并确认这些测试仍被 `full` 执行（`scripts/ci.py` 的 stage 名与 `scripts/test_real_e2e.py` 的匹配式）。空套件、测试改名或移除某个套件都会失败，不再有「0 测试通过」的空转通过。结果写入报告目录的 `contracts.json`。

`ci.py full` 的 `browser-ui` 会启动 Chromium；在受限沙箱/无图形环境里首次拉起浏览器可能因用户目录权限失败，这是环境问题（`environment_error` 或阶段失败），不是产品断言失败，按下面「排障」在新 clone 或正常桌面会话重跑。

每次检查生成唯一 `.ci-reports/<时间-随机标识>/report.json`：commit、dirty、系统/架构、工具版本、阶段命令与退出码。各阶段日志、Playwright JSON、HTML、失败 trace/截图保存在同目录。退出码：0通过、1检查失败、2环境缺失、124超时、130取消。用 `--timeout 60` 控制整个检查的阶段预算。日志只继承白名单环境，不继承 API key/token；仅包含合成账号，分享前仍应检查机器路径及测试输出。报告目录不会自动删除；成员确认不再需要后自行删除旧目录。每阶段监督器用psutil记录子进程及创建时间，跟踪独立session中的后代；正常结束、取消、超时均清理已记录后代，再清理本阶段独立进程组，不按进程名称清理；端口动态分配、数据在临时目录内，不访问业务库。不同checkout的依赖、浏览器及报告分离；同checkout并行构建不支持，应使用独立checkout。

## 主动安装本机门禁

```sh
python3 scripts/ci_hooks.py install
# 正常 git commit / git push 将运行 full；失败阻止操作
python3 scripts/ci_hooks.py uninstall
```

脚本使用 worktree 范围 core.hooksPath，不写全局配置，不替换已有hook；开启仓库的 extensions.worktreeConfig 以隔离兄弟worktree。卸载只移除本checkout配置；如果有继承的hook则恢复继承行为。pre-commit 与 pre-push 都运行完整检查，quick 用于日常反馈。安装前应先bootstrap。hook检查当前工作树，不保证部分暂存提交内容与测试内容相同，提交前应完整暂存相关改动。无专用服务器、cron或常驻服务。`--no-verify`、改配置均可绕过；离线/睡眠不会自动调度。这不是服务端强制合并门禁。

## 排障及其他成员复核

在新目录 `git clone <本项目仓库地址>`，进入目录执行bootstrap和full，保存生成报告。Intel或另一台Mac需独立重复此步骤并记录系统和架构。下载失败先核实网络/代理与工具版本，重新bootstrap；浏览器缺失重新bootstrap，不回退到系统Chrome。测试失败阅读对应阶段日志和HTML，保留报告，不用skip或放宽断言。超时/取消查看report状态；重复full应产生新报告且不依赖上次数据库。

隔离副本中可引入 `tests/test_ci_failure_probe.py`，内容 `def test_probe(): assert False`，运行full应返回1；删除后重跑。故障演示可在隔离副本的 pytest 配置中临时加 `addopts = "-x"` 并将探针命名为 `test_000_ci_probe.py`，加快故意失败反馈，随后恢复配置。缺依赖可在隔离副本重命名`.venv`，应返回2；超时用`full --timeout 0.01`应返回124；运行中Ctrl-C返回130。并行验证在两个独立clone中同时运行full。演示hook失败时，仅在临时副本安装hook并保留故意失败测试，`git commit`应被阻止，卸载后确认配置移除。不得将实验hook安装到成员既有工作区。
