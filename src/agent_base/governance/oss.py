"""企业内网账号（OSS 统一登录）验证客户端。

只做「验证账号」：拿 OSS 签发的 token 回查工号/部门/区域（``getUserDetail``），
密码不经平台（GOVERNANCE_DESIGN.md I-4 / AIHub 设计 §14.8）。平台不存 OSS 密码，
只在登录时把 token 透传给 OSS 换身份，随后签发平台自己的 JWT。

``http_transport`` 仅供测试注入，避免起真实网络服务。
"""
from __future__ import annotations

import httpx


class OssAuth:
    """OSS 身份回查门面。endpoint 为 OSS 服务根地址（如 ``http://127.0.0.1:8890``）。"""

    def __init__(self, endpoint: str, *, http_transport=None, timeout: float = 10.0):
        self.endpoint = endpoint.rstrip("/")
        self.http_transport = http_transport
        self.timeout = timeout

    def verify(self, token: str) -> dict:
        """校验 token 并回查身份，返回 ``{w3account, department, region}``。

        失败（不可达 / 非 200 / 缺工号）统一抛 ``ValueError``，由上层归一为 401。
        """
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, transport=self.http_transport) as client:
                response = client.post(
                    f"{self.endpoint}/auth/getUserDetail",
                    headers={"X-Auth-Token": token},
                )
        except httpx.HTTPError as error:
            raise ValueError(f"OSS 服务不可达：{type(error).__name__}") from error
        if response.status_code != 200:
            raise ValueError("OSS token 无效或已过期")
        data = (response.json() or {}).get("data") or {}
        w3account = data.get("w3account")
        if not w3account:
            raise ValueError("OSS 身份回查缺少工号")
        return {
            "w3account": w3account,
            "department": data.get("departmentPath", ""),
            "region": data.get("region", ""),
        }
