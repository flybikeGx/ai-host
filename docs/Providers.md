# 模型 provider 配置

dsh 执行入口默认使用内置的 `deepseek-official` 路由。需要经过内部网关、代理或自托管服务时，在 `agent-base.toml` 中用 `[providers.<name>]` 声明路由，并把 `runtime.provider` 指向它。一份配置同时决定 CLI Admin、网页 Admin、普通任务、cron 和 repo 多轮对话实际使用的模型路由。

现有官方配置不需要任何改动：没有 `[providers.*]` 时行为与之前一致。`deepseek-official` 和 `deepseek-account` 是 dsh DeepSeek 适配器自带的路由，不能通过 `[providers.*]` 重新定义。

## 配置字段

`[providers.<name>]` 和 `[[providers.<name>.models]]` 只接受下列字段，出现未知字段即报错，避免拼写错误被静默忽略。

| 字段 | 必需 | 含义 |
| --- | --- | --- |
| `adapter` | 否 | 目前只支持 `pi-ai`，省略即该值 |
| `api` | 是 | 协议：`openai-completions`、`openai-responses` 或 `anthropic-messages` |
| `base_url` | 是 | HTTP(S) 端点；不得包含用户名密码、查询串或片段 |
| `api_key_env` | 否 | 凭据所在的环境变量名；省略表示该路由不需要凭据 |
| `display_name` | 否 | 选择器显示名 |
| `default_context_window` | 否 | 模型条目未声明容量时的回退值 |
| `default_max_tokens` | 否 | 模型条目未声明输出上限时的回退值 |
| `models` | 是 | 非空模型目录，见下表 |

模型条目：

| 字段 | 必需 | 含义 |
| --- | --- | --- |
| `id` | 是 | 请求实际使用的模型 ID，必须在路由内唯一 |
| `name` | 否 | 显示名 |
| `context_window` / `max_tokens` | 否 | 正整数容量与输出上限 |
| `input` | 否 | `["text"]` 或 `["text", "image"]` |
| `reasoning_efforts` | 否 | `false` 表示非推理模型；或 `{档位 = 网关拼写}`，如 `high = "high"` |

`runtime.model` 必须是所选路由模型目录中的 `id`；`runtime.effort` 必须在该模型声明的档位里，非推理模型不能配置 effort。任一条件不满足都在加载配置时失败，不会回退到官方 provider 或目录中的其他模型。

## 示例

`config/internal-gateway.example.toml` 是完整示例，复制到仓库根 `agent-base.toml` 后修改端点和模型目录：

```toml
[runtime]
provider = "internal-gateway"
model = "internal-flash"
effort = "high"

[providers.internal-gateway]
adapter = "pi-ai"
api = "openai-completions"
base_url = "https://llm.internal.example/v1"
api_key_env = "INTERNAL_GATEWAY_API_KEY"
display_name = "Internal Gateway"
default_context_window = 131072
default_max_tokens = 32768

[[providers.internal-gateway.models]]
id = "internal-flash"
context_window = 131072
max_tokens = 32768
input = ["text"]

[providers.internal-gateway.models.reasoning_efforts]
high = "high"
```

示例文件不含任何凭据；`api_key_env` 只是变量名。

## 凭据

- 配置、invocation patch、安装收据、API 响应和日志只出现环境变量名，不出现密钥值。
- 启动前在同一个 shell 里设置该变量，例如 `export INTERNAL_GATEWAY_API_KEY=...`。
- 管理 MCP 子进程只额外获得所选 provider 的凭据引用（以及既有的 `DEEPSEEK_*` 代理变量），不再依赖固定的 DeepSeek 变量列表。
- 普通业务工具和 repo 业务 MCP 仍只拿到清单声明的环境；provider 凭据不会因为选了自定义路由就传给它们。
- `doctor` 输出 `provider`、`credential_env` 和 `credential_configured`，缺凭据时返回非零。它只报告配置和凭据状态，不发推理请求。

## 生效范围与快照

- patch 由同一份运行时配置生成。dsh 组合里 `llm-pi-ai` 适配器已以休眠状态加载，配置该条目即注册路由；不会重复插入第二个适配器实例。
- 提交 Run 或创建对话时会把所选 provider 及全部路由定义写入该 Run/会话的运行时快照。之后修改全局配置只影响新建任务和新对话，已有会话不会隐式热切换；快照本身包含路由定义，执行时不重新读取变化的全局配置。
- 快照中只有路由定义（端点、协议、模型目录、凭据变量名），没有凭据值。

## 失败行为

以下情况都会明确失败，不会自动换源：

- `runtime.provider` 未定义，或与内置路由同名。
- `api` 不是三种受支持协议，`base_url` 不是 HTTP(S) 或带内嵌凭据。
- 模型目录为空、模型 ID 重复，或 `runtime.model` 不在目录中。
- 选择了模型未声明的推理档位。
- 声明了 `api_key_env` 但启动时该变量为空。

## 边界

- 本阶段只覆盖 dsh 执行入口；切换到 CodeAgent 时认证仍由该 CLI 及其内网账号体系提供，见 [CODEAGENT_MIGRATION.md](CODEAGENT_MIGRATION.md)。
- pi-ai 的 `compat`、`modelOverrides`、`requestImagePixelBudget` 等高级字段暂未开放，需要时再按真实网关行为逐项评估，不在此处预设。
- 用本地 OpenAI 兼容测试网关做过真实 dsh 链路验证（模型选择、工具调用、多轮工具结果回传和凭据隔离），验证记录见 [VALIDATION.md](VALIDATION.md)。真实内网网关尚未验收。
