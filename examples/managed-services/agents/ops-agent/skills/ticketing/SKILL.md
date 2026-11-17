# 工单运营技能

- `ticket_search`：只读查询；结果含 `boot_id` 与 `total`。
- `ticket_subscribe`：幂等登记订阅；同一 (agent, project) 重复调用返回 duplicate=true，不会重复建订阅。
- 订阅记录保存在服务部署数据目录中，服务重启后仍保留。
