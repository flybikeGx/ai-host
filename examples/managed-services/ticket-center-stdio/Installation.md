# 工单中心服务安装说明（stdio 示例）

本组件提供共享工单查询与幂等订阅登记能力，作为第二阶段托管 MCP 的 stdio 示例。

- 逻辑服务：`ticket-center`（契约 `ticket-center/v1`）。
- 数据目录由平台按部署分配（`${service.data}`），安装步骤负责初始化种子数据；订阅记录跨重启保留。
- 业务凭据只通过环境变量引用（`TICKET_AUTH` ← `TICKET_SERVICE_AUTH`），不写入本仓库。
- 维护责任人：服务部署登记的 owner。

先在本机用任意 MCP 客户端完成测试，再由 Admin 在 AI Host 托管安装。

```agent-base-install
schema_version: 3
kind: mcp-service
service:
  id: ticket-center
  version: 0.1.0
  contract: ticket-center/v1
  title: 工单查询与订阅（stdio 示例）
installation:
  steps:
    - argv: ["${runtime.python}", "${package.root}/prepare.py", "${service.data}"]
      timeout_seconds: 30
runtime:
  transport: stdio
  argv: ["${runtime.python}", "${package.root}/server.py", "${service.data}"]
  environment:
    TICKET_AUTH: {from_env: TICKET_SERVICE_AUTH}
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
