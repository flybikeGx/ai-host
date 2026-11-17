"""治理层：多用户分权分域（domain/RBAC）+ 服务注册中心 + 中央网关的控制面。

与执行基座（agent-base 单用户执行引擎）解耦：独立 ``control.sqlite``、独立
``/api/v1`` 路由、独立令牌，不改动既有单用户运行逻辑。已实现身份/RBAC 骨架、
服务注册中心（service/service_tool/service_check、embedded/remote 双模）与
中央网关（统一调用入口 + 反向代理 + 双层审计）。
"""
from __future__ import annotations

from .gateway import Gateway
from .oss import OssAuth
from .registry import ServiceRegistry
from .service import Governance

__all__ = ["Governance", "ServiceRegistry", "Gateway", "OssAuth"]
