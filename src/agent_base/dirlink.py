"""Cross-platform directory links: symlink first, Windows junction fallback.

Junctions are only used for the base's own directory mounts (installed skills,
workspace skill mounts, run package mounts). Repo-content links stay symlinks
and fail loudly when the platform cannot create them.
"""
from __future__ import annotations

import os
import stat as stat_module
import subprocess
from pathlib import Path

# ERROR_PRIVILEGE_NOT_HELD: the documented failure when a Windows user without
# the symlink privilege (or Developer Mode) creates a symbolic link.
PRIVILEGE_WINERRORS = {1314}
JUNCTION_TIMEOUT_SECONDS = 30


def is_junction(path) -> bool:
    """Detect directory junctions (reparse mount points), not just symlinks."""
    if os.name != "nt":
        return False
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not getattr(info, "st_file_attributes", 0) & stat_module.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return getattr(info, "st_reparse_tag", None) == stat_module.IO_REPARSE_TAG_MOUNT_POINT


def is_link(path) -> bool:
    """True for symlinks and junctions; false for real files and directories."""
    path = Path(path)
    return path.is_symlink() or is_junction(path)


def is_privilege_error(error: BaseException) -> bool:
    return getattr(error, "winerror", None) in PRIVILEGE_WINERRORS


def junction_argv(link: Path, target: Path) -> list[str]:
    # Fixed command structure: paths travel as separate argv items and are
    # quoted by list2cmdline, never concatenated into one shell source string.
    return ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)]


def _is_local_volume(target: Path) -> bool:
    import ctypes
    if not target.anchor:
        return False
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(target.anchor))
    return drive_type == 3  # DRIVE_FIXED: local disk, includes other local volumes.


def _validate_junction_target(target: Path):
    if not target.is_absolute():
        raise ValueError(f"junction 目标必须是绝对路径：{target}")
    if str(target).startswith("\\\\"):
        raise ValueError(f"junction 不支持 UNC/网络路径目标：{target}")
    if is_link(target) or not target.is_dir():
        raise ValueError(f"junction 目标必须是真实本地目录：{target}")
    if os.name == "nt" and not _is_local_volume(target):
        raise ValueError(f"junction 目标必须在本地卷上：{target}")


def _create_junction(link: Path, target: Path):
    _validate_junction_target(target)
    try:
        result = subprocess.run(junction_argv(link, target), capture_output=True, text=True,
                                timeout=JUNCTION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"mklink /J 超时：{link}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise ValueError(f"mklink /J 失败（退出码 {result.returncode}）：{detail}")
    # Verify the reparse point actually landed on the intended target; this
    # also catches any argument the command interpreter may have mangled.
    try:
        if not is_junction(link):
            raise ValueError(f"junction 创建后校验失败：{link}")
        created, expected = os.stat(link), os.stat(target)
        if (created.st_dev, created.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"junction 指向与预期不符：{link}")
    except OSError as error:
        raise ValueError(f"junction 创建后校验失败：{link}") from error


def _create_windows(link: Path, target: Path):
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError as error:
        if not is_privilege_error(error):
            raise ValueError(f"创建目录符号链接失败：{error}") from error
    _create_junction(link, target)


def create_directory_link(link: Path, target: Path):
    """Create a directory link: symlink by default, junction fallback on Windows.

    The fallback happens only for the confirmed missing-symlink-privilege
    error; any other failure (target exists, invalid path, unsupported
    filesystem) is reported and never masked.
    """
    link, target = Path(link), Path(target)
    if link.exists() or is_link(link):
        raise ValueError(f"链接位置已存在：{link}")
    if is_link(target) or not target.is_dir():
        raise ValueError(f"目录链接目标必须是真实目录：{target}")
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    _create_windows(link, target)


def remove_directory_link(link: Path, *, missing_ok: bool = False):
    """Remove a directory link itself; never touch the target's contents."""
    link = Path(link)
    if link.is_symlink():
        link.unlink()
        return
    if is_junction(link):
        os.rmdir(link)
        return
    if link.exists():
        raise ValueError(f"真实目录不作为链接移除：{link}")
    if not missing_ok:
        raise ValueError(f"链接不存在：{link}")


def remove_tree(root: Path):
    """rmtree that unlinks directory links instead of descending into them."""
    root = Path(root)
    if is_link(root):
        remove_directory_link(root)
        return
    if not root.exists():
        return
    if not root.is_dir():
        root.unlink()
        return
    for child in root.iterdir():
        if is_link(child):
            remove_directory_link(child)
        elif child.is_dir():
            remove_tree(child)
        else:
            child.unlink()
    root.rmdir()
