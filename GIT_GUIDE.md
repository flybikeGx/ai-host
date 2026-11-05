# Git、Gitee 与内网交接

`agent-base/` 是本项目的独立 Git 根目录，初始分支为 `main`。同级原 AI Hub、推理 MVP、旧设计目录不属于本仓库。项目主仓库为 [Gitee：flybike/ai-host](https://gitee.com/flybike/ai-host)，Agent Base 的内容直接放在仓库根目录。开发机将原 GitHub 地址保留为 `github` remote，`origin` 指向 Gitee。

## 1. 提交范围

| 纳入版本管理 | 留在机器上，不提交 |
| --- | --- |
| Python/React 源码、测试、演示仓库模板 | `.venv`、node_modules、dsh 本地安装 |
| `uv.lock`、两个 npm package-lock | `.agent-base` 及其他本机状态目录 |
| 构建好的 `src/agent_base/static/chat` | 数据库、对话、session、日志、业务产物 |
| 设计书、迁移说明、筛选后的两张截图 | 本机 `agent-base.toml`、额外本机配置、`.env` |
| `config/*.example.toml` | 个人 CLI 登录目录、真实 API key/token |

`.gitignore` 已覆盖默认生成目录、配置、环境文件和常见测试缓存；`.gitattributes` 统一文本为 LF，图片作为二进制。自定义 storage.root 时，若不使用已忽略的 `.agent-base-*` 目录，要把该路径加入本机忽略规则。

静态前端有意入库，使内网直接运行 Python 服务即可显示网页。修改前端后先 `npm run build`，再同时提交源码与构建文件。依赖锁文件有意入库，安装时使用 frozen/ci。

## 2. 推送到 Gitee

本机 origin 使用 `git@gitee.com:flybike/ai-host.git`。已经配置该地址时，在项目目录运行：

```sh
git status
git remote -v
git push -u origin main
```

如果是没有 origin 的新副本，先执行 `git remote add origin git@gitee.com:flybike/ai-host.git`。推送其他目标时，用 `git remote set-url origin YOUR_GIT_REPO_URL` 更换地址。使用自己的 Git 认证，不在 URL 中嵌入 token。

若远端已有独立历史，先 fetch 和比较，再按团队方式合并；不默认 force push。本项目尚未选择开源许可证，在对外开源发布前由维护者确定许可。

## 3. 无外部 Git 服务访问时使用 bundle

本次交接在本机生成 `.agent-base/exports/agent-base-baseline.bundle`，它携带初版 Git 历史，不包含被忽略的运行数据。另附 SHA-256 文件用于核对传输完整性。

以后重新生成最新交接包：

```sh
mkdir -p .agent-base/exports
git bundle create .agent-base/exports/agent-base-baseline.bundle --all
git bundle verify .agent-base/exports/agent-base-baseline.bundle
```

将 bundle 放到内网后执行：

```sh
git clone agent-base-baseline.bundle agent-base
cd agent-base
git status
git log -1 --oneline
```

由 bundle 克隆的 origin 指向本地 bundle 文件。如要推送到内网 Git 服务，应改成内网仓库：

```sh
git remote set-url origin YOUR_INTERNAL_GIT_REPO_URL
git push -u origin main
```

bundle/源码只解决代码交接，不包含 CodeAgent 安装包、企业登录和依赖缓存。Python、CLI、企业网络与业务 repo 的准备见 [CodeAgent 迁移指南](CODEAGENT_MIGRATION.md)。

## 4. 内网分支与后续同步

建议保留 main 作为共同基线，在内网开始适配时建立分支：

```sh
git switch -c feat/codeagent-runtime
```

先提交统一适配边界，再逐项提交 CodeAgent 能力与验收，便于和外网持续维护的 dsh 基线比较。企业专用 host、认证配置和私有业务数据继续放在本机配置或私有制品系统中。

跨网络同步只传经审阅的 Git 提交或 bundle，不直接同步 `.agent-base`。本地 repo origin 的位置变化会改变其 repo_id，业务仓库应在内网按正式 origin 重新安装。原生会话不跨引擎透明迁移，不能把旧数据库拷贝到新目录就当成恢复成功。

## 5. 日常检查

```sh
git status --short
git diff --check
.venv/bin/python scripts/check_prompt_paths.py .
git diff --cached --stat
```

提交前查看暂存差异，确认没有本机路径、真实凭据和业务会话。新增初始化或 CLI 适配逻辑后执行相关 Python 测试；修改网页交互后执行浏览器测试。纯文档改动检查图、链接与命令即可，不需要重复发起真实模型任务。
