# 后台任务与交付契约

本指南用于创建可无人值守的 Agent 仓库，以及判断一次 Run 是否真正完成。只安装不执行业务；普通聊天不强制按本契约输出。

## 仓库需要什么

| 文件 | 用途 |
| --- | --- |
| `AGENTS.md` | 角色、长期规则与权限边界，必需 |
| `agent.yaml` 或 Installation.md 配置块 | 安装与资源，机器配置来源二选一 |
| `entrypoint.md` | 无人值守步骤、输入、产物和缺输入行为，推荐 |
| `skills/*/SKILL.md` | 声明的专项技能 |

已有 entrypoint 时总会执行，显式 task 作为本次补充；没有入口才按 task → task.default 回退。都没有则准备失败，不能把泛泛角色当具体任务。

使用相对路径。工作输入位于 `input/`；触发参数在 `activation.json`。缺输入应报告 blocked，不等待真人，不盲目重试结果未知的外部操作。入口随安装版本冻结，更新需提交并重新安装。

## 最终报告

基座每次启动时注入报告要求，无需在 AGENTS、入口或技能中复制 schema。最终回复是单个 JSON 对象，不能带 Markdown 围栏；过程仍可正常调用工具。

```json
{
  "schema_version": 1,
  "status": "completed",
  "summary": "已生成并核对报告。",
  "artifacts": [{"path": "artifacts/report.json", "description": "汇总结果"}],
  "checks": [{"name": "核对输入总数", "status": "passed"}],
  "issues": [],
  "next_steps": []
}
```

完整格式以 [JSON Schema](../src/agent_base/schemas/agent-report-v1.schema.json) 为准。以上顶层字段必需，数组可为空；检查 status 为 passed、failed、not_run。Agent status 可用 completed、partial、blocked、failed，后三者使 Run 失败并保留现场。

## 如何判断成功

| 标记 | 说明 |
| --- | --- |
| Agent `completed` | 自报完成，仍需平台验证 |
| Run `succeeded` | 最终报告有效，声明产物与要求检查通过 |
| `checks_passed` | 平台执行了冻结的独立检查并通过 |
| `structural_only` | 只验证结构与文件要求，无独立业务检查 |
| `conversation_only` | 普通对话轮次成功，不代表后台交付验收 |
| `not_verified` | 验证未完成或失败 |

产物必须真实位于 `artifacts/`，不能使用绝对路径、反斜杠、重复路径或越界链接。平台合并清单与自报产物，检查并记录大小与 SHA-256。成功后归档、清理临时工作区；失败保留现场。

## 去哪里看结果

Run 的 `result.agent_report` 是 Agent 自报；运行目录 `report.json` 是基座执行报告，业务 `artifacts/report.json` 则是另一份文件。没有有效回复时 agent_report 为 null。

```sh
.venv/bin/agent-base status <run-id>
.venv/bin/agent-base events <run-id>
```

HTTP：`GET /api/runs/{run_id}/report`。活动 Run、仅安装和普通对话返回 409；接口不触发模型调用。

格式错误、重复字段、NaN/Infinity、额外文本、产物缺失都不会自动修复或重试。更换 CLI/模型后先试运行 [hello 示例](../examples/hello-agent/entrypoint.md)。容量与超时见 [规格速查](Specifications.md)，导出步骤见 [导出指南](export-guild.md)。
