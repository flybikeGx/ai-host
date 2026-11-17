"""本地模拟服务（mocks）：OSS 统一登录 / MCP / 知识库，供治理层端到端验证。

与生产代码（``governance`` / ``registry`` / ``gateway`` / ``cli``）解耦：这些是可独立运行的
假服务，仅用于本地验证 I-4 企业账号验证与网关/注册中心框架，**不承载真实凭据或正式知识**。

- ``oss``：模拟企业 OSS 统一登录 REST（``/auth/login`` + ``/auth/getUserDetail``）。
- ``mcp``：模拟通用 MCP 服务器（``echo`` / ``add``）。
- ``knowledge``：模拟知识库 MCP 服务（``knowledge_catalog`` / ``knowledge_search`` /
  ``knowledge_read`` / ``knowledge_submit``）。

运行入口见 CLI：``agent-base oss-mock`` / ``mcp-mock`` / ``knowledge-mock``。
"""
from __future__ import annotations

from .mcp import create_echo_mcp, echo_http_app, serve_echo
from .knowledge import create_knowledge_mcp, knowledge_http_app, serve_knowledge
from .oss import create_app as oss_app, serve as serve_oss

__all__ = [
    "oss_app",
    "serve_oss",
    "create_echo_mcp",
    "echo_http_app",
    "serve_echo",
    "create_knowledge_mcp",
    "knowledge_http_app",
    "serve_knowledge",
]
