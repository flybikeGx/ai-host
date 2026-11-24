"""The build-time frontend registry stays in sync with package manifests."""
import os
import subprocess
import sys

from agent_base.config import PROJECT
from agent_base.mcp_packages import discover, load_package


def test_info_board_manifest_matches_the_frozen_contract():
    package = load_package(PROJECT / "first_party/info_board")
    assert package.id == "info-board"
    assert package.route == "/info" and package.mount == "spa-module"
    assert package.default_installation == "available"
    assert package.data_dir == "info-board"
    assert package.manifest["service"]["health"]["expected_tools"] == [
        "info_create", "info_list", "info_get", "info_update", "info_delete"]
    assert package.manifest["frontend"]["requires"] == ["info.read"]
    assert package.manifest["permissions"]["anonymous"] is False


def test_discovery_includes_info_board_once():
    packages = discover(PROJECT / "first_party")
    assert packages["info-board"].id == "info-board"
    routes = [package.route for package in packages.values() if package.route]
    assert len(routes) == len(set(routes))


def test_generated_frontend_registry_is_current():
    result = subprocess.run([sys.executable, str(PROJECT / "scripts/build_mcp_registry.py"), "--check"],
                            cwd=PROJECT, capture_output=True, text=True,
                            env={**os.environ,
                                 "PYTHONPATH": str(PROJECT / "src") + ":" + str(PROJECT)})
    assert result.returncode == 0, result.stdout + result.stderr
    generated = (PROJECT / "frontend/src/mcp-registry.generated.js").read_text()
    assert "first_party/info_board/frontend/src/index.jsx" in generated
    assert "'/info'" in generated or '"/info"' in generated
