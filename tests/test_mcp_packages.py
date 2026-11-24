"""First-party MCP package manifests, loaders and installation state (D1)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_base.mcp_packages import (Installation, Installations, PackageError, PackageRegistry,
                                     discover, expand_install_step, expand_runtime, load_package,
                                     skipped)

PROJECT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT / "first_party" / "_template"


def copy_template(target: Path, **changes) -> dict:
    """Copy the template package, then apply top-level manifest changes."""
    shutil.copytree(TEMPLATE, target)
    manifest = yaml.safe_load((target / "mcp.yaml").read_text(encoding="utf-8"))
    for key, value in changes.items():
        manifest[key] = value
    (target / "mcp.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                                     encoding="utf-8")
    return manifest


def write_manifest(root: Path, manifest: dict) -> None:
    (root / "mcp.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")


def test_template_package_is_valid():
    package = load_package(TEMPLATE)
    assert package.id == "example-package"
    assert package.version == "0.1.0"
    assert package.contract == "example-package/v1"
    assert package.mount == "spa-module"
    assert package.route == "/example"
    assert package.data_dir == "example-package"
    assert package.default_installation == "available"
    assert package.navigation() == {"id": "example-package", "route": "/example",
                                    "label": "示例包", "icon": "box", "order": 90}
    assert package.manifest["service"]["health"]["expected_tools"] == ["echo"]


def test_discover_skips_underscore_and_placeholder_directories(tmp_path):
    shutil.copytree(TEMPLATE, tmp_path / "example_package")
    shutil.copytree(TEMPLATE, tmp_path / "_ignored")
    (tmp_path / "knowledge").mkdir()
    packages = discover(tmp_path)
    assert list(packages) == ["example-package"]
    assert skipped(tmp_path) == ["knowledge"]


def test_discover_rejects_directory_id_mismatch(tmp_path):
    shutil.copytree(TEMPLATE, tmp_path / "wrong_dir")
    with pytest.raises(PackageError, match="包目录名"):
        discover(tmp_path)


def test_discover_rejects_duplicate_routes(tmp_path):
    shutil.copytree(TEMPLATE, tmp_path / "example_package")
    second = copy_template(tmp_path / "other_package")
    second["id"] = "other-package"
    write_manifest(tmp_path / "other_package", second)
    with pytest.raises(PackageError, match="frontend.route 重复"):
        discover(tmp_path)


def test_rejects_unknown_fields(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root, unexpected=True)
    with pytest.raises(PackageError, match="未知字段"):
        load_package(root)


def test_rejects_reserved_route(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root)
    manifest["frontend"]["route"] = "/services"
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="保留路由"):
        load_package(root)


def test_rejects_bad_schema_and_kind(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root, schema_version=2)
    with pytest.raises(PackageError, match="schema_version"):
        load_package(root)
    manifest["schema_version"] = 1
    manifest["kind"] = "agent"
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="kind"):
        load_package(root)


def test_rejects_unknown_placeholder(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root)
    manifest["service"]["runtime"]["argv"].append("${unknown.value}")
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="不支持的占位符"):
        load_package(root)


def test_rejects_entry_outside_package(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root)
    manifest["frontend"]["entry"] = "../../outside.jsx"
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="frontend.entry"):
        load_package(root)


def test_rejects_missing_static_assets_and_wrong_session(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root)
    manifest["frontend"] = {"mount": "static-iframe", "route": "/example",
                            "navigation": {"label": "示例包", "icon": "box"},
                            "requires": ["example.read"], "standalone": True}
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="frontend.assets"):
        load_package(root)
    manifest["frontend"]["assets"] = "frontend/dist"
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="capability-token"):
        load_package(root)


def test_static_iframe_package_uses_capability_session(tmp_path):
    root = tmp_path / "packages"
    root.mkdir()
    package_root = root / "example_package"
    manifest = copy_template(package_root)
    manifest["installation"]["default"] = "installed"
    manifest["frontend"] = {"mount": "static-iframe", "route": "/apps/example",
                            "navigation": {"label": "示例包", "icon": "box"},
                            "assets": "frontend/dist", "requires": ["example.read"],
                            "standalone": True}
    manifest["permissions"]["session"] = "capability-token"
    write_manifest(package_root, manifest)
    package = load_package(package_root)
    assert package.mount == "static-iframe"
    registry = PackageRegistry(root, tmp_path / "state")
    assert registry.navigation() == []
    assert registry.frontend_modules() == []
    assert registry.static_mounts() == [{"id": "example-package", "route": "/apps/example",
                                         "assets": "frontend/dist", "requires": ["example.read"]}]


def test_rejects_expected_tool_not_declared(tmp_path):
    root = tmp_path / "example_package"
    manifest = copy_template(root)
    manifest["permissions"]["service_tools"] = []
    write_manifest(root, manifest)
    with pytest.raises(PackageError, match="service_tools"):
        load_package(root)


def test_expand_runtime_and_install_step():
    package = load_package(TEMPLATE)
    argv = expand_runtime(package, python="/usr/bin/python", config_path=Path("/state/config.toml"),
                          data_dir=Path("/state/example-package"), port=8123)
    assert argv == ["/usr/bin/python", str(package.root / "server.py"), "--port", "8123",
                    "--data", "/state/example-package"]
    with pytest.raises(PackageError, match="占位符"):
        expand_runtime(package, python="/usr/bin/python", config_path=Path("/c"),
                       data_dir=Path("/d"))
    step = {"argv": ["${runtime.python}", "${package.root}/prepare.py", "${service.data}"]}
    assert expand_install_step(package, step, python="/py", config_path=Path("/c"),
                               data_dir=Path("/data")) == ["/py", str(package.root / "prepare.py"), "/data"]


def installed_template(tmp_path):
    root = tmp_path / "packages"
    root.mkdir()
    package_root = root / "example_package"
    manifest = copy_template(package_root)
    manifest["installation"]["default"] = "installed"
    write_manifest(package_root, manifest)
    return root, package_root


def test_default_state_and_lifecycle(tmp_path):
    root, package_root = installed_template(tmp_path)
    registry = PackageRegistry(root, tmp_path / "state" / "mcp")
    package = registry.packages["example-package"]
    assert registry.status(package)["installed"] is True
    assert registry.status(package)["enabled"] is True
    assert registry.status(package)["source"] == "default"
    assert [item["id"] for item in registry.navigation()] == ["example-package"]
    assert registry.frontend_modules()[0]["entry"] == "frontend/src/index.jsx"

    data = tmp_path / "state" / "example-package"
    data.mkdir(parents=True)
    (data / "keep.txt").write_text("keep", encoding="utf-8")

    disabled = registry.installations.disable(package)
    assert disabled.installed and not disabled.enabled
    assert registry.navigation() == []
    assert registry.frontend_modules() == []
    assert (data / "keep.txt").read_text(encoding="utf-8") == "keep"

    enabled = registry.installations.enable(package)
    assert enabled.installed and enabled.enabled
    assert [item["id"] for item in registry.navigation()] == ["example-package"]

    removed = registry.installations.uninstall(package)
    assert not removed.installed and not removed.enabled
    state = json.loads((tmp_path / "state" / "mcp" / "installations.json").read_text(encoding="utf-8"))
    assert state["revision"] == 3
    with pytest.raises(PackageError, match="尚未安装"):
        registry.installations.enable(package)


def test_install_is_idempotent_and_stale_detection(tmp_path):
    root, package_root = installed_template(tmp_path)
    registry = PackageRegistry(root, tmp_path / "state" / "mcp")
    package = registry.packages["example-package"]
    registry.installations.disable(package)
    first = registry.installations.install(package)
    second = registry.installations.install(package)
    assert first.installed and first.enabled and second.installed and second.enabled

    manifest = yaml.safe_load((package_root / "mcp.yaml").read_text(encoding="utf-8"))
    manifest["version"] = "0.2.0"
    write_manifest(package_root, manifest)
    reloaded = PackageRegistry(root, tmp_path / "state" / "mcp")
    stale = reloaded.installation(reloaded.packages["example-package"])
    assert stale.stale is True
    assert stale.installed is True


def test_corrupt_state_falls_back_to_defaults(tmp_path):
    root, _ = installed_template(tmp_path)
    state = tmp_path / "state" / "mcp"
    state.mkdir(parents=True)
    (state / "installations.json").write_text("not json", encoding="utf-8")
    registry = PackageRegistry(root, state)
    package = registry.packages["example-package"]
    assert registry.status(package)["installed"] is True
    assert registry.status(package)["source"] == "default"


def test_state_rejects_enabling_uninstalled_package(tmp_path):
    root, _ = installed_template(tmp_path)
    installations = Installations(tmp_path / "state")
    package = discover(root)["example-package"]
    installations.uninstall(package)
    with pytest.raises(PackageError, match="未安装"):
        installations.set(package, enabled=True)


def test_cli_validate_and_packages(tmp_path, config):
    environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}
    validate = subprocess.run([sys.executable, "-m", "agent_base", "--config", str(config.path),
                               "mcp", "validate", str(TEMPLATE)],
                              capture_output=True, text=True, env=environment, timeout=60)
    assert validate.returncode == 0, validate.stderr
    payload = json.loads(validate.stdout)
    assert payload["ok"] is True
    assert payload["packages"][0]["id"] == "example-package"

    packages = subprocess.run([sys.executable, "-m", "agent_base", "--config", str(config.path),
                               "mcp", "packages"],
                              capture_output=True, text=True, env=environment, timeout=60)
    assert packages.returncode == 0, packages.stderr
    summary = json.loads(packages.stdout)
    assert summary["count"] == 0
    assert summary["skipped"] == ["file_hub", "knowledge"]
