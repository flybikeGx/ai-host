import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from agent_base.review import MAX_DOCUMENT_BYTES, Reviews


def commit(repo, message="fixture"):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", message], check=True)


def head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def test_prepare_pins_commit_and_reports_documents(config, resource_repos):
    repo = resource_repos["finance"]
    reviews = Reviews(config)
    candidate = reviews.prepare(str(repo))
    assert candidate["commit"] == head(repo)
    assert candidate["documents"]["AGENTS.md"]["present"] is True
    assert candidate["documents"]["Installation.md"]["present"] is True
    assert candidate["documents"]["entrypoint.md"]["present"] is True
    # On case-insensitive filesystems installation.md resolves to Installation.md;
    # the status reports the path as the filesystem answers it.
    assert candidate["documents"]["installation.md"]["present"] == (repo / "installation.md").is_file()
    assert (Path(candidate["root"]) / "checkout" / "AGENTS.md").is_file()


def test_read_candidate_document_and_missing_errors(config, resource_repos):
    repo = resource_repos["finance"]
    (repo / "entrypoint.md").unlink()
    commit(repo, "drop entrypoint")
    reviews = Reviews(config)
    candidate = reviews.prepare(str(repo))
    answer = reviews.read(candidate_id=candidate["id"], document="AGENTS.md")
    assert answer["phase"] == "candidate" and answer["commit"] == candidate["commit"]
    assert "finance" in answer["text"]
    with pytest.raises(ValueError, match="文件不存在"):
        reviews.read(candidate_id=candidate["id"], document="entrypoint.md")
    with pytest.raises(ValueError, match="只允许读取"):
        reviews.read(candidate_id=candidate["id"], document="agent.yaml")
    with pytest.raises(ValueError, match="候选不存在"):
        reviews.read(candidate_id="0" * 16, document="AGENTS.md")
    with pytest.raises(ValueError, match="之一"):
        reviews.read(document="AGENTS.md")
    with pytest.raises(ValueError, match="无效候选 ID"):
        reviews.candidate("../escape")


def test_read_oversize_document_fails_clearly(config, resource_repos):
    repo = resource_repos["finance"]
    (repo / "AGENTS.md").write_text("x" * (MAX_DOCUMENT_BYTES + 1))
    commit(repo, "oversize")
    reviews = Reviews(config)
    candidate = reviews.prepare(str(repo))
    assert candidate["documents"]["AGENTS.md"]["bytes"] > MAX_DOCUMENT_BYTES
    with pytest.raises(ValueError, match="读取上限"):
        reviews.read(candidate_id=candidate["id"], document="AGENTS.md")


def test_read_rejects_escaping_symlink(config, resource_repos, tmp_path):
    repo = resource_repos["finance"]
    (tmp_path / "outside.md").write_text("outside")
    (repo / "entrypoint.md").unlink()
    (repo / "entrypoint.md").symlink_to(tmp_path / "outside.md")
    commit(repo, "escape")
    reviews = Reviews(config)
    candidate = reviews.prepare(str(repo))
    with pytest.raises(ValueError, match="越界"):
        reviews.read(candidate_id=candidate["id"], document="entrypoint.md")


def test_install_reuses_pinned_commit_after_branch_moves(config, repositories, resource_repos):
    repo = resource_repos["finance"]
    reviews = Reviews(config)
    candidate = reviews.prepare(str(repo))
    pinned = candidate["commit"]
    (repo / "AGENTS.md").write_text("AFTER REVIEW BRANCH MOVED")
    commit(repo, "move branch")
    assert head(repo) != pinned
    item = repositories.install(candidate["origin"], candidate["ref"], checkout=Path(candidate["root"]) / "checkout",
                                commit=candidate["commit"])
    assert item["commit_sha"] == pinned
    assert "AFTER REVIEW BRANCH MOVED" not in (Path(item["root"]) / "source" / "AGENTS.md").read_text()
    # Direct installs bypass the candidate flow; consumption is verified in the admin wiring test.
    assert Path(candidate["root"]).exists()


def test_admin_call_candidate_flow_matches_wiring(config, resource_repos):
    from agent_base.mcp_server import admin_call
    from agent_base.service import Service
    repo = resource_repos["finance"]
    service = Service(config)
    candidate = admin_call(service, "prepare_repo_review", {"origin": str(repo)})
    assert candidate["documents"]["AGENTS.md"]["present"] is True
    document = admin_call(service, "read_repo_review_doc", {"candidate_id": candidate["id"], "document": "AGENTS.md"})
    assert document["phase"] == "candidate" and document["text"].strip()
    with pytest.raises(ValueError):
        admin_call(service, "read_repo_review_doc", {"document": "AGENTS.md"})
    with pytest.raises(ValueError):
        admin_call(service, "read_repo_review_doc", {"candidate_id": candidate["id"], "installation_id": "x", "document": "AGENTS.md"})
    item = admin_call(service, "install_repo_resources", {"candidate_id": candidate["id"]})
    assert item["commit_sha"] == candidate["commit"]
    reread = admin_call(service, "read_repo_review_doc", {"installation_id": item["id"], "document": "AGENTS.md"})
    assert reread["phase"] == "installed" and reread["commit"] == candidate["commit"]
    assert reread["text"].strip()
    with pytest.raises(ValueError, match="候选不存在或已清理"):
        Reviews(config).candidate(candidate["id"])
    fresh = Reviews(config).prepare(str(repo))
    with pytest.raises(ValueError, match="origin 与审阅候选不一致"):
        admin_call(service, "install_repo_resources", {"candidate_id": fresh["id"], "origin": "https://example.invalid/other.git"})
    with pytest.raises(ValueError, match="ref 与审阅候选不一致"):
        admin_call(service, "install_repo_resources", {"candidate_id": fresh["id"], "ref": "other"})
    with pytest.raises(ValueError, match="需要 origin 或 candidate_id"):
        admin_call(service, "install_repo_resources", {})


def test_post_install_reread_reflects_install_steps(config, resource_repos):
    repo = resource_repos["finance"]
    manifest = {"schema_version": 2, "installation": {"steps": [
        {"argv": ["${runtime.python}", "-c", "from pathlib import Path; Path('AGENTS.md').write_text('STEP_REWROTE_AGENTS', encoding='utf-8')"]}]}}
    (repo / "Installation.md").write_text("# init\n\n```agent-base-install\n" + yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False) + "```\n")
    commit(repo, "steps rewrite docs")
    from agent_base.mcp_server import admin_call
    from agent_base.service import Service
    service = Service(config)
    candidate = admin_call(service, "prepare_repo_review", {"origin": str(repo)})
    before = admin_call(service, "read_repo_review_doc", {"candidate_id": candidate["id"], "document": "Installation.md"})
    assert "agent-base-install" in before["text"]
    item = admin_call(service, "install_repo_resources", {"candidate_id": candidate["id"]})
    after = admin_call(service, "read_repo_review_doc", {"installation_id": item["id"], "document": "AGENTS.md"})
    assert after["phase"] == "installed" and after["text"].strip() == "STEP_REWROTE_AGENTS"


def test_expired_candidates_are_cleaned_up(config, resource_repos):
    reviews = Reviews(config)
    candidate = reviews.prepare(str(resource_repos["finance"]))
    meta_file = Path(candidate["root"]) / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["created"] = time.time() - 25 * 3600
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    fresh = reviews.prepare(str(resource_repos["finance"]))
    assert not Path(candidate["root"]).exists()
    with pytest.raises(ValueError, match="候选不存在或已清理"):
        reviews.candidate(candidate["id"])
    assert Path(fresh["root"]).exists()


def test_prepare_rejects_bad_origin_and_ref(config):
    reviews = Reviews(config)
    with pytest.raises(ValueError, match="凭据"):
        reviews.prepare("https://user:token@example.invalid/repo.git")
    with pytest.raises(ValueError, match="无效 ref"):
        reviews.prepare(str(Path(__file__)), "-ooutput")
    with pytest.raises(ValueError, match="无效仓库"):
        reviews.prepare("-")
