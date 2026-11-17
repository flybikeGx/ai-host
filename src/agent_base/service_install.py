"""Managed shared-service installation control plane.

The ManagedServices application service owns install planning, durable
operations, private start/protocol validation, and deployment lifecycle with
persistent locks and generations. It never touches v2 repo-private services.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from .config import Config
from .engines import CREDENTIAL_KEYWORDS, provider_credential_names, runtime_environment, scrub_environment
from .manifests import (DEFAULT_HEALTH, freeze_service_source, load_service_manifest, manifest_digest,
                         service_expand)
from .package import within
from .processes import Cancelled, DeadlineExceeded, alive, identity, kill_tree, run_process, spawn_options
from .repositories import installation_lock
from .service_store import (OPERATION_ACTIVE, OPERATION_TERMINAL, ServiceStore, encoded)
from .store import Store


INSTALL_DEADLINE = 900
STARTUP_WAIT = 60
DRAIN_TIMEOUT = 30


def gateway_token_path(deployment_root: Path) -> Path:
    return deployment_root / "token"


class ManagedServices:
    """Install, validate and operate shared MCP deployments."""

    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.data)
        self.services = ServiceStore(config.data)
        self.revisions_root = config.data / "services"
        self.deployments_root = config.data / "deployments"

    # -- paths ----------------------------------------------------------------
    def revision_root(self, service_id: str, revision_id: str) -> Path:
        return self.revisions_root / service_id / "revisions" / revision_id

    def deployment_root(self, deployment_id: str) -> Path:
        return self.deployments_root / deployment_id

    @staticmethod
    def is_builtin(deployment: dict | None) -> bool:
        """Platform-owned components (currently the knowledge MCP) use the same
        catalog, lock and probe layers but a dedicated process manager."""
        return bool(deployment and deployment.get("config", {}).get("builtin") == "knowledge")

    def _prepare_dirs(self, deployment_id: str) -> Path:
        root = self.deployment_root(deployment_id)
        (root / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    # -- planning -------------------------------------------------------------
    def plan_install(self, candidate: dict, *, deployment_id: str | None = None, owner: str = "",
                     credentials: dict | None = None, env: dict | None = None, port: int | None = None,
                     backend_headers: dict | None = None, auth_mode: str | None = None,
                     publish: bool = False, allow_guardian_restart: bool = False) -> dict:
        """Freeze an install plan from a pinned review candidate. Read-only."""
        checkout = Path(candidate["root"]) / "checkout"
        component = candidate.get("component")
        base = checkout if component is None else within(checkout, component)
        manifest = load_service_manifest(base)
        service = manifest["service"]
        manifest_hash = manifest_digest(manifest)
        revision_id = hashlib.sha256(encoded({"service": service["id"], "origin": candidate["origin"],
                                              "commit": candidate["commit"], "component": component or "",
                                              "manifest": manifest}).encode()).hexdigest()[:24]
        deployment = self.services.deployment(deployment_id) if deployment_id else None
        existing_revision = self.services.revision(revision_id)
        credentials = credentials or {}
        backend_headers = backend_headers or {}
        if auth_mode is not None and auth_mode not in {"platform", "passthrough"}:
            raise ValueError("auth_mode 必须是 platform 或 passthrough")
        missing = []
        for dest, value in credentials.items():
            if isinstance(value, dict) and value.get("from_env") and not os.environ.get(value["from_env"]):
                missing.append(value["from_env"])
        for value in backend_headers.values():
            if isinstance(value, dict) and value.get("from_env") and not os.environ.get(value["from_env"]):
                missing.append(value["from_env"])
        for value in manifest.get("runtime", {}).get("environment", {}).values():
            if isinstance(value, dict) and value.get("from_env") and not os.environ.get(value["from_env"]):
                missing.append(value["from_env"])
        risks = []
        if manifest.get("operations", {}).get("session_sharing") == "isolated":
            risks.append("服务声明 session_sharing: isolated，不能作为共享部署绑定多个 Agent")
        if deployment and deployment["mode"] == "external":
            risks.append("同名部署已登记为外部服务，托管安装需先删除外部登记")
        if not manifest.get("operations", {}).get("restart_safe"):
            risks.append("服务未声明 restart_safe，自动恢复不会重启它")
        if manifest.get("environment", {}).get("lockfile"):
            risks.append("安装需要 uv 与锁定的依赖环境")
        return {
            "kind": "install", "deployment_id": deployment_id, "service_id": service["id"],
            "service_title": service.get("title", ""), "contract": service.get("contract", ""),
            "origin": candidate["origin"], "ref": candidate.get("ref"), "commit": candidate["commit"],
            "component": component, "candidate_id": candidate["id"],
            "manifest_digest": manifest_hash, "revision_id": revision_id,
            "reuse_revision": bool(existing_revision), "transport": manifest["runtime"]["transport"],
            "owner": owner, "credentials": credentials, "env": env or {}, "port": port,
            "backend_headers": backend_headers,
            "auth_mode": auth_mode or ("passthrough" if auth_mode == "passthrough" else "platform"),
            "publish": bool(publish), "allow_guardian_restart": bool(allow_guardian_restart),
            "health": {**DEFAULT_HEALTH, **manifest.get("health", {})},
            "expected_tools": manifest.get("health", {}).get("expected_tools", []),
            "runbook": manifest.get("operations", {}).get("runbook"),
            "restart_safe": bool(manifest.get("operations", {}).get("restart_safe")),
            "session_sharing": manifest.get("operations", {}).get("session_sharing", "safe"),
            "existing_deployment": {k: deployment[k] for k in ("id", "revision_id", "desired_state", "publish_state", "mode")} if deployment else None,
            "missing": sorted(set(missing)), "risks": risks, "progress": {},
        }

    # -- operations -----------------------------------------------------------
    def _active_install(self, deployment_id: str, *, operation_id: str | None = None) -> dict | None:
        return self.services.active_install(deployment_id, operation_id=operation_id)

    def apply_install(self, plan: dict, *, idempotency_key: str | None = None,
                      background: bool = True, deadline: float | None = None) -> dict:
        deployment_id = plan.get("deployment_id")
        if not deployment_id:
            raise ValueError("安装计划缺少 deployment_id")
        if plan.get("missing"):
            raise ValueError(f"安装缺少环境配置，先补齐后重试：{plan['missing']}")
        # The store atomically checks admission and creates the operation;
        # a separate read here cannot reserve the deployment for this request.
        operation = self.services.create_operation(deployment_id=deployment_id, kind="install",
                                                   plan=plan, idempotency_key=idempotency_key)
        if operation.get("duplicate"):
            return operation
        if background:
            self._spawn_executor(operation["id"], deadline or INSTALL_DEADLINE)
        else:
            self._execute_install(operation["id"], deadline or INSTALL_DEADLINE)
            operation = self.services.operation(operation["id"])
        return operation

    def _spawn_executor(self, operation_id: str, deadline: float):
        def run():
            try:
                self._execute_install(operation_id, deadline)
            except BaseException:
                import traceback
                traceback.print_exc()

        thread = threading.Thread(target=run, name=f"service-install-{operation_id}", daemon=True)
        thread.start()

    def get_operation(self, operation_id: str, *, wait: float = 0) -> dict:
        until = time.time() + max(0, wait)
        while True:
            operation = self.services.operation(operation_id)
            if not operation:
                raise ValueError(f"操作不存在：{operation_id}")
            if operation["status"] in OPERATION_TERMINAL or time.time() >= until:
                return operation
            time.sleep(0.1)

    def cancel_operation(self, operation_id: str) -> dict:
        operation = self.services.operation(operation_id)
        if not operation:
            raise ValueError(f"操作不存在：{operation_id}")
        if operation["status"] in OPERATION_TERMINAL:
            return operation
        self.services.update_operation(operation_id, status="cancelled")
        return self.services.operation(operation_id)

    def _update_progress(self, operation_id: str, **fields):
        operation = self.services.operation(operation_id)
        plan = operation["plan"]
        progress = {**plan.get("progress", {}), **fields}
        plan["progress"] = progress
        self.services.update_operation(operation_id, plan=plan)

    def _execute_install(self, operation_id: str, deadline_seconds: float):
        operation = self.services.operation(operation_id)
        if operation["status"] in OPERATION_TERMINAL:
            return
        plan = operation["plan"]
        deployment_id = operation["deployment_id"]

        def cancelled():
            item = self.services.operation(operation_id)
            return not item or item["status"] == "cancelled"

        deadline = time.time() + deadline_seconds
        previous = None
        candidate_config = None
        self.services.update_operation(operation_id, status="preparing", pid=os.getpid(), birth=identity(os.getpid()))
        try:
            deployment, previous, candidate_config = self._install_materials(operation_id, plan, deadline, cancelled)
            self.services.update_operation(operation_id, status="installing")
            deployment = self._run_install_steps(operation_id, plan, deployment, deadline, cancelled)
            # Candidate and active are separate until installation steps have
            # completed. Only now may the deployment record point at it; the
            # active install operation blocks every other lifecycle entry.
            if previous is not None:
                current = self._reload(deployment_id)
                existing = {key: value for key, value in current["config"].items() if key != "tools"}
                if current["revision_id"] != plan["revision_id"] or existing != candidate_config:
                    self.services.update_deployment(deployment_id, revision_id=plan["revision_id"],
                                                     config=candidate_config,
                                                     config_revision=previous["config_revision"] + 1)
                    deployment = self._reload(deployment_id)
            self.services.update_operation(operation_id, status="validating")
            self.start(deployment_id, reason=f"install:{operation_id}", operation_id=operation_id)
            result = self.validate(deployment_id, business=True, operation_id=operation_id)
            if not result["ok"]:
                raise ValueError(f"私有启动验证失败：{result['errors']}")
            deployment = self._reload(deployment_id)
            config = {**deployment["config"], "tools": result["tools"]}
            self.services.update_deployment(deployment_id, config=config, verification=result["verification"],
                                             observed_state="healthy", observed_at=time.time(),
                                             observed_evidence=encoded({"kind": "install_validation", **result}))
            self.services.health_event(deployment_id, "business" if result["verification"] == "business_verified" else "protocol",
                                       "healthy", result)
            if plan.get("publish") and result["ok"]:
                from .service_catalog import ServiceCatalog
                ServiceCatalog(self.config).publish(deployment_id, actor=f"install:{operation_id}")
            self.services.update_operation(operation_id, status="succeeded",
                                           plan={**plan, "result": {"verification": result["verification"],
                                                                    "tools": result["tools"],
                                                                    "generation": self.services.deployment(deployment_id)["generation"]}})
        except Cancelled:
            self._finish_operation(operation_id, "cancelled", "安装已取消")
            self._recover_failed_install(deployment_id, plan, previous)
        except BaseException as error:
            self._finish_operation(operation_id, "failed", str(error)[:1000])
            self._recover_failed_install(deployment_id, plan, previous)

    def _recover_failed_install(self, deployment_id: str, plan: dict, previous: dict | None):
        """Restore the active generation when a candidate install did not switch.

        A rejected upgrade must leave the old gateway and its in-flight calls
        untouched. Only a gateway this operation actually created is stopped.
        """
        try:
            current = self.services.deployment(deployment_id)
        except ValueError:
            return
        if current is None:
            return
        if previous:
            old_alive = bool(previous.get("gateway_pid")) and alive(previous["gateway_pid"],
                                                                     previous.get("gateway_birth"))
            if old_alive:
                self.services.update_deployment(
                    deployment_id, revision_id=previous["revision_id"], config=previous["config"],
                    config_revision=previous["config_revision"], desired_state=previous["desired_state"],
                    generation=previous["generation"], gateway_pid=previous["gateway_pid"],
                    gateway_birth=previous["gateway_birth"], observed_state=previous["observed_state"],
                    observed_at=previous["observed_at"], observed_evidence=previous["observed_evidence"],
                    verification=previous["verification"], endpoint=previous["endpoint"])
                self.services.health_event(deployment_id, "process", "upgrade_rejected_restored",
                                           {"candidate_revision": plan.get("revision_id"),
                                            "active_revision": previous["revision_id"]})
                return
            # The old generation was already stopped: stop only the candidate's
            # own gateway, then record an explicit stopped state instead of
            # pretending the old revision is still serving.
            if current.get("gateway_pid") and current["revision_id"] == plan.get("revision_id"):
                try:
                    self._stop_locked(current, f"install-failed:{plan.get('revision_id')}",
                                      drain=False, desired="stopped")
                except (OSError, ValueError):
                    pass
            self.services.update_deployment(deployment_id, revision_id=previous["revision_id"],
                                            config=previous["config"],
                                            config_revision=previous["config_revision"],
                                            desired_state="stopped", gateway_pid=None, gateway_birth=None,
                                            observed_state="stopped", observed_at=time.time(),
                                            observed_evidence=encoded({"kind": "upgrade_failed",
                                                                       "candidate_revision": plan.get("revision_id")}),
                                            verification=previous["verification"], endpoint=None)
            self.services.health_event(deployment_id, "process", "upgrade_failed_requires_input",
                                       {"candidate_revision": plan.get("revision_id")})
            return
        # A brand-new deployment: clean up the gateway this operation created.
        if current["mode"] == "managed" and current.get("gateway_pid"):
            try:
                self._stop_locked(current, "install-failed", drain=False, desired="stopped")
            except (OSError, ValueError):
                pass

    def _finish_operation(self, operation_id: str, status: str, error: str | None):
        try:
            self.services.update_operation(operation_id, status=status, error=error)
        except ValueError:
            pass  # already terminal (e.g. cancelled while failing)

    def _install_materials(self, operation_id: str, plan: dict, deadline: float, cancelled) -> dict:
        service_id = plan["service_id"]
        revision_id = plan["revision_id"]
        revision = self.services.revision(revision_id)
        if revision:
            self._update_progress(operation_id, revision_ready=True)
        else:
            # The plan always carries the pinned candidate id from the review flow.
            from .review import Reviews
            reviews = Reviews(self.config)
            item = reviews.candidate(plan["candidate_id"])
            checkout = Path(item["root"]) / "checkout"
            base = checkout if not plan.get("component") else within(checkout, plan["component"])
            root = self.revision_root(service_id, revision_id)
            source = root / "source"
            if source.exists():
                # A previous executor crashed after freezing; the directory is
                # immutable-by-construction so re-freezing is a safe repeat.
                shutil.rmtree(source, ignore_errors=True)
            manifest = freeze_service_source(base, source)
            if manifest_digest(manifest) != plan["manifest_digest"]:
                raise ValueError("冻结后的清单摘要与计划不一致，安装中止")
            (root / "env").mkdir(parents=True, exist_ok=True)
            revision = self.services.save_revision(service_id=service_id, origin=plan["origin"],
                                                   commit=plan["commit"], component=plan.get("component") or "",
                                                   manifest=manifest, root=root, python=sys.executable)
            self._update_progress(operation_id, revision_ready=True)
        deployment = self.services.deployment(plan["deployment_id"])
        config = {"owner": plan.get("owner", ""), "credentials": plan.get("credentials") or {},
                  "env": plan.get("env") or {}, "allow_guardian_restart": plan.get("allow_guardian_restart", False),
                  "backend_headers": plan.get("backend_headers") or {},
                  "auth_mode": plan.get("auth_mode") or "platform",
                  "health": {k: plan["health"][k] for k in ("interval_seconds", "timeout_seconds", "failure_threshold",
                                                            "recovery_threshold", "startup_timeout_seconds")}}
        if plan.get("port"):
            config["port"] = plan["port"]
        if deployment:
            if deployment["mode"] == "external":
                raise ValueError("同名部署已登记为外部服务")
            # Keep the active generation identity for rollback: a rejected or
            # failed candidate must not silently replace the serving revision.
            previous = {key: deployment[key] for key in
                        ("revision_id", "config", "config_revision", "desired_state", "generation",
                         "gateway_pid", "gateway_birth", "observed_state", "observed_at",
                         "observed_evidence", "verification", "endpoint")}
            plan = {**plan, "previous": previous, "candidate_revision_id": revision_id}
            self.services.update_operation(operation_id, plan=plan)
            # Do not touch the active revision/config while steps run. The
            # candidate is promoted only after installation succeeds.
            return deployment, previous, config
        port = plan.get("port") or self._allocate_port()
        deployment = self.services.create_deployment(plan["deployment_id"], service_id=service_id, mode="managed",
                                                     config=config, revision_id=revision_id, gateway_port=port)
        # Steps reference ${service.data}; the persistent data directory exists
        # from creation and survives every later install failure.
        self._prepare_dirs(deployment["id"])
        return deployment, None, config

    def _run_install_steps(self, operation_id: str, plan: dict, deployment: dict,
                           deadline: float, cancelled) -> dict:
        revision = self.services.revision(plan["revision_id"])
        root = self.revision_root(deployment["service_id"], plan["revision_id"])
        source = root / "source"
        python = sys.executable
        data_dir = self.deployment_root(deployment["id"]) / "data"
        logs = self.deployment_root(deployment["id"]) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        # Repo code never receives model provider credentials.
        install_env = scrub_environment(runtime_environment(self.config.runtime, root / "install-home", credential=False),
                                        provider_credential_names(self.config.runtime), keywords=False)
        manifest = revision["manifest"]
        if manifest.get("environment", {}).get("lockfile"):
            self._check_cancelled(cancelled, deadline)
            if self.services.operation(operation_id)["plan"]["progress"].get("dependencies_installed"):
                pass  # safely repeatable
            env = {**install_env, "UV_PROJECT_ENVIRONMENT": str(root / "env"),
                   "UV_CACHE_DIR": str(self.config.data / "uv-cache")}
            code = run_process(["uv", "sync", "--frozen", "--no-dev", "--project", str(source)], cwd=source, env=env,
                               output=logs / "dependencies.log", error=logs / "dependencies.stderr.log",
                               deadline=deadline, cancelled=cancelled)
            if code:
                raise ValueError("安装依赖失败，见 logs/dependencies.stderr.log")
            python = str(root / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            self.services.save_revision(service_id=deployment["service_id"], origin=revision["origin"],
                                        commit=revision["commit_sha"], component=revision["component"],
                                        manifest=manifest, root=root, python=python)
            self._update_progress(operation_id, dependencies_installed=True)
        for index, step in enumerate(manifest.get("installation", {}).get("steps", [])):
            key = f"step_{index}"
            progress = self.services.operation(operation_id)["plan"]["progress"]
            if progress.get(key):
                continue
            self._check_cancelled(cancelled, deadline)
            self._update_progress(operation_id, in_flight_step=index)
            code = run_process(service_expand(step["argv"], package=source, python=python, data_dir=data_dir),
                               cwd=source, env=install_env, output=logs / f"install-{index}.log",
                               error=logs / f"install-{index}.stderr.log",
                               deadline=min(deadline, time.time() + step.get("timeout_seconds", 120)), cancelled=cancelled)
            if code:
                # The step may have partially applied; its outcome is unknown.
                self.services.update_operation(operation_id, status="needs_input",
                                               error=f"安装步骤 {index} 失败（退出码 {code}）；结果未知，不会自动重跑")
                raise Cancelled("安装步骤失败")
            progress = {**self.services.operation(operation_id)["plan"]["progress"], key: True}
            progress.pop("in_flight_step", None)
            self._update_progress(operation_id, **progress)
        return deployment

    @staticmethod
    def _check_cancelled(cancelled, deadline):
        if cancelled():
            raise Cancelled("安装已取消")
        if time.time() >= deadline:
            raise DeadlineExceeded("安装超时")

    def _allocate_port(self) -> int:
        """Pick and verify a loopback gateway port outside the ephemeral range."""
        import random
        for _ in range(20):
            candidate = random.randint(21000, 32000)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("127.0.0.1", candidate))
                    return candidate
            except OSError:
                continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    # -- gateway environment --------------------------------------------------
    def required_secret_envs(self, deployment: dict, manifest: dict) -> set[str]:
        needed = set()
        for value in manifest.get("runtime", {}).get("environment", {}).values():
            if isinstance(value, dict) and value.get("from_env"):
                needed.add(value["from_env"])
        for value in deployment["config"].get("credentials", {}).values():
            if isinstance(value, dict) and value.get("from_env"):
                needed.add(value["from_env"])
        for key in ("headers", "backend_headers"):
            for value in deployment["config"].get(key, {}).values():
                if isinstance(value, dict) and value.get("from_env"):
                    needed.add(value["from_env"])
        return needed

    def gateway_environment(self, deployment: dict) -> tuple[dict, list[str]]:
        """Explicit business secrets only; model credentials never reach gateways."""
        if self.is_builtin(deployment):
            return {}, []
        manifest = self.services.revision(deployment["revision_id"])["manifest"] if deployment.get("revision_id") else {}
        base = runtime_environment(self.config.runtime, self.deployment_root(deployment["id"]) / "gateway-home", credential=False)
        needed = self.required_secret_envs(deployment, manifest)
        env = {key: value for key, value in base.items()
               if key in needed or not any(word in key.upper() for word in CREDENTIAL_KEYWORDS)}
        missing = sorted(name for name in needed if not env.get(name))
        return env, missing

    # -- lifecycle ------------------------------------------------------------
    def ensure_external_gateway(self, deployment_id: str, *, reason: str = "external") -> dict:
        """Start the platform-owned loopback access layer for an external MCP."""
        root = self._prepare_dirs(deployment_id)
        with installation_lock(root / "deploy.lock", 120):
            deployment = self._reload(deployment_id)
            if deployment["mode"] != "external":
                raise ValueError("只有外部服务使用接入层启动")
            if deployment["maintenance"]:
                raise ValueError("部署维护中，不能启动接入层")
            if deployment["desired_state"] == "removed":
                raise ValueError("部署已标记删除")
            health = self.gateway_health(deployment)
            if health and health.get("gateway_alive"):
                return deployment
            return self._start_external_locked(deployment, reason, root)

    def _start_external_locked(self, deployment: dict, reason: str, root: Path) -> dict:
        deployment_id = deployment["id"]
        env, missing = self.gateway_environment(deployment)
        if missing:
            raise ValueError(f"缺少外部服务凭据环境变量：{missing}（只设置环境变量，不写入配置）")
        token_file = gateway_token_path(root)
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32))
            token_file.chmod(0o600)
        if deployment.get("gateway_pid") and alive(deployment["gateway_pid"], deployment["gateway_birth"]):
            kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])
        generation = deployment["generation"] + 1
        port = deployment["gateway_port"] or self._allocate_port()
        self.services.update_deployment(deployment_id, generation=generation, desired_state="running",
                                        observed_state="starting", observed_at=time.time(),
                                        gateway_port=port,
                                        observed_evidence=encoded({"kind": "external_starting",
                                                                   "reason": reason}))
        (root / "runtime.json").unlink(missing_ok=True)
        with (root / "logs" / "gateway.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, "-m", "agent_base.deployment_gateway",
                                      "--config", str(self.config.path), "--deployment", deployment_id],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **spawn_options())
        birth = identity(child.pid)
        self.services.update_deployment(deployment_id, gateway_pid=child.pid, gateway_birth=birth)
        until = time.monotonic() + max(5, STARTUP_WAIT)
        while time.monotonic() < until:
            if not alive(child.pid, birth):
                break
            runtime = self._runtime_state(self._reload(deployment_id))
            if runtime and runtime.get("generation") == generation and runtime.get("ready"):
                self.services.update_deployment(deployment_id, endpoint=f"http://127.0.0.1:{port}/mcp",
                                                observed_state="healthy", observed_at=time.time(),
                                                observed_evidence=encoded({"kind": "external_access_ready",
                                                                           "reason": reason}))
                self.services.health_event(deployment_id, "process", "external_access_started",
                                           {"generation": generation, "port": port})
                return self._reload(deployment_id)
            time.sleep(0.1)
        kill_tree(child.pid, birth)
        self.services.update_deployment(deployment_id, gateway_pid=None, gateway_birth=None,
                                        observed_state="unhealthy", observed_at=time.time(),
                                        observed_evidence=encoded({"kind": "external_access_failed",
                                                                   "reason": reason}))
        raise ValueError(f"外部接入层启动失败，见 {root / 'logs' / 'gateway.log'}")

    def start(self, deployment_id: str, *, reason: str = "admin", operation_id: str | None = None) -> dict:
        root = self._prepare_dirs(deployment_id)
        with installation_lock(root / "deploy.lock", 120):
            deployment = self._reload(deployment_id)
            active = self._active_install(deployment_id, operation_id=operation_id)
            if active:
                raise ValueError(f"部署正在安装/升级中（操作 {active['id']}，{active['status']}）；"
                                 "外部启动入口不会激活候选代际")
            if deployment["maintenance"]:
                raise ValueError("部署维护中，不能启动；请先结束维护状态")
            if deployment["desired_state"] == "removed":
                raise ValueError("部署已标记删除")
            if self.is_builtin(deployment):
                from .knowledge_migration import start_builtin
                return start_builtin(self, deployment, reason)
            if deployment["mode"] == "external":
                # Starting an external service only enables the platform-owned
                # loopback route; the remote process is never started.
                health = self.gateway_health(deployment)
                if health and health.get("gateway_alive") and health.get("ready"):
                    return deployment
                return self._start_external_locked(deployment, reason, root)
            health = self.gateway_health(deployment)
            if health and health.get("gateway_alive") and health.get("ready"):
                # A healthy gateway only counts if it runs the revision/config
                # this deployment record now points at. Reinstalling a new
                # commit must switch generations instead of reusing old code.
                running = (health.get("revision_id"), health.get("config_revision"))
                target = (deployment.get("revision_id"), deployment.get("config_revision"))
                if running == target:
                    return deployment
                self.services.health_event(deployment_id, "process", "revision_switch",
                                           {"running_revision": running[0], "target_revision": target[0],
                                            "running_config": running[1], "target_config": target[1]})
                if health.get("in_flight"):
                    self._drain(deployment, timeout=DRAIN_TIMEOUT)
                    remaining = (self.gateway_health(deployment) or {}).get("in_flight")
                    if remaining:
                        raise ValueError("存在在途业务调用，不能安全排空以切换版本；旧代际保持运行")
                self._stop_locked(deployment, "revision-switch", drain=False, desired="running")
                deployment = self._reload(deployment_id)
            if deployment.get("gateway_pid") and alive(deployment["gateway_pid"], deployment["gateway_birth"]):
                kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])
            env, missing = self.gateway_environment(deployment)
            if missing:
                raise ValueError(f"缺少业务凭据环境变量：{missing}（只设置环境变量，不写入配置）")
            token_file = gateway_token_path(root)
            if not token_file.exists():
                token_file.write_text(secrets.token_urlsafe(32))
                token_file.chmod(0o600)
            generation = deployment["generation"] + 1
            self.services.update_deployment(deployment_id, generation=generation, desired_state="running",
                                            observed_state="starting", observed_at=time.time(),
                                            observed_evidence=encoded({"kind": "starting", "reason": reason}))
            (root / "runtime.json").unlink(missing_ok=True)
            manifest = self.services.revision(deployment["revision_id"])["manifest"]
            timeout = manifest.get("health", {}).get("startup_timeout_seconds", STARTUP_WAIT)
            port = deployment["gateway_port"]
            for attempt in range(3):
                with (root / "logs" / "gateway.log").open("ab") as log:
                    child = subprocess.Popen([sys.executable, "-m", "agent_base.deployment_gateway",
                                              "--config", str(self.config.path), "--deployment", deployment_id],
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **spawn_options())
                birth = identity(child.pid)
                self.services.update_deployment(deployment_id, gateway_pid=child.pid, gateway_birth=birth,
                                                gateway_port=port)
                until = time.monotonic() + max(5, timeout)
                while time.monotonic() < until:
                    if not alive(child.pid, birth):
                        break
                    runtime = self._runtime_state(self._reload(deployment_id))
                    if runtime and runtime.get("generation") == generation and runtime.get("ready"):
                        self.services.update_deployment(deployment_id,
                                                        endpoint=f"http://127.0.0.1:{port}/mcp")
                        return self._reload(deployment_id)
                    time.sleep(0.1)
                kill_tree(child.pid, birth)
                log_tail = self._log_tail(root / "logs" / "gateway.log")
                if attempt < 2 and "Address already in use" in log_tail:
                    # The recorded port was taken between allocation and bind;
                    # move to a fresh one and record it as the new stable address.
                    port = self._allocate_port()
                    (root / "runtime.json").unlink(missing_ok=True)
                    continue
                self.services.update_deployment(deployment_id, observed_state="unhealthy", observed_at=time.time(),
                                                observed_evidence=encoded({"kind": "startup_timeout", "reason": reason}))
                raise ValueError(f"服务网关启动失败，见 {root / 'logs' / 'gateway.log'}")
            raise ValueError(f"服务网关启动失败，见 {root / 'logs' / 'gateway.log'}")

    @staticmethod
    def _log_tail(path: Path, limit: int = 4000) -> str:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - limit))
                return stream.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self, deployment_id: str, *, reason: str = "admin", drain: bool = True,
             desired: str = "stopped") -> dict:
        root = self.deployment_root(deployment_id)
        root.mkdir(parents=True, exist_ok=True)
        with installation_lock(root / "deploy.lock", 120):
            active = self._active_install(deployment_id)
            if active:
                raise ValueError(f"部署正在安装/升级中（操作 {active['id']}，{active['status']}）；"
                                 "请通过操作取消或等待完成")
            return self._stop_locked(self._reload(deployment_id), reason, drain=drain, desired=desired)

    def _stop_locked(self, deployment: dict, reason: str, *, drain: bool = True,
                     desired: str = "stopped") -> dict:
        """Stop one deployment; the caller already holds the deployment lock."""
        deployment_id = deployment["id"]
        root = self.deployment_root(deployment_id)
        root.mkdir(parents=True, exist_ok=True)
        if deployment["mode"] == "external":
            if drain and deployment.get("gateway_pid"):
                self._drain(deployment, timeout=DRAIN_TIMEOUT)
            if deployment.get("gateway_pid"):
                kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])
            (root / "runtime.json").unlink(missing_ok=True)
            self.services.update_deployment(deployment_id, desired_state=desired, gateway_pid=None,
                                            gateway_birth=None, endpoint=None, observed_state="stopped",
                                            observed_at=time.time(),
                                            observed_evidence=encoded({"kind": "external_route_stopped",
                                                                       "reason": reason}))
            self.services.health_event(deployment_id, "process", "external_access_stopped",
                                       {"reason": reason})
            return self._reload(deployment_id)
        if self.is_builtin(deployment):
            from .knowledge_migration import stop_builtin
            return stop_builtin(self, deployment, reason, desired)
        if drain and deployment.get("gateway_pid"):
            self._drain(deployment, timeout=DRAIN_TIMEOUT)
        if deployment.get("gateway_pid"):
            kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])
        (root / "runtime.json").unlink(missing_ok=True)
        self.services.update_deployment(deployment_id, desired_state=desired, gateway_pid=None,
                                        gateway_birth=None, observed_state="stopped", observed_at=time.time(),
                                        observed_evidence=encoded({"kind": "stopped", "reason": reason}))
        self.services.health_event(deployment_id, "process", "stopped", {"reason": reason})
        return self._reload(deployment_id)

    def _drain(self, deployment: dict, *, timeout: float):
        """Wait for in-flight business calls; never force-kill mid-call silently."""
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            health = self.gateway_health(deployment)
            if health is None or not health.get("gateway_alive"):
                return
            if not health.get("in_flight"):
                return
            time.sleep(0.2)
        # Report the conflict; the caller decides (stop proceeds, upgrade must not).
        self.services.health_event(deployment["id"], "process", "drain_timeout",
                                    {"in_flight": (self.gateway_health(deployment) or {}).get("in_flight")})

    def restart(self, deployment_id: str, *, reason: str, actor: str = "admin") -> dict:
        deployment = self._reload(deployment_id)
        active = self._active_install(deployment_id)
        if active:
            raise ValueError(f"部署正在安装/升级中（操作 {active['id']}，{active['status']}）；"
                             "不能在候选就绪前重启")
        if deployment["mode"] == "external":
            raise ValueError("外部服务不能由平台重启")
        if actor != "admin":
            manifest = self.services.revision(deployment["revision_id"])["manifest"] if deployment.get("revision_id") else {}
            if not manifest.get("operations", {}).get("restart_safe"):
                raise ValueError("服务未声明 restart_safe，拒绝自动重启")
            if not deployment["config"].get("allow_guardian_restart"):
                raise ValueError("部署未授权看护重启")
            budget = self.recovery_budget(deployment)
            if not budget["allowed"]:
                raise ValueError(f"恢复预算不足：{budget['reason']}")
            health = self.gateway_health(deployment)
            if health and health.get("in_flight"):
                raise ValueError("存在在途业务调用，不能安全排空；已记录故障，不强制重启")
        self.stop(deployment_id, reason=reason)
        return self.start(deployment_id, reason=reason)

    def recovery_budget(self, deployment: dict, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        policy = deployment["config"].get("recovery", {})
        max_attempts = policy.get("max_attempts", 3)
        window = policy.get("window_seconds", 600)
        backoff = policy.get("backoff", [30, 60, 120])
        attempts = deployment["recovery_attempts"]
        start = deployment["recovery_window_start"]
        if start and now - start > window:
            attempts, start = 0, None
        if attempts >= max_attempts:
            return {"allowed": False, "reason": "窗口内恢复次数已用尽", "attempts": attempts, "next_retry_at": deployment["next_retry_at"]}
        if deployment["next_retry_at"] and now < deployment["next_retry_at"]:
            return {"allowed": False, "reason": "退避等待中", "attempts": attempts, "next_retry_at": deployment["next_retry_at"]}
        return {"allowed": True, "attempts": attempts, "backoff": backoff, "window_seconds": window,
                "next_retry_at": now + backoff[min(attempts, len(backoff) - 1)]}

    def consume_recovery(self, deployment_id: str, next_retry_at: float):
        deployment = self._reload(deployment_id)
        start = deployment["recovery_window_start"] or time.time()
        self.services.update_deployment(deployment_id, recovery_attempts=deployment["recovery_attempts"] + 1,
                                        recovery_window_start=start, next_retry_at=next_retry_at)

    def _reload(self, deployment_id: str) -> dict:
        deployment = self.services.deployment(deployment_id)
        if not deployment:
            raise ValueError(f"部署不存在：{deployment_id}")
        return deployment

    def _runtime_state(self, deployment: dict) -> dict | None:
        root = self.deployment_root(deployment["id"])
        try:
            return json.loads((root / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- health / probing -----------------------------------------------------
    def gateway_health(self, deployment: dict) -> dict | None:
        if self.is_builtin(deployment):
            from .knowledge_migration import builtin_health
            return builtin_health(self.config, deployment)
        if not deployment.get("endpoint"):
            return None
        token_file = gateway_token_path(self.deployment_root(deployment["id"]))
        if not token_file.is_file():
            return None
        gateway_alive = alive(deployment.get("gateway_pid"), deployment.get("gateway_birth"))
        result = {"gateway_alive": gateway_alive, "deployment_id": deployment["id"],
                  "generation": deployment["generation"]}
        if not gateway_alive:
            return result
        try:
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(deployment["endpoint"].removesuffix("/mcp") + "/health",
                                      headers={"Authorization": "Bearer " + token_file.read_text().strip()})
                if response.status_code == 200:
                    result.update(response.json())
                    result["gateway_alive"] = result.get("pid") == deployment.get("gateway_pid")
        except (httpx.HTTPError, OSError, ValueError):
            pass
        return result

    def issue_probe_grant(self, deployment_id: str, seconds: float = 120) -> str:
        return self.services.issue_grant({"kind": "probe", "deployment_id": deployment_id,
                                          "pid": os.getpid(), "birth": identity(os.getpid())}, seconds)

    def validate(self, deployment_id: str, *, business: bool = True,
                 operation_id: str | None = None) -> dict:
        """Real MCP validation over the gateway endpoint; never a patch check."""
        deployment = self._reload(deployment_id)
        if self.is_builtin(deployment):
            from .knowledge_migration import validate_builtin
            return validate_builtin(self.config, deployment, business=business)
        if deployment["mode"] == "external":
            if deployment["desired_state"] != "running":
                return {"ok": False, "errors": ["部署不是期望运行状态"], "verification": "not_verified"}
            try:
                deployment = self.ensure_external_gateway(deployment_id, reason="external-validate")
            except ValueError as error:
                return {"ok": False, "errors": [str(error)[:300]], "verification": "not_verified"}
            expected = deployment["config"].get("expected_tools", [])
            token = self.issue_probe_grant(deployment_id)
            return asyncio.run(self._mcp_validate(deployment["endpoint"], token, expected, None))
        if not deployment.get("endpoint"):
            return {"ok": False, "errors": ["部署尚无活动端点"], "verification": "not_verified"}
        revision = self.services.revision(deployment["revision_id"]) if deployment.get("revision_id") else None
        manifest = revision["manifest"] if revision else {}
        expected = manifest.get("health", {}).get("expected_tools", [])
        token = self.issue_probe_grant(deployment_id)
        return asyncio.run(self._mcp_validate(deployment["endpoint"], token, expected, manifest if business else None))

    @staticmethod
    async def _mcp_validate(endpoint: str, token: str, expected_tools: list[str],
                            manifest: dict | None) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        errors, tools = [], []
        try:
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
                async with streamable_http_client(endpoint, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        async with asyncio.timeout(20):
                            await session.initialize()
                            cursor = None
                            while True:
                                listing = await session.list_tools(cursor=cursor)
                                tools.extend(tool.name for tool in listing.tools)
                                cursor = listing.nextCursor
                                if not cursor:
                                    break
        except Exception as error:
            return {"ok": False, "errors": [f"协议验证失败：{type(error).__name__}: {str(error)[:300]}"],
                    "verification": "not_verified", "tools": []}
        missing = [name for name in expected_tools if name not in tools]
        if missing:
            errors.append(f"缺少声明的工具：{missing}")
        verification = "protocol_verified"
        if manifest is not None and not errors:
            probe = manifest.get("health", {}).get("probe_tool")
            if probe:
                try:
                    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
                        async with streamable_http_client(endpoint, http_client=client) as streams:
                            async with ClientSession(streams[0], streams[1]) as session:
                                async with asyncio.timeout(20):
                                    await session.initialize()
                                    answer = await session.call_tool(probe["tool"], probe.get("arguments", {}))
                    if answer.isError:
                        errors.append(f"业务探针失败：{answer.content[0].text if answer.content else 'unknown'}")
                    else:
                        verification = "business_verified"
                except Exception as error:
                    errors.append(f"业务探针异常：{str(error)[:300]}")
        return {"ok": not errors, "errors": errors, "verification": verification if not errors else "not_verified",
                "tools": sorted(set(tools))}

    # -- crash recovery -------------------------------------------------------
    def recover_operations(self):
        """Reconcile operations whose executor died; unknown steps need input."""
        for operation in self.services.operations(limit=50):
            if operation["status"] not in OPERATION_ACTIVE:
                continue
            if operation.get("pid") and alive(operation["pid"], operation["birth"]):
                continue
            progress = operation["plan"].get("progress", {})
            previous = operation["plan"].get("previous")
            if operation["kind"] == "install" and previous:
                # The executor died during an upgrade. Restore the recorded
                # active generation when it is still serving instead of leaving
                # the candidate revision as the recorded active one.
                self._recover_failed_install(operation["deployment_id"], operation["plan"], previous)
                self.services.update_operation(operation["id"], status="needs_input",
                                               error="执行器在升级过程中中断；已核对并恢复活动代际，"
                                                     "候选版本需显式重试或放弃")
                continue
            if operation["status"] == "installing" and progress.get("in_flight_step") is not None:
                self.services.update_operation(operation["id"], status="needs_input",
                                               error=f"执行器中断于安装步骤 {progress['in_flight_step']}，结果未知；"
                                                     "请核对日志后显式重试或放弃，不会自动重跑")
            elif operation["status"] == "installing" and not progress.get("dependencies_installed") \
                    and not progress.get("revision_ready"):
                self.services.update_operation(operation["id"], status="failed", error="执行器在准备阶段中断；可安全重新发起")
            else:
                self.services.update_operation(operation["id"], status="needs_input",
                                               error="执行器中断；请核对部署状态后显式重试或放弃")

    def read_log(self, deployment_id: str, name: str, *, tail_bytes: int = 16000) -> dict:
        deployment = self.services.deployment(deployment_id)
        if self.is_builtin(deployment):
            if name not in {"service.log", "service.stderr.log"}:
                raise ValueError("内置知识库只允许读取 service.log")
            file = Path(deployment["config"]["data_root"]) / name
            if not file.is_file() or file.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("日志不存在或超过读取上限")
            with file.open("rb") as stream:
                stream.seek(max(0, file.stat().st_size - tail_bytes))
                text = stream.read().decode("utf-8", errors="replace")
            import re
            text = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1<redacted>", text)
            return {"deployment_id": deployment_id, "name": name, "text": text, "tail": True}
        if name not in {"gateway.log", "dependencies.log", "dependencies.stderr.log"} \
                and not name.startswith("install-"):
            raise ValueError("仅允许读取部署日志目录中的已知日志")
        logs = self.deployment_root(deployment_id) / "logs"
        file = within(logs, name)
        if not file.is_file() or file.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("日志不存在或超过读取上限")
        with file.open("rb") as stream:
            stream.seek(max(0, file.stat().st_size - tail_bytes))
            text = stream.read().decode("utf-8", errors="replace")
        import re
        text = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1<redacted>", text)
        return {"deployment_id": deployment_id, "name": name, "text": text, "tail": True}
