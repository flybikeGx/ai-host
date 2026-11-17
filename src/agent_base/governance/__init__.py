"""治理层：多用户分权分域（domain/RBAC）的控制面。

与执行基座（agent-base 单用户执行引擎）解耦：独立 ``control.sqlite``、独立
``/api/v1`` 路由、独立令牌，不改动既有单用户运行逻辑。首版聚焦「身份与权限」，
供后续在页面上做用户/权限管理。网关、注册中心、资源、执行配额等属后续阶段。
"""
from __future__ import annotations

from .service import Governance

__all__ = ["Governance"]
