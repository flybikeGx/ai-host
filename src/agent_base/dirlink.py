"""Cross-platform directory links: symlink first, Windows junction fallback.

Junctions are only used for the base's own directory mounts (installed skills,
workspace skill mounts, run package mounts). Repo-content links stay symlinks
and fail loudly when the platform cannot create them.

The junction is written directly as a reparse point through DeviceIoControl, so
paths never pass through `cmd.exe` or any other command interpreter. That
matters because Windows paths may contain `&`, `^`, `%NAME%`, `!` or other cmd
metacharacters even without spaces, and `mklink` is a cmd builtin.
"""
from __future__ import annotations

import os
import stat as stat_module
import struct
from pathlib import Path

# ERROR_PRIVILEGE_NOT_HELD: the documented failure when a Windows user without
# the symlink privilege (or Developer Mode) creates a symbolic link.
PRIVILEGE_WINERRORS = {1314}

# Win32 constants for setting an IO_REPARSE_TAG_MOUNT_POINT reparse point.
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
FSCTL_SET_REPARSE_POINT = 0x000900A4
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = -1
MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024


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


def junction_reparse_data(target) -> bytes:
    """Build the REPARSE_DATA_BUFFER for a mount point, without any shell layer.

    Layout: REPARSE_DATA_BUFFER (8 bytes) followed by MountPointReparseBuffer:
    four USHORT name fields (8 bytes) and the path buffer holding the substitute
    name (``\\??\\<target>``) and print name, each NUL-terminated in UTF-16.
    """
    target = str(target)
    substitute = "\\??\\" + target
    substitute_bytes = substitute.encode("utf-16-le")
    print_bytes = target.encode("utf-16-le")
    buffer = substitute_bytes + b"\0\0" + print_bytes + b"\0\0"
    if 8 + 8 + len(buffer) > MAXIMUM_REPARSE_DATA_BUFFER_SIZE:
        raise ValueError(f"junction 目标路径过长，超过 reparse 缓冲上限：{target}")
    header = struct.pack("<IHH", IO_REPARSE_TAG_MOUNT_POINT, 8 + len(buffer), 0)
    fields = struct.pack("<HHHH", 0, len(substitute_bytes),
                         len(substitute_bytes) + 2, len(print_bytes))
    return header + fields + buffer


def _create_junction_native(link: Path, target: Path):
    """Set the junction reparse point directly; no cmd.exe involvement."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                         ctypes.c_void_p]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    try:
        link.mkdir()
    except OSError as error:
        raise ValueError(f"无法创建 junction 目录：{link}") from error
    try:
        handle = kernel32.CreateFileW(
            str(link), GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None, OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, None)
        if handle in (None, INVALID_HANDLE_VALUE, ctypes.c_void_p(-1).value):
            raise ValueError(f"无法打开目录以设置 junction：{ctypes.WinError(ctypes.get_last_error())}")
        try:
            data = junction_reparse_data(target)
            buffer = ctypes.create_string_buffer(data, len(data))
            returned = wintypes.DWORD(0)
            if not kernel32.DeviceIoControl(handle, FSCTL_SET_REPARSE_POINT, buffer, len(data),
                                            None, 0, ctypes.byref(returned), None):
                raise ValueError(f"设置 junction 失败：{ctypes.WinError(ctypes.get_last_error())}")
        finally:
            kernel32.CloseHandle(handle)
    except BaseException:
        # Never leave a plain empty directory behind on failure.
        try:
            link.rmdir()
        except OSError:
            pass
        raise


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


def _verify_junction(link: Path, target: Path):
    try:
        if not is_junction(link):
            raise ValueError(f"junction 创建后校验失败：{link}")
        created, expected = os.stat(link), os.stat(target)
        if (created.st_dev, created.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"junction 指向与预期不符：{link}")
    except OSError as error:
        raise ValueError(f"junction 创建后校验失败：{link}") from error


def _create_junction(link: Path, target: Path):
    _validate_junction_target(target)
    _create_junction_native(link, target)
    _verify_junction(link, target)


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
