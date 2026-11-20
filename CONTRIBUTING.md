# 贡献要求

每次提交前，为改动增加或更新有意义的相关测试，并运行相关测试及 `python3 scripts/ci.py full`，确保单测、前端构建和真实端到端检查通过。不得以 skip、xfail、放宽断言或未经验证的承诺绕过失败。

未实现的多用户契约使用 `python3 scripts/ci.py contracts` 单独报告；历史缺口和本次回归分别记录，不把缺口隐藏为通过。bootstrap、快速检查、完整门禁及自愿安装本机 hooks 的步骤见 [本地 CI](docs/LocalCI.md)。仅修改本项目规则，不修改全局 Agent/Git 配置。
