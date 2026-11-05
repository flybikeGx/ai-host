# inventory 初始化

本 repo 提供一个 analysis skill、一个常驻 domain MCP 和一个写报告工具。
MCP 安装后保持运行，同一安装版本的实例共享进程。技能只安装一次。
Python 环境需可导入 MCP SDK；演示直接使用基座的解释器，不额外下载依赖。

```agent-base-install
schema_version: 2
package:
  name: inventory-agent
  version: 0.1.0
task:
  default: 读取 analysis skill，使用本 repo MCP 分析 [2, 3, 5] 并生成报告。
installation:
  steps:
    - argv: ["${runtime.python}", "${package.root}/tools/initialize.py"]
      timeout_seconds: 10
skills:
  - name: analysis
    path: skills/analysis
mcp:
  servers:
    - id: domain
      transport: stdio
      argv: ["${runtime.python}", "${package.root}/mcp/domain_server.py"]
      timeout_seconds: 20
tools:
  local:
    - name: write_report
      description: 保存领域 MCP 的完整返回对象与 skill 标记到本次工作区。
      argv: ["${runtime.python}", "${package.root}/tools/write_report.py"]
      input_schema:
        type: object
        properties:
          analysis: {type: object}
          skill_marker: {type: string}
        required: [analysis, skill_marker]
        additionalProperties: false
completion:
  artifacts:
    - path: artifacts/report.json
      min_bytes: 10
  checks:
    - argv: ["${runtime.python}", "${package.root}/tools/verify.py"]
      timeout_seconds: 10
limits:
  wall_timeout_seconds: 180
```
