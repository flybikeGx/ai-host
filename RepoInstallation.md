# Repo 安装、固定 skills 与常驻 MCP

`schema_version: 2` 将资源从 Run 提升到 repo 安装版本。skills、依赖环境和 MCP 程序安装一次；MCP 在安装成功时就已启动，没有任务时也继续运行。Run 只连接自己的命名空间，结束或超时不停止共享服务。当前采用资源发现与提示词约束，允许感知其他目录，不引入容器强隔离。

## 三个可以直接运行的 repo

| 示例 | skill 名称 | MCP 工具名 | 对 [2, 3, 5] 的结果 |
| --- | --- | --- | --- |
| finance | analysis | domain__analyze | 求和：10 |
| inventory | analysis | domain__analyze | 求积：30 |
| research | analysis | domain__analyze | 求平均：约 3.3333 |

每个 [示例目录](examples/repo-resources) 都包含独立 AGENTS.md、Installation.md、skill、MCP 程序、初始化脚本、报告工具和验证脚本。相同的资源名称用于验证命名空间不会串用。

```sh
.venv/bin/python scripts/create_resource_demos.py
.venv/bin/agent-base repo install .demo/resources/finance
.venv/bin/agent-base repo install .demo/resources/inventory
.venv/bin/agent-base repo install .demo/resources/research
.venv/bin/agent-base repo list
```

创建脚本生成三个独立本地 Git repo，已有目录保留。安装命令会输出 installation_id、namespace、固定资源位置和服务 PID。演示使用基座解释器中的 MCP SDK；实际 repo 可声明 uv.lock 安装自己的依赖。

```sh
.venv/bin/agent-base run .demo/resources/finance --wait
.venv/bin/agent-base run .demo/resources/finance --wait
```

两次运行绑定同一安装版本、skill 文件和常驻 MCP。v2 的首次 `run/install` 也能自动安装资源；建议先显式 `repo install`，便于提前检查并复用资源。已有可用安装时，正常 run 采用已安装版本，不自动跟随远端更新；更新源码后再次 `repo install` 才准备新版本。

## Installation 配置

机器配置允许二选一：

- 根 `agent.yaml`。
- 根 `Installation.md` 或 `installation.md` 中唯一的 `agent-base-install` 代码块。

两处不能同时放机器配置，Markdown 正文用于说明资源和依赖，不作为自由 shell 安装脚本。推荐参照 [finance/Installation.md](examples/repo-resources/finance/Installation.md)。

````markdown
# 示例初始化

提供 analysis skill、domain MCP；安装完成后 MCP 保持运行。

```agent-base-install
schema_version: 2
package:
  name: example-agent
skills:
  - name: analysis
    path: skills/analysis
mcp:
  servers:
    - id: domain
      transport: stdio
      argv: ["${runtime.python}", "${package.root}/mcp/server.py"]
      timeout_seconds: 30
```
````

延续现有 task、environment、installation.steps、tools.local、completion 和 limits 格式。`installation.steps` 在固定安装目录执行一次；脚本使用安装目录作为 cwd。初始化前、后都检查提示词路径。安装失败不会成为可复用的 ready 版本。

只加载声明的 skills 和 MCP。安装依赖、命令与接入层内部可以使用基座解析出的实际路径；仓库的 AGENTS.md 和 skill 文档使用相对路径。

## 生命周期与资源位置

```text
.agent-base/repos/<repo_id>/installations/<installation_id>/
  source/               固定 commit 的代码、AGENTS、MCP 与工具
  skills/               固定技能目录，链接到声明的 skill
  env/                  可选的 uv.lock 环境
  receipt.json          安装清单、commit 和 namespace
  python.txt            运行解释器
  service.json          当前服务端点
  service.log           MCP 连接与服务日志
  token                 本机接入凭据，不进入 prompt/patch 明文
```

repo_id 来自规范化本地路径或原始远端 URL 的摘要；不同访问 URL 暂不自动归并为同一个身份。installation_id 由 repo、commit 和解析后的清单决定。重复安装同一版本复用已有资源；两个安装请求通过文件锁串行化，不重复初始化和启动 MCP。

每个 Run 在自己的工作区创建：

```text
.agent/resources.json    本 repo namespace 和资源目录
.agent/skills/           指向固定安装技能目录的链接
artifacts/              本次产物
```

Run 不重新安装、复制或改写共享 skills。其 package 也引用固定安装源；检查脚本使用同一安装版本。不同 commit 安装版本可共存，各有服务，旧运行不会在执行中切换版本。暂不自动回收旧安装，避免误停仍被使用的 MCP。

## MCP 为什么可以一直运行

每个安装版本拥有一个独立 Python MCP 网关进程，对外提供本机 Streamable HTTP。网关启动时连接清单内的 MCP，持有连接直到服务停止：

```text
Run A 的 dsh ──HTTP──┐
                    ├─ repo 专属常驻 MCP 网关 ── 持续连接 ── 声明的 stdio/HTTP MCP
Run B 的 dsh ──HTTP──┘
```

stdio MCP 由网关启动一次，多个 Run 通过网关使用它；不会让多个 CLI 直接竞争同一对 stdin/stdout。同一上游会话的调用串行执行，不修改共享 cwd/env。适用于 repo 级共享状态或无状态 MCP；需要为不同实例维护独立上游会话的服务，当前版本不适用。

外部 HTTP MCP 共享网关持有的连接，远端服务进程由外部系统管理；本机网关常驻不代表可以管理第三方服务的部署。

网关聚合以下工具：

- `read_skill`：只读取本安装声明的技能，参数枚举为本 repo 的 skill 名称。
- `<mcp_id>__<tool_name>`：上游 MCP 暴露的工具，例如 domain__analyze。
- `tools.local`：按本次 Run 的工作区运行 JSON stdio 业务工具，输出写入本次目录。

启动时完成 initialize 和完整 tools/list，失败则安装失败。当前网关转发 MCP tools，不代理 resources/prompts/sampling 等其他能力；清单中的 MCP 都作为必需依赖处理。

每个实例只挂载一个 `repo_<repo_id>` MCP 命名空间。CLI 配置中不包含其他 repo 或 admin MCP；DSH_HOME/DSH_AGENTS_HOME 按 Run 分开，避免继承用户全局资源。提示词包含本命名空间和相对技能目录，并明确说明其他 repo 的资源不能帮助当前任务。

工作区含专属根标记，CLI 的 AGENTS 发现停留在该工作区，避免继续向父目录加载基座管理员的角色说明。

## 故障、超时与恢复

MCP 网关独立于任务监督进程，不属于 CLI 的进程组。Run 超时或取消会结束本次 CLI；本地业务工具检查该 Run 的取消状态和 deadline。共享上游调用的时限取工具超时和 Run 剩余时限中的较小值。

单次业务调用失败或超时不会直接重启共享服务。工具错误返回真实失败，超时标记 `timeout_outcome_unknown`，不自动重放业务操作。远端取消尽力执行，不能据此证明副作用已经撤销。

网关每五秒对空闲上游连接执行 ping；繁忙连接不探测，以免把正常长任务误判为故障。`serve/worker` 每十五秒检查服务状态，对异常安装退避恢复，连续失败三次后停止自动尝试。手动 start 可重置恢复计数。没有常驻调度器时，服务仍运行，但故障后需下一次使用或手动 start 才会恢复。

```sh
agent-base repo status <installation-id>
agent-base repo stop <installation-id>
agent-base repo start <installation-id>
agent-base repo restart <installation-id>
```

显式 stop 会影响共享该安装的所有实例，并停用自动恢复。重启服务只恢复连接，不重放中断的工具调用。升级基座代码后，已有服务仍使用启动时的代码，必要时显式 restart。

## Web 与 admin

`agent-base serve` 页面增加 Repo 资源列表，显示命名空间、skill 名称和 MCP 进程状态。已注册的工作仍从“立即运行”触发。

API：

- `GET /api/repos`、`GET /api/repos/{installation_id}`：资源清单和健康状态。
- `POST /api/repos/install`：body 为 `{"origin":"...","ref":"..."}`，ref 可省略。
- `POST /api/repos/{installation_id}/start` 或 `stop`：管理服务。

admin MCP 对应 install_repo_resources、list_repo_resources、get_repo_resources、start_repo_resources、stop_repo_resources。普通 repo 实例不挂载这些管理工具。

## 绝对路径检查

```sh
.venv/bin/agent-base lint .demo/resources/finance .demo/resources/inventory .demo/resources/research
.venv/bin/python scripts/check_prompt_paths.py examples/repo-resources
```

扫描所有 AGENTS.md、SKILL.md、skills 目录及发现的 skill 子目录中的 Markdown；报告文件、行号、列号及命中的路径。检测常见 POSIX、Windows 盘符、UNC、file URI、HOME/用户目录写法，忽略 HTTP URL、相对路径和 prompt XML 标签。退出码 1 表示检查失败。

v2 安装前后自动执行同一检查，生成的任务 prompt 也检查且不注入安装目录等绝对文件路径。静态检查针对文档中的路径字面量，不分析动态生成路径的任意程序。真实 CLI 自身生成的环境描述不在这个检查器的控制范围内。

## 验收

```sh
.venv/bin/pytest -q
.venv/bin/python scripts/validate_resources.py
```

第二条使用实际模型凭据，会运行三个 repo、四个 dsh 实例，核对工具调用事件、计算结果、namespace、报告、共享 MCP 的 PID/boot_id/counter，以及技能文件 hash/inode 不变。验收后服务保持运行，便于继续体验。详情见 [VALIDATION.md](VALIDATION.md)。

实现使用 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 的客户端、服务端和 HTTP 会话管理器；传输语义参考 [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。旧 schema_version:1 继续采用原有每 Run 安装和挂载逻辑；需要长期资源时使用 v2。
