# 库存分析入口

读取当前 namespace 的 analysis skill，使用 domain__analyze 分析 [2, 3, 5]。
调用 write_report 保存 MCP 的完整返回对象，skill_marker 使用 INVENTORY_SKILL_OK。
交付 artifacts/report.json，核对实际文件与结果。
最终回复遵循基座启动时注入的汇报契约，不在仓库中另行定义格式。
