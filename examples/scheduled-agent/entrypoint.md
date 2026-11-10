# 定时汇总入口

1. 读取 activation.json，了解本次触发来源及 payload。
2. 读取 input/metrics.json；缺少必需输入时报告 blocked，不等待人工交互。
3. 调用 summarize_metrics，按服务汇总请求数和错误数，生成 artifacts/report.json。
4. 检查真实产物，概述服务数量、请求总数和错误总数。

不修改输入，不重复触发自身任务。最终回复遵循启动时注入的结构化汇报要求。
