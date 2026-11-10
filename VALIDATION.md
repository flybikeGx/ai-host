# 初版验证记录

## 2026-11-10：对话入口与图形化定时任务

- 完整 Python 回归 82 项通过，新增 14 项覆盖安装版本绑定、一次性触发去重、停机补触发、暂停/恢复、时区与夏令时边界、API 认证和实际 entrypoint 报告。
- Playwright Chrome 8 项通过：原有聊天交互、Agent 列表唯一创建入口、独立历史筛选、跨页面保留 Token、图形化计划、暂停/恢复、运行记录/报告、手机布局与无横向溢出。使用独立测试数据、fake CLI 和真实本机 MCP。
- 真实验收由 Chrome 页面创建一次性计划 `schedule-d86b92df56b04ae59f0ade3b94dd1715`，指定 **2026-11-10 15:34 Asia/Shanghai**。实际时钟到点后，常驻调度器创建 Run `33059347f56641adb508eb9b185d1c5e`，真实 dsh 按 entrypoint 调用演示 MCP 得到数值 10；最终为 succeeded / checks_passed，Agent 自报 completed。
- 真实计划只有一个 Run，触发后 enabled=false、next_due=null，MCP boot_id 未变。没有保留周期任务。演示 `scheduled-analysis-agent` 仍可从 Agent 列表选择，旧三个 Repo 的安装与历史未修改。
- 网页、`/cron` 和新 API 已在本机 serve 中加载。截图保存在 docs/assets；原始验收收据位于 `.agent-base/validations/graphical-schedule-1794295954561/result.json`，不提交业务运行数据。

界面与 API 说明见 [SchedulingUI.md](SchedulingUI.md)。

## 2026-11-10：Repo 引入契约增强

- Python 回归：68 项通过，其中新增 entrypoint/报告相关 20 项。包含入口单独启动、cron、准备后启动、冻结源、v2 常驻 MCP 复用、普通网页对话不注入入口、错误报告/blocked、额外产物归档、超时/取消及报告 HTTP 接口。
- Playwright Chrome：4 项全部通过，使用独立服务和 fake CLI；确认普通网页对话保持原有交互和资源行为。
- 文档链接、代码块、Python 编译、全项目 AGENTS/entrypoint/skill 路径检查通过。
- 真实 dsh：run_id `5cb3510b1aa949a2a26e02d996ae5c65`，使用隔离状态目录和新建 hello 示例 Git repo；未传 task，实际 prompt 顺序为 AGENTS → entrypoint → 基座报告契约，没有附加旧 task.default。原生记录包含 5 次工具调用，生成真实业务报告，冻结检查脚本通过；Run 为 succeeded / checks_passed，Agent 最终 JSON 状态为 completed，归档 1 个真实产物。

真实执行收据：`.agent-base/validations/entrypoint-1794277460092584000/result.json`。基座 report.json 位于该隔离目录内的 `state/runs/5cb3510b1aa949a2a26e02d996ae5c65/`。运行记录不提交到 Git。

后续收尾已启动新版 serve，网页与报告接口均返回 HTTP 200，列表包含 Admin 和三个 Repo Agent，三个常驻资源服务均存活。启动前无活动 Run 或消息，旧验收 cron 保持暂停；没有新增周期任务或修改/验证 CodeAgent 内网运行。新契约格式与兼容规则见 [RepoContract.md](RepoContract.md)。

---

记录日期：2026-11-05。平台：macOS，Python 3.13.14，Node.js v26.4.0，dsh npm 0.2.1-rc.5。

## 自动验证

执行 `.venv/bin/python -m pytest -q`，48 个用例通过。范围包括：

- 实际 Git clone、固定 commit、AGENTS 与中文多行任务传递、model/effort patch。
- 先安装再启动，拒绝重复 start。
- 两个任务并发、第三个排队、不同工作区隔离。
- 排队和运行中取消、超时、运行器失败、缺少产物。
- 中断状态归结，迟到的中断检查不能覆盖成功状态。
- 管理对话复用原生 session ID。
- 官方 MCP Python 客户端与真实管理 MCP server 交互，安装并启动 fake-runtime 任务。
- 真实业务 MCP server 的中文输入、执行和 schema 错误回执。
- Web 触发、原子幂等键、工作启停、跨来源写请求拒绝、可选 Bearer 认证。
- 同机调度器竞争、队列容量、满队列仍能出队、cron 停机合并和重叠跳过。
- 未派发队列重启保留、实际后台调度线程、成功临时目录清理、输入副本隔离。
- 运行中 session 可见、超时 session 保留、强制杀死监督进程后的 CLI 清理和 interrupted。
- Installation.md 配置块解析、重复配置拒绝、安装复用及并发安装只初始化一次。
- 三 repo 同名 skill/MCP 的命名空间、固定技能文件及常驻 MCP 跨实例复用。
- 任务超时不停止共享服务、工具调用超时不重启服务、服务启停及三次失败恢复限制。
- v2 首次运行自动安装、prepared 后启动、工作触发时绑定已有安装与 Web 资源清单。
- POSIX/Windows/UNC/HOME 路径检查、skill 引用文档检查、相对路径及中文斜杠分隔词的误报回归。
- 网页对话原生 session 续接、独立工作区、并发消息去重、活动轮次互斥、取消/超时/异常状态。
- 会话历史、SSE 游标重放、归档与认证、工具错误状态、屏蔽原生 thinking、产物下载与路径范围。

上述自动测试的模型执行器是 deterministic fake CLI，不是外网模型。

## 真实模型链路

使用启动环境已存在的 DeepSeek 凭据，未写入仓库、patch 或验证记录。

1. `.venv/bin/agent-base admin --session smoke --message '只回复 ADMIN_AGENT_READY，不调用任何工具。'`：实际收到 `ADMIN_AGENT_READY`。
2. 创建本地示例 Git 仓库后，在 admin 对话中要求安装、运行默认任务并等待结果。
3. admin 原生事件记录有 3 次 tool_call/tool_result；后台任务实际创建独立 clone。
4. 仓库 agent 的原生记录有 2 次 tool_call/tool_result，完成业务 MCP 写报告及读取检查。
5. 基座运行冻结的 `verify_report.py`，输出 `report verified`，记录为 succeeded / checks_passed。

本次真实验收 run_id：`b0cb29ddb2d948f6bf5e3367ba9fa7ec`。

本机结果记录：`.agent-base/runs/b0cb29ddb2d948f6bf5e3367ba9fa7ec/result.json`。

封存产物：`.agent-base/runs/b0cb29ddb2d948f6bf5e3367ba9fa7ec/artifacts/artifacts/report.txt`。

文件实际存在，包含 `AGENT_BASE_DEMO_OK`，大小 83 字节，SHA-256 为 `ef31437f98a406d0d9a5290c249dd3e0096ca7d3ce29b20cf0bb29f367ddffed`。

独立检查了文件 hash 与 result.json 一致，没有仅凭模型自述判定通过。本机运行记录位于忽略目录；复制源码后需重新创建自己的示例任务。

## 真实 Web / cron 调度验收

使用 `scripts/create_scheduled_demo.py` 创建独立 Git 仓库 `.demo/scheduled/repo` 和单独的输入工作区 `.demo/scheduled/source`。启动 `agent-base serve --port 8765` 后，执行 `scripts/validate_scheduler.py`，通过真实本机 HTTP 请求激活任务，并等待实际分钟时刻触发 cron；没有使用模拟时钟或 fake CLI。

| 来源 | run_id | 结果 |
| --- | --- | --- |
| Web | `4279c80f4bdf49a1b18154d2fdd85e70` | succeeded / checks_passed |
| cron | `c46e06935e504043adbba85062c70cf4` | succeeded / checks_passed |
| 5 秒时限 | `7d7b8ab011ce46c2b08442f33cc2e4ab` | timed_out，session 保留，CLI 已退出 |

Web 与 cron 均由 dsh 实际调用 summarize_metrics MCP 工具生成 `artifacts/report.json`，独立验证脚本重新读取输入核对每个服务与总计。两份报告都为 **230 次请求、4 次错误**，包含实际触发来源与 key。Web 报告 SHA-256 为 `870ada88c04421222c9edf24424b3e531b026dac7033c3dd3f7b87d943cf48a2`，cron 报告为 `80479090acadb959db32b340a0bf05438d4e72d7efd9543a0701403c73d121ab`。

验证了三个运行各有不同 session；重复 Web key 返回同一 Run；源数据 hash 和源 Git 状态未变；两个成功工作区已删除，归档仍存在；超时工作区保留，按原 PID 与出生时间确认 CLI 不再存活。临时验收 cron 已暂停，未修改系统 crontab。

本机完整收据：`.agent-base/validations/accept-d0ec9f9d/result.json`。产物在对应 `.agent-base/runs/<run_id>/artifacts/artifacts/report.json`。演示仓库、输入和本机收据均位于忽略目录，可通过脚本重建。

## Repo 长期安装 / skills / MCP 真实验收

执行 `scripts/create_resource_demos.py` 创建三个独立 Git repo，每个包含 AGENTS.md、Installation.md、analysis skill、domain MCP、初始化和产物验证脚本。三个 repo 使用相同资源名称但不同计算逻辑。

执行 `scripts/validate_resources.py`，通过真实 dsh 运行四个实例，全部 succeeded / checks_passed：

| 实例 | run_id | MCP 返回结果 | 上游 MCP PID |
| --- | --- | --- | --- |
| finance-1 | `22faf2b6201544199c4e1073e8a6895b` | 求和 10，调用计数 2 | 38545 |
| finance-2 | `7c63faff905b4d148fbeeab456671ce9` | 求和 10，调用计数 1 | 38545 |
| inventory | `8113e3f58de54b1f8a372b248c517909` | 求积 30 | 38865 |
| research | `cd7338b593d645598366e07b6c5b8d75` | 求平均 3.3333333333333335 | 38875 |

每次都在基座事件中核对实际 read_skill、domain__analyze、write_report 调用。finance 两个并发实例共享同一个 MCP PID 和 boot_id，调用计数不同；另外两个 repo 各有独立 MCP 进程和命名空间，未串用结果。

三个安装的 installation-count.txt 均为 1；任务前后共享 skill 的 hash 和 inode 未变；自定义 prompt 不含绝对路径，runtime patch 仅挂载所属 repo namespace。四实例结束后 MCP 网关 PID 仍为 38544、38864、38874；更新 Web 进程后再次核对也未重启这些服务。

本机收据：`.agent-base/validations/resources-1793848182866163000/result.json`；收尾状态：同目录 `final-status.json`。报告在对应 Run 的 artifacts 目录。全部文档路径检查命令 `python scripts/check_prompt_paths.py .` 返回 ok=true。

Web 已登记 finance-demo、inventory-demo、research-demo 三个工作，并展示安装资源清单。三个常驻 MCP 保留运行；没有新增周期 cron。

补充验证 CLI 根标记后，真实 dsh 冒烟 run `af0a5e788ca34d77bdd962634fd38c9a` 同样 succeeded / checks_passed，仍使用原 finance 上游 MCP PID 38545，调用计数增至 3。没有重装技能或重启 MCP。新增的根标记断言也通过三命名空间集成测试。

## 网页对话真实验收

通过 `scripts/validate_chat.py` 对本机 8765 服务执行真实 HTTP 请求，使用真实 dsh，七轮 CLI 调用均正常结束：

- Admin 两轮：第一轮记住随机标记，第二轮只根据原生会话回答；两轮均无工具调用、不读写文件。返回正确标记，native session ID 保持一致；重复 client_key 没有创建新轮次。
- Admin 管理轮：调用实际管理 MCP 查询所有已安装 Agent、skills 和 MCP 状态。接收到 session opening 后主动断开 SSE，随后仍正常完成，证明查看连接断开不取消任务。
- 财务、库存、研究各一轮：实际 read_skill、domain__analyze、write_report，得到正确 repo 的报告；跨命名空间未串用工具，产物可通过会话下载 API 读取。
- 财务第二轮追问：根据原会话回答结果为 10，复用相同 native session 和工作区。
- 三个既有 repo 服务的 boot_id 前后未变，skills 和安装没有被重新创建。

本机完整收据：`.agent-base/validations/chat-1793862715448360000/result.json`。

保留的网页会话完整 ID 见收据；财务会话为 `cb1ec963fae040f5bd45cdb89220b10e`，可在 `/chat#cb1ec963fae040f5bd45cdb89220b10e` 打开。独立记忆验证会话已归档。

另执行四项 Playwright 真实 Chrome 测试，全部通过：多轮历史和 Markdown 安全渲染；刷新恢复且不重复发送；repo 工具卡片、产物下载、切换会话不中断及停止；390px 手机布局和中文输入法回车行为。测试使用独立 8879 服务和 fake CLI，与上面的真实模型验收分开。

实际页面截图在同一收据目录的 `web-chat.png` 和 `web-chat-mobile.png`。只读检查确认页面列出四个 Agent，浏览器没有 JavaScript 运行错误；前端生产构建、Python 编译及全项目提示词路径检查均通过。

## 尚未验证

- Windows 真机、CodeAgent 内网、CodeAgent MCP/管理对话接口。
- 远程 MCP 的实际服务和 OAuth，以及所有第三方仓库的安装行为。
- 不可信仓库的操作系统隔离、多租户部署、跨机器恢复。

固定版本 dsh 的 final 事件不含 reason；系统明确记录 legacy_final_exit_code，不声称拿到了不存在的原生字段。原生 MCP 插件可能按可选插件处理启动失败，收据不能替代外部 MCP 上线验收。
