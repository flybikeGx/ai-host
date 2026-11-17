读取技能说明后，使用 tickets 服务完成两步：
1) ticket_search 查询关键词"支付"的工单；
2) ticket_subscribe 以 agent 名 ops-agent、project 名 demo-project 登记订阅（重复调用应返回 duplicate）。
把查询结果与订阅登记（subscription_id、duplicate）写入 artifacts/ops.json，并按统一汇报契约输出报告。
