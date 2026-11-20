#!/usr/bin/env python3
"""Fail-closed coverage audit for the frozen C1–C7 contract set.

`scripts/ci.py full` is the gate that executes the contracts. This audit proves
that every contract in `docs/CHAT_MULTIUSER_DESIGN.md` §14 still has concrete,
collectable must-pass tests inside that gate; it never executes them. A missing
or renamed test therefore fails the audit instead of silently producing an
empty green suite.

The audit reads, but never writes, the real checkout:

* `pytest --collect-only -q` for the unit layer,
* `playwright test --list` for the real-process and browser suites,
* the stage names in `scripts/ci.py` and the migration markers in
  `scripts/test_real_e2e.py`, so removing a suite from `full` also fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# One entry per §14 row. Anchors name tests that already exist and are part of
# the default gate; they are the evidence that the row is still covered.
CONTRACTS = [
    {
        "phase": "C1",
        "name": "身份底座：Identity、双模认证、/me 域权限与 refresh",
        "pytest": [
            "tests/test_web_auth.py::test_identity_bearer_cookie_equivalence_and_stale_claims",
            "tests/test_web_auth.py::test_identity_bootstrap_loopback_and_jwt_mode",
            "tests/test_web_auth.py::test_domain_resolution_and_permissions",
            "tests/test_web_auth.py::test_auth_mode_loaded_from_toml_and_environment",
            "tests/test_governance.py::test_refresh_and_per_request_account_revalidation",
        ],
        "e2e": [
            "AIHO-12: real cookie login, restore, domain metadata and logout revocation",
            "C1: explicit invalid bearer never falls back to valid cookie",
        ],
    },
    {
        "phase": "C2",
        "name": "数据模型与迁移：归属列、安装域列与幂等回填",
        "pytest": [
            "tests/test_chat_migration.py::test_schema_backfill_backup_and_idempotency",
            "tests/test_chat_migration.py::test_missing_admin_and_domain_warn_and_later_backfill",
            "tests/test_chat_migration.py::test_startup_propagates_migration_failure",
            "tests/test_chat_migration.py::test_install_http_writes_default_and_page_domain",
        ],
        "runner_markers": ["migration_backup", "restart_idempotent", "failed_start_unavailable"],
    },
    {
        "phase": "C3",
        "name": "读路径归属：targets 按域、会话按 owner、按 id 访问收口 404",
        "pytest": [
            "tests/test_chat_authz.py::test_owner_isolation_on_list_snapshot_events_files_and_download",
            "tests/test_chat_authz.py::test_targets_are_domain_scoped_and_admin_hidden_from_members",
            "tests/test_chat_authz.py::test_null_owner_is_visible_only_to_bootstrap_and_admin_scope_all",
            "tests/test_chat_authz.py::test_snapshot_files_field_collapses_without_file_read",
        ],
        "e2e": [
            "C3 developer only reads own conversations",
            "C3 Agent catalog is domain-scoped and Admin Agent is platform-admin only",
            "C3 platform admin scope=all reads with audit and no scope leak",
            "C3 open SSE stream ends within 30 seconds after privilege revocation",
        ],
    },
    {
        "phase": "C4",
        "name": "写路径鉴权：新建/发消息/停止/改名归档 + 每用户并发与配额",
        "pytest": [
            "tests/test_chat_authz.py::test_write_routes_are_owner_only_and_admin_has_no_bypass",
            "tests/test_chat_authz.py::test_viewer_and_service_accounts_cannot_write",
            "tests/test_chat_authz.py::test_create_stamps_owner_and_domain_and_rejects_invisible_targets",
            "tests/test_chat_quota.py::test_per_user_quota_counts_only_that_user_and_rejects_the_ninth",
            "tests/test_chat_quota.py::test_global_32_turn_cap_stays_the_409_fallback",
            "tests/test_chat_quota.py::test_per_user_stream_quota_is_enforced_per_identity",
        ],
        "e2e": [
            "C4 platform admin has no write bypass and scope=all stays read-only",
            "C4 viewer and service accounts are rejected on every write route",
            "C4 ninth active turn hits the per-user 429 quota",
            "C4 fifth simultaneous SSE stream hits the per-user 429 quota",
        ],
    },
    {
        "phase": "C5",
        "name": "审计与 trace：chat 动作落库、turn_id 串联、不含正文",
        "pytest": [
            "tests/test_chat_audit.py::test_six_business_actions_land_with_frozen_shape",
            "tests/test_chat_audit.py::test_trace_and_user_filters_link_the_turn",
            "tests/test_chat_audit.py::test_audit_storage_failure_does_not_block_chat_actions",
        ],
        "e2e": ["C5 send and stop share turn trace in persisted audit"],
    },
    {
        "phase": "C6",
        "name": "前端：登录门控、域选择、按 /me 权限显隐、错误处理",
        "e2e": ["C6 AIHO-19 developer can open chat UI"],
        "ui": [
            "viewer /me permissions hide every create and write entry",
            "developer /me permissions allow writing but never the Admin Agent or scope=all",
            "platform admin /me shows the Admin Agent and the scope=all entry",
            "domain switch stores the id, sends x-domain-id and recovers from domain_forbidden",
            "no token or cookie ever reaches localStorage",
        ],
    },
    {
        "phase": "C7",
        "name": "回归与文档：全量测试、迁移演练、手工走查、部署与边界文档",
        "docs": [
            "docs/WebChat.md",
            "docs/ARCHITECTURE.md",
            "GOVERNANCE_DESIGN.md",
            "docs/CHAT_MULTIUSER_DESIGN.md",
            "docs/C7Validation.md",
        ],
        "minimums": {"pytest": 320, "e2e": 31, "ui": 31},
    },
]

# `full` must keep running every suite the contracts live in.
GATE_MARKERS = {
    "scripts/ci.py": ["browser-ui", "'unit'", "'real-e2e'", "'mock-restart'"],
    "scripts/test_real_e2e.py": ["auth|chat_authz|chat_write|chat_audit|chat_ui"],
}


def parse_pytest_collection(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines()
            if "::" in line and not line.startswith(("=", " ", "\t"))}


def parse_playwright_listing(text: str) -> set[str]:
    titles = set()
    for line in text.splitlines():
        if "›" in line:
            titles.add(line.split("›", 1)[1].strip())
    return titles


def evaluate(registry, *, pytest_ids, e2e_titles, ui_titles, runner_text,
             gate_markers, marker_texts, existing_docs):
    """Return (report, ok). Pure so the gate's rules stay directly testable."""
    faults = []
    report = []
    for script, needles in gate_markers.items():
        text = marker_texts.get(script, "")
        faults += [f"full gate does not run {needle} in {script}" for needle in needles if needle not in text]
    for contract in registry:
        missing = [item for item in contract.get("pytest", []) if item not in pytest_ids]
        missing += [item for item in contract.get("e2e", []) if item not in e2e_titles]
        missing += [item for item in contract.get("ui", []) if item not in ui_titles]
        missing += [item for item in contract.get("runner_markers", []) if item not in runner_text]
        missing += [item for item in contract.get("docs", []) if item not in existing_docs]
        for name, minimum in contract.get("minimums", {}).items():
            actual = {"pytest": len(pytest_ids), "e2e": len(e2e_titles), "ui": len(ui_titles)}[name]
            if actual < minimum:
                missing.append(f"{name}: expected >= {minimum}, collected {actual}")
        passed = not missing
        faults.extend(f"{contract['phase']}: {item}" for item in missing)
        report.append({"phase": contract["phase"], "name": contract["name"],
                       "covered": passed, "missing": missing})
    phases = [entry["phase"] for entry in registry]
    if phases != [f"C{index}" for index in range(1, 8)]:
        faults.append("契约登记表必须恰好覆盖 C1–C7")
    return {"contracts": report, "missing": faults,
            "totals": {"pytest": len(pytest_ids), "e2e": len(e2e_titles), "ui": len(ui_titles)}}, not faults


def _capture(command, cwd):
    environment = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "LANG", "PLAYWRIGHT_BROWSERS_PATH")}
    environment.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".ci-browsers"))
    process = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True)
    if process.returncode:
        raise SystemExit(f"{' '.join(command)} failed:\n{process.stdout}{process.stderr}")
    return process.stdout


def main():
    report_dir = Path(os.environ.get("CI_REPORT") or (ROOT / ".ci-reports" / "contracts"))
    report_dir.mkdir(parents=True, exist_ok=True)
    missing_binaries = [path for path in (ROOT / ".venv/bin/python", ROOT / "frontend/node_modules")
                        if not path.exists()]
    if missing_binaries:
        print("Run python3 scripts/ci.py bootstrap first", flush=True)
        return 2
    pytest_out = _capture([str(ROOT / ".venv/bin/python"), "-m", "pytest", "--collect-only", "-q"], ROOT)
    e2e_out = _capture(["npx", "--no-install", "playwright", "test",
                        "--config", "playwright.real.config.js", "--list"], ROOT / "frontend")
    ui_out = _capture(["npx", "--no-install", "playwright", "test", "--list"], ROOT / "frontend")
    report, ok = evaluate(
        CONTRACTS,
        pytest_ids=parse_pytest_collection(pytest_out),
        e2e_titles=parse_playwright_listing(e2e_out),
        ui_titles=parse_playwright_listing(ui_out),
        runner_text=(ROOT / "scripts/test_real_e2e.py").read_text(),
        gate_markers=GATE_MARKERS,
        marker_texts={script: (ROOT / script).read_text() if (ROOT / script).is_file() else ""
                      for script in GATE_MARKERS},
        existing_docs={contract_doc for contract in CONTRACTS for contract_doc in contract.get("docs", [])
                       if (ROOT / contract_doc).is_file()},
    )
    report["mode"] = "coverage"
    report["status"] = "passed" if ok else "failed"
    (report_dir / "contracts.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for contract in report["contracts"]:
        state = "covered" if contract["covered"] else "MISSING " + "; ".join(contract["missing"])
        print(f"{contract['phase']} {contract['name']}: {state}", flush=True)
    print("totals " + json.dumps(report["totals"]), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
