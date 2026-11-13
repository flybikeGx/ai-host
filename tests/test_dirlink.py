import os
import subprocess
from pathlib import Path

import pytest

from agent_base import dirlink
from agent_base.dirlink import (create_directory_link, is_junction, is_link, is_privilege_error,
                                junction_argv, remove_directory_link, remove_tree)


def test_symlink_create_and_remove_roundtrip_keeps_target(tmp_path):
    target = tmp_path / "目标 目录"
    target.mkdir()
    (target / "keep.txt").write_text("data", encoding="utf-8")
    link = tmp_path / "mount point"
    create_directory_link(link, target)
    assert link.is_symlink() and (link / "keep.txt").is_file()
    assert is_link(link)
    remove_directory_link(link)
    assert not link.exists()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "data"


def test_create_rejects_existing_link_path_real_dir_and_bad_targets(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    taken = tmp_path / "taken"
    create_directory_link(taken, target)
    with pytest.raises(ValueError, match="链接位置已存在"):
        create_directory_link(taken, target)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    with pytest.raises(ValueError, match="链接位置已存在"):
        create_directory_link(real_dir, target)
    plain_file = tmp_path / "file.txt"
    plain_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="必须是真实目录"):
        create_directory_link(tmp_path / "l1", plain_file)
    with pytest.raises(ValueError, match="必须是真实目录"):
        create_directory_link(tmp_path / "l2", tmp_path / "missing")
    chained = tmp_path / "chained"
    create_directory_link(chained, target)
    with pytest.raises(ValueError, match="必须是真实目录"):
        create_directory_link(tmp_path / "l3", chained)


def test_remove_distinguishes_real_directory_and_missing(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    with pytest.raises(ValueError, match="真实目录"):
        remove_directory_link(real_dir)
    with pytest.raises(ValueError, match="链接不存在"):
        remove_directory_link(tmp_path / "missing")
    remove_directory_link(tmp_path / "missing", missing_ok=True)


def test_remove_tree_never_follows_directory_links(tmp_path):
    shared = tmp_path / "shared"
    (shared / "nested").mkdir(parents=True)
    (shared / "nested" / "重要 文件.txt").write_text("keep", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "repo" / ".agent").mkdir(parents=True)
    create_directory_link(workspace / "repo" / ".agent" / "skills", shared)
    (workspace / "repo" / "task.txt").write_text("x", encoding="utf-8")
    remove_tree(workspace)
    assert not workspace.exists()
    assert (shared / "nested" / "重要 文件.txt").read_text(encoding="utf-8") == "keep"
    link_root = tmp_path / "link-root"
    create_directory_link(link_root, shared)
    remove_tree(link_root)
    assert not link_root.exists() and shared.is_dir()


def test_is_privilege_error_only_matches_windows_privilege_denial():
    privilege = OSError(1314, "A required privilege is not held by the client")
    privilege.winerror = 1314
    assert is_privilege_error(privilege)
    access_denied = OSError(5, "Access is denied")
    access_denied.winerror = 5
    assert not is_privilege_error(access_denied)
    assert not is_privilege_error(OSError(13, "Permission denied"))
    assert not is_privilege_error(ValueError("x"))


def test_junction_argv_uses_fixed_structure_with_separate_arguments(tmp_path):
    link = tmp_path / "有空格 的链接"
    target = tmp_path / "中文 目标/子目录"
    argv = junction_argv(link, target)
    assert argv == ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)]
    assert all("&&" not in item and "|" not in item for item in argv[:5])


def test_junction_target_validation_rejects_relative_file_and_unc(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    plain_file = tmp_path / "file.txt"
    plain_file.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    create_directory_link(link, real_dir)
    with pytest.raises(ValueError, match="绝对路径"):
        dirlink._validate_junction_target(Path("relative/dir"))
    with pytest.raises(ValueError, match="必须是真实本地目录"):
        dirlink._validate_junction_target(plain_file)
    with pytest.raises(ValueError, match="必须是真实本地目录"):
        dirlink._validate_junction_target(link)
    with pytest.raises(ValueError):
        dirlink._validate_junction_target(Path("\\\\server\\share\\dir"))


def test_windows_privilege_error_falls_back_to_junction(tmp_path, monkeypatch):
    target = tmp_path / "junction 目标"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "mount 链接"
    real_symlink = os.symlink

    def denied(*args, **kwargs):
        error = OSError(1314, "A required privilege is not held by the client")
        error.winerror = 1314
        raise error

    monkeypatch.setattr(os, "symlink", denied)
    argv_seen = []

    def fake_mklink(argv, **kwargs):
        argv_seen.append(argv)
        real_symlink(argv[6], argv[5])  # simulate what mklink /J produces
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dirlink.subprocess, "run", fake_mklink)
    # On POSIX the junction detector is a no-op; emulate the reparse point so
    # the post-creation verification runs exactly as it would on Windows.
    monkeypatch.setattr(dirlink, "is_junction", lambda path: Path(path).is_symlink())
    dirlink._create_windows(link, target)
    assert argv_seen == [["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)]]
    assert (link / "keep.txt").is_file()


def test_windows_junction_failure_is_reported_not_masked(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"

    def denied(*args, **kwargs):
        error = OSError(1314, "A required privilege is not held by the client")
        error.winerror = 1314
        raise error

    monkeypatch.setattr(os, "symlink", denied)

    def failing_mklink(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "You do not have sufficient privilege")

    monkeypatch.setattr(dirlink.subprocess, "run", failing_mklink)
    with pytest.raises(ValueError, match="mklink /J 失败"):
        dirlink._create_windows(link, target)


def test_windows_other_symlink_errors_do_not_fall_back(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"

    def denied(*args, **kwargs):
        error = OSError(5, "Access is denied")
        error.winerror = 5
        raise error

    monkeypatch.setattr(os, "symlink", denied)

    def must_not_run(argv, **kwargs):
        raise AssertionError("mklink must not be attempted for non-privilege errors")

    monkeypatch.setattr(dirlink.subprocess, "run", must_not_run)
    with pytest.raises(ValueError, match="创建目录符号链接失败"):
        dirlink._create_windows(link, target)


def test_junction_verification_rejects_wrong_target(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    link = tmp_path / "link"

    def fake_mklink(argv, **kwargs):
        Path(argv[5]).symlink_to(wrong)  # interpreter mangled the argument
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dirlink.subprocess, "run", fake_mklink)
    monkeypatch.setattr(dirlink, "is_junction", lambda path: Path(path).is_symlink())
    with pytest.raises(ValueError, match="指向与预期不符"):
        dirlink._create_junction(link, target)


def test_junction_detection_helper_is_windows_only(tmp_path):
    # A real directory is never a junction on any platform.
    assert is_junction(tmp_path) is False
    assert is_link(tmp_path) is False
