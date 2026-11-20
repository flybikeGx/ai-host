# AIHO-23 验证记录（2026-11-20）

代码版本：`60ecc24`，起点 `c9ddef4`；保留起点及所有后续提交，未变更分支或重排 C3–C7。基线 `59181b0` 可以读取，但不在原任务分支上；引入其测试及 `e4ff8b4` / `c13fee1` 的 C1/C2 前置改动。首次仅引入测试时真实进程报 SQLite 缺少 owner_user_id，补齐已验收前置后通过，没有放宽断言。

实测：一台 Mac，macOS 26.6.2 / arm64，Git 2.54.0，uv 0.11.26，Node 26.4.0 / npm 12.0.2；入口 Python 3.15.2，uv 环境 Python 3.13.14；Playwright 1.64.1 / Chromium 156.0.8078.4（准确版本以附件 version 日志为准）。Intel、第二台 Mac、macOS 13、Python 3.11 和 Node 22 尚未实测；文档所列为支持目标范围，不代表已在所有组合跑通。

## 命令与结果

- `python3 scripts/ci.py bootstrap`：0。工作树和全新 `git clone --no-hardlinks` 的隔离副本各自安装冻结依赖并下载项目 Chromium，没有复用原工作树的 venv/node_modules/浏览器目录。下载依赖需要网络。
- `python3 scripts/ci.py full`：0。最终代码工作树：299 passed、3 skipped、1 warning；构建、6项真实浏览器基线、迁移备份/重启/故障启动及 mock token 重启验证通过。
- 3项skip全部为已有测试对本地 `.runtime/dsh` 的可选依赖；未安装另一CLI，不调用付费模型。7项新增CI自检及6项真实浏览器基线无skip/xfail。1项Starlette/httpx弃用警告保留。
- 独立真实进程 `--contracts`：1，22项中21失败、1通过；与AIHO-22已知缺口一致，HTML、trace及截图保留，绝不计为全通过。
- 两个checkout同时运行full：各自数据、端口、报告独立，均通过。原工作树重复full多次，均产生新报告。
- 故意失败：在专用临时clone中添加 `test_000_ci_probe.py` 的失败断言，临时pytest `addopts="-x"` 提前停止。安装hook后实际 `git commit --allow-empty` 和向临时本地bare仓库的 `git push` 都返回1，HEAD及远端未推进。卸载后worktree hooksPath已移除；未安装到成员工作区，也未修改全局Git配置。
- 移开临时clone的venv，quick返回2，报告environment_error；恢复后 `full --timeout .01` 返回124。
- 实际full超时5秒：124，观察到22个进程，最终存活0。实际full在repo_server运行中取消：130，观察到18个进程，最终存活0。报告和各阶段processes.json保留。
- 真实E2E在第二次服务启动期间SIGTERM退出；未发现该测试后端残留。直接runner信号退出与外层CI的规范化130分别记录。

取消压力测试首先发现了启动期信号时序和独立session后代清理缺口，已修复并清理实验残留；新增可重现回归测试包含启动期取消及独立session子进程清理。最终验证使用阶段监督器跟踪PID及创建时间，清理自己的进程树，不按名称杀进程。测试SQLite均在临时目录，未使用业务数据库或真实账号。

## 复核

附件证据包按 `working/`、`clean/`、`fault/` 分组，包含报告、阶段日志、Playwright HTML/JSON/trace/截图及故障探针结果。首次干净副本的bootstrap报告为 `20261120-102509-f4529524`，干净基线full为 `20261120-102612-1c352b29`；最终代码干净副本同样299 passed、3 skipped，所有阶段退出0，commit=60ecc24、dirty=false；其full报告为 `20261120-103837-6ee78f83`。报告记录实际commit和dirty；旧的开发中报告不能冒充最终干净代码报告。

其他成员在新clone中按 `docs/LocalCI.md` 执行bootstrap、quick、full和独立contracts，分享生成的报告；主动安装/卸载hook后在临时副本验证失败拦截。安装前的空机器、离线未缓存依赖、第二架构、睡眠/断电及服务器强制合并门禁未被本次结果覆盖。hook可绕过，不是服务器门禁。
