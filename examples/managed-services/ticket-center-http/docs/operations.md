# 运维手册：ticket-center（stdio 示例）

## 启动方式

由平台部署网关按清单 `runtime.argv` 启动；stdio 进程由网关持有，多个 Agent 共享同一部署代际。

## 数据

- `${service.data}/tickets.jsonl`：工单种子数据，安装步骤 `prepare.py` 初始化（已存在则跳过，幂等）。
- `${service.data}/subscriptions.jsonl`：订阅登记，追加写入；重启后保留。订阅后台消费属于未来业务实现，本示例只做登记。

## 凭据

`TICKET_AUTH` 只通过部署配置引用环境变量 `TICKET_SERVICE_AUTH`，值不进入仓库、日志或收据。

## 健康

- 协议探针：MCP initialize + 工具目录（expected_tools）。
- 业务探针：`ticket_search {query: "健康探测"}`（只读）。

## 故障处理建议

- 进程退出：restart_safe 已声明，可按部署恢复预算自动重启（退避 30/60/120 秒）。
- 鉴权失败：核对部署凭据引用，不要在探针里改凭据。
- 数据问题：备份 data 目录后处理；不要删除 subscriptions.jsonl。
