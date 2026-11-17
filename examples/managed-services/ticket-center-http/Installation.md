# 工单中心服务安装说明（原生 HTTP 示例）

本组件提供与 stdio 示例相同的工单能力，以本机原生 HTTP MCP 提供，用于验证平台对 `streamable_http` 服务的托管安装、私有端口分配与网关路由。

- 平台将 `${service.port}` 分配给后端进程；对外只暴露部署网关的稳定端点。
- 后端校验 `Authorization` 请求头，凭据值来自部署配置引用的环境变量 `TICKET_SERVICE_HTTP_AUTH`（形如 `Bearer ...`）。
- 数据目录由平台按部署分配（`${service.data}`）。

```agent-base-install
schema_version: 3
kind: mcp-service
service:
  id: ticket-center
  version: 0.1.0
  contract: ticket-center/v1
  title: 工单查询与订阅（HTTP 示例）
installation:
  steps:
    - argv: ["${runtime.python}", "${package.root}/prepare.py", "${service.data}"]
      timeout_seconds: 30
runtime:
  transport: streamable_http
  argv: ["${runtime.python}", "${package.root}/server.py", "--host", "127.0.0.1", "--port", "${service.port}", "--data", "${service.data}"]
  endpoint_path: /mcp
health:
  interval_seconds: 30
  timeout_seconds: 5
  failure_threshold: 3
  recovery_threshold: 2
  expected_tools: [ticket_search, ticket_subscribe]
  probe_tool: {tool: ticket_search, arguments: {query: "健康探测"}}
  startup_timeout_seconds: 30
operations:
  runbook: docs/operations.md
  restart_safe: true
  session_sharing: safe
```
