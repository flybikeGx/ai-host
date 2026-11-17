"""RBAC 求值：有效权限 = 角色权限并集 ∪ 直授 allow − 直授 deny（deny 优先）。

域/组作用域收窄：只统计 ``domain_id ∈ {目标域, null}`` 且 ``group_id ∈ {目标组, null}``
的授权记录；``null`` 表示全局/全组。见 GOVERNANCE_DESIGN.md §4.1。
"""
from __future__ import annotations

from .store import ControlStore


def effective_permissions(store: ControlStore, user_id: str, domain_id: str | None,
                          group_id: str | None = None) -> set[str]:
    allowed = store.role_permission_names(user_id, domain_id, group_id)
    overrides = store.override_permission_names(user_id, domain_id, group_id)
    allowed |= overrides["allow"]
    allowed -= overrides["deny"]
    return allowed


def check(store: ControlStore, user_id: str, permission: str, domain_id: str | None,
          group_id: str | None = None) -> bool:
    return permission in effective_permissions(store, user_id, domain_id, group_id)


def user_domains(store: ControlStore, user_id: str) -> list[str]:
    """用户可访问的域集合：成员域 + 全局授权覆盖的所有域。"""
    memberships = {m["domain_id"] for m in store.list_memberships(user_id)}
    # 全局角色（domain_id IS NULL）视为跨域生效，纳入所有已知域。
    return sorted(memberships)
