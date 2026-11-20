"""§10 chat audit: frozen action/detail shapes and one writer for every row.

Every ``level="chat"`` row goes through :func:`insert`, so the detail whitelist
and trace rule are enforced in one place. Chat rows never carry message text,
assistant text, tool payloads, artifact content, titles, tokens or passwords.

A chat action must not fail because its audit row could not be written; the
HTTP wrapper :func:`write` logs the failure and lets the action succeed. The
startup migration is the exception: it calls :func:`insert` inside its own
transaction so a failed ``chat.migrate`` row still rolls the backfill back
(§8.4).
"""
from __future__ import annotations

import json
import logging
import time
import uuid

log = logging.getLogger(__name__)

# action -> (required detail keys, optional detail keys). ``mode`` is allowed on
# every identity-driven action (§10: token/bootstrap anonymous bootstrap marker).
DETAIL = {
    "chat.create": ({"conversation_id", "target", "target_kind", "title_auto", "model", "effort"}, set()),
    "chat.send": ({"conversation_id", "turn_id"}, set()),
    "chat.stop": ({"conversation_id", "turn_id", "requested"}, set()),
    "chat.rename": ({"conversation_id", "fields"}, set()),
    "chat.archive": ({"conversation_id", "archived"}, set()),
    "chat.download": ({"conversation_id", "path", "size"}, set()),
    "chat.scope_all": ({"path"}, {"conversation_id", "count"}),
    "chat.migrate": ({"conversations_owner", "conversations_domain",
                      "repo_installations_domain", "bootstrap_admin_id",
                      "default_domain_id"}, set()),
}
TRACED = {"chat.send", "chat.stop"}
MODE = "mode"
_TABLES = {"audit_log", "control.audit_log"}


def _require_text(value, field: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"chat 审计字段 {field} 必须是非空字符串")


def validate(action: str, trace_id: str | None, detail: dict) -> None:
    """Reject any row outside the frozen §10 shape before it is stored."""
    if action not in DETAIL:
        raise ValueError(f"未知 chat 审计动作：{action}")
    required, optional = DETAIL[action]
    unknown = set(detail) - required - optional - {MODE}
    if unknown:
        raise ValueError(f"chat 审计 {action} 含禁止字段：{sorted(unknown)}")
    missing = required - set(detail)
    if missing:
        raise ValueError(f"chat 审计 {action} 缺少字段：{sorted(missing)}")
    if action in TRACED:
        if trace_id != detail.get("turn_id"):
            raise ValueError(f"chat 审计 {action} 的 trace_id 必须等于 turn_id")
        if action == "chat.send":
            _require_text(detail["turn_id"], "turn_id")
        _require_text(detail["conversation_id"], "conversation_id")
    elif trace_id is not None:
        raise ValueError(f"chat 审计 {action} 不得设置 trace_id")
    if action == "chat.scope_all":
        _require_text(detail["path"], "path")
        _require_text(detail.get("conversation_id"), "conversation_id", allow_none=True)
        count = detail.get("count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ValueError("chat 审计 count 必须是非负整数或 null")
    if action == "chat.migrate":
        for key in ("conversations_owner", "conversations_domain", "repo_installations_domain"):
            value = detail[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"chat 审计 {key} 必须是非负整数")
        _require_text(detail["bootstrap_admin_id"], "bootstrap_admin_id", allow_none=True)
        _require_text(detail["default_domain_id"], "default_domain_id", allow_none=True)


def insert(conn, *, action: str, domain_id: str | None, user_id: str | None,
           trace_id: str | None, detail: dict, level: str = "chat",
           table: str = "audit_log") -> None:
    """Write one audit row on an existing connection.

    ``table`` is an internal constant; it exists so the startup migration can
    write into its attached ``control`` schema inside the same transaction.
    """
    if table not in _TABLES:
        raise ValueError(f"未知审计表：{table}")
    payload = dict(detail)
    if level == "chat":
        validate(action, trace_id, payload)
    conn.execute(
        f"INSERT INTO {table}(id,domain_id,user_id,trace_id,level,action,detail,at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, domain_id, user_id, trace_id, level, action,
         json.dumps(payload, ensure_ascii=False), time.time()))


def write(store, identity, action: str, *, domain_id: str | None,
          detail: dict | None = None, trace_id: str | None = None) -> None:
    """Audit one identity-driven chat action without ever blocking it.

    The detail carries ``mode`` so a NULL user_id (anonymous bootstrap) is
    still attributable to the token/bootstrap path (§10). Any storage or shape
    failure is logged locally and swallowed.
    """
    payload = dict(detail or {})
    payload[MODE] = identity.mode
    try:
        store.audit(domain_id=domain_id, user_id=identity.user_id, trace_id=trace_id,
                    level="chat", action=action, detail=payload)
    except Exception:
        log.exception("chat audit write failed: action=%s conversation_id=%s",
                      action, payload.get("conversation_id"))
