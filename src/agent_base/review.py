"""Read-only candidate review: pin a commit, read install documents, install that commit."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .package import within
from .processes import run_process

REVIEW_DOCUMENTS = ("AGENTS.md", "Installation.md", "installation.md", "entrypoint.md")
MAX_DOCUMENT_BYTES = 128 * 1024
CANDIDATE_TTL_SECONDS = 24 * 3600


def _document_status(checkout: Path) -> dict:
    status = {}
    for name in REVIEW_DOCUMENTS:
        file = checkout / name
        status[name] = {"present": file.is_file(), **({"bytes": file.stat().st_size} if file.is_file() else {})}
    return status


def _component_root(checkout: Path, component: str | None) -> Path:
    """Explicit component subdirectory; must stay inside the pinned checkout."""
    if component is None:
        return checkout
    if not isinstance(component, str) or not component.strip() or component.startswith("/") or ".." in Path(component).parts:
        raise ValueError(f"组件路径必须是包内相对目录：{component!r}")
    root = within(checkout, component)
    if not root.is_dir():
        raise ValueError(f"组件目录不存在：{component}")
    return root


def _component_summary(root: Path) -> dict:
    """Read-only manifest peek: kind and service identity, never executes code."""
    from .manifests import load_service_manifest
    try:
        manifest = load_service_manifest(root)
    except ValueError as error:
        return {"kind": "mcp-service", "valid": False, "error": str(error)}
    service = manifest.get("service", {})
    return {"kind": "mcp-service", "valid": True, "service_id": service.get("id"),
            "version": service.get("version"), "contract": service.get("contract"),
            "transport": manifest.get("runtime", {}).get("transport")}


def _read_document(source: Path, document: str) -> str:
    if document not in REVIEW_DOCUMENTS:
        raise ValueError(f"只允许读取审阅文档：{', '.join(REVIEW_DOCUMENTS)}")
    file = within(source, document)
    if not file.is_file():
        raise ValueError(f"文档不存在：{document}")
    if file.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError(f"文档超过 {MAX_DOCUMENT_BYTES // 1024} KiB 读取上限：{document}")
    return file.read_text(encoding="utf-8")


class Reviews:
    def __init__(self, config):
        self.config = config
        self.root = config.data / "review"

    def _root(self, candidate_id: str) -> Path:
        if not isinstance(candidate_id, str) or not re.fullmatch(r"[0-9a-f]{16}", candidate_id):
            raise ValueError(f"无效候选 ID：{candidate_id!r}")
        return self.root / candidate_id

    def candidate(self, candidate_id: str) -> dict:
        root = self._root(candidate_id)
        meta = root / "meta.json"
        if not meta.is_file():
            raise ValueError(f"候选不存在或已清理：{candidate_id}")
        item = json.loads(meta.read_text(encoding="utf-8"))
        item["root"] = str(root)
        return item

    def prepare(self, origin: str, ref: str | None = None, *, component: str | None = None,
                deadline: float | None = None, cancelled=lambda: False) -> dict:
        if not isinstance(origin, str) or not origin.strip() or origin.startswith("-"):
            raise ValueError("无效仓库")
        if origin.startswith(("http://", "https://")) and urlparse(origin).username:
            raise ValueError("仓库 URL 不得包含凭据，请使用 Git credential helper")
        if ref and (ref.startswith("-") or any(c in ref for c in "\0\n")):
            raise ValueError("无效 ref")
        origin = str(Path(origin).expanduser().resolve()) if Path(origin).expanduser().exists() else origin
        self.cleanup()
        candidate_id = secrets.token_hex(8)
        root = self.root / candidate_id
        checkout = root / "checkout"
        root.mkdir(parents=True, mode=0o700)
        deadline = deadline or time.time() + 600
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        # Keep the directory on failure so clone logs stay inspectable; cleanup()
        # removes stale meta-less candidates later.
        code = run_process(["git", "-c", "core.hooksPath=" + os.devnull, "clone", "--no-local", "--no-checkout", "--", origin, str(checkout)],
                           cwd=root, env=env, output=root / "clone.log", error=root / "clone.stderr.log",
                           deadline=deadline, cancelled=cancelled)
        if code:
            raise ValueError(f"clone 失败，见 {root / 'clone.stderr.log'}")
        try:
            result = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--verify", "--end-of-options", (ref or "HEAD") + "^{commit}"],
                                    capture_output=True, text=True, timeout=max(0.1, deadline - time.time()))
            if result.returncode:
                raise ValueError("无法解析仓库 commit")
            commit = result.stdout.strip()
            subprocess.run(["git", "-C", str(checkout), "-c", "core.hooksPath=" + os.devnull, "checkout", "--detach", commit],
                           check=True, capture_output=True, timeout=max(0.1, deadline - time.time()))
            component_root = _component_root(checkout, component)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        summary = None
        if component is not None:
            summary = _component_summary(component_root)
        meta = {"id": candidate_id, "origin": origin, "ref": ref, "commit": commit,
                "component": component, "component_summary": summary,
                "created": time.time(), "documents": _document_status(component_root)}
        (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {**meta, "root": str(root)}

    def read(self, *, candidate_id: str | None = None, installation: dict | None = None, document: str) -> dict:
        if bool(candidate_id) == bool(installation):
            raise ValueError("需要 candidate_id 或 installation_id 之一")
        if candidate_id:
            item = self.candidate(candidate_id)
            base = Path(item["root"]) / "checkout"
            base = _component_root(base, item.get("component"))
            if document == "runbook":
                text = self._read_runbook(base, item)
            else:
                text = _read_document(base, document)
            return {"phase": "candidate", "candidate_id": item["id"], "commit": item["commit"],
                    "origin": item["origin"], "component": item.get("component"), "document": document, "text": text}
        text = _read_document(Path(installation["root"]) / "source", document)
        return {"phase": "installed", "installation_id": installation["id"], "commit": installation["commit_sha"],
                "document": document, "text": text}

    def _read_runbook(self, base: Path, item: dict) -> str:
        from .manifests import config_block, runbook_text
        if item.get("component_summary", {}).get("kind") != "mcp-service":
            raise ValueError("只有 mcp-service 组件声明 runbook")
        try:
            manifest, _ = config_block(base)
        except ValueError as error:
            raise ValueError(f"无法读取服务清单：{error}")
        text = runbook_text(base, manifest)
        if text is None:
            raise ValueError("该服务清单未声明 operations.runbook")
        return text

    def consume(self, candidate_id: str):
        root = self._root(candidate_id)
        if not (root / "meta.json").is_file():
            raise ValueError(f"候选不存在或已清理：{candidate_id}")
        shutil.rmtree(root, ignore_errors=True)

    def cleanup(self):
        if not self.root.is_dir():
            return
        now = time.time()
        for child in self.root.iterdir():
            meta = child / "meta.json"
            item = None
            if meta.is_file():
                try:
                    item = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    item = None
            if isinstance(item, dict) and isinstance(item.get("created"), (int, float)):
                expired = now - item["created"] > CANDIDATE_TTL_SECONDS
            else:
                # Meta-less directories may belong to a prepare still in flight;
                # only retire them once they have clearly gone stale.
                try:
                    expired = now - child.stat().st_mtime > 3600
                except OSError:
                    continue
            if expired:
                shutil.rmtree(child, ignore_errors=True)
