---
name: analysis
description: 对数值求平均，生成研究摘要，使用本 repo 的领域 MCP 及报告工具。
---

# research 分析

1. 使用当前命名空间的 domain__analyze，参数 values 为 [2, 3, 5]。
2. 必须保留工具返回的完整对象，不要自行计算来代替 MCP 调用。
3. 调用同一命名空间的 write_report，analysis 参数为上一步完整返回对象，skill_marker 为 RESEARCH_SKILL_OK。
4. 报告保存在 artifacts/report.json，简要说明计算结果。
