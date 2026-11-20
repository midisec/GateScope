"""Store a tunnel control-plane key through an interactive, hidden root prompt.

Standalone: only the Python standard library is needed. This utility never
connects to OpenAI, starts services, reads an existing key, or accepts a key
from command-line arguments or environment variables. Formatting validation
does not establish whether OpenAI has issued or authorized the entered key.
"""

from __future__ import annotations

import fcntl
import getpass
import hmac
import os
import re
import secrets
import stat
import sys
import warnings
from pathlib import Path
from typing import Callable, TextIO


DEFAULT_DESTINATION = Path("/etc/gatescope-tunnel/control-plane.env")
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]+", re.ASCII)
MAX_KEY_CHARACTERS = 4096


class SecretSetupError(Exception):
    """Only fixed, non-sensitive error identifiers may cross the CLI boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_key(key: str) -> None:
    if (not isinstance(key, str) or not 4 <= len(key) <= MAX_KEY_CHARACTERS
            or KEY_PATTERN.fullmatch(key) is None):
        raise SecretSetupError("INVALID_CONTROL_PLANE_KEY")


def _path_parts(destination: Path) -> tuple[list[str], str]:
    path = Path(destination)
    if not path.is_absolute() or ".." in path.parts or path.name != "control-plane.env":
        raise SecretSetupError("UNSAFE_DESTINATION")
    directory_parts = list(path.parent.parts[1:])
    if not directory_parts:
        raise SecretSetupError("UNSAFE_DESTINATION")
    return directory_parts, path.name


def _safe_ancestor(info: os.stat_result, expected_uid: int) -> bool:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, expected_uid}:
        return False
    if not info.st_mode & 0o022:
        return True
    # Root-owned sticky temporary ancestors are safe for isolated test paths:
    # unprivileged users cannot replace somebody else's private child directory.
    # The destination directory itself never receives this exception.
    return info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)


def _open_directory(destination: Path, *, expected_uid: int, create: bool) -> int | None:
    parts, _ = _path_parts(destination)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/", flags)
    try:
        if not _safe_ancestor(os.fstat(current), expected_uid):
            raise SecretSetupError("UNSAFE_PARENT_DIRECTORY")
        for index, component in enumerate(parts):
            last = index == len(parts) - 1
            created = False
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not last or not create:
                    if last and not create:
                        os.close(current)
                        return None
                    raise SecretSetupError("PARENT_DIRECTORY_MISSING") from None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
            try:
                info = os.fstat(child)
                if last:
                    if created:
                        os.fchmod(child, 0o700)
                        info = os.fstat(child)
                        os.fsync(current)
                    if info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != 0o700:
                        raise SecretSetupError("UNSAFE_SECRET_DIRECTORY")
                elif not _safe_ancestor(info, expected_uid):
                    raise SecretSetupError("UNSAFE_PARENT_DIRECTORY")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _existing_file(directory: int, *, expected_uid: int) -> bool:
    try:
        info = os.stat("control-plane.env", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise SecretSetupError("UNSAFE_EXISTING_SECRET_FILE")
    return True


def destination_exists(destination: Path, *, expected_uid: int | None = None) -> bool:
    """Check metadata only: existing secret content is never opened or read."""
    uid = os.geteuid() if expected_uid is None else expected_uid
    directory = _open_directory(destination, expected_uid=uid, create=False)
    if directory is None:
        return False
    try:
        return _existing_file(directory, expected_uid=uid)
    finally:
        os.close(directory)


def store_control_plane_key(
    key: str, destination: Path = DEFAULT_DESTINATION, *, overwrite: bool = False,
    expected_uid: int | None = None,
) -> None:
    """Atomically publish/replace exactly one owner-only environment file.

    expected_uid supports isolated filesystem tests. The CLI separately requires
    effective UID 0, so the default installed directory and file remain root-only.
    """
    validate_key(key)
    uid = os.geteuid() if expected_uid is None else expected_uid
    directory = _open_directory(destination, expected_uid=uid, create=True)
    if directory is None:  # create=True either returns an open directory or raises.
        raise SecretSetupError("SECRET_DIRECTORY_UNAVAILABLE")
    temporary_name = ".control-plane." + secrets.token_hex(16) + ".tmp"
    temporary_created = False
    try:
        # Serialize this utility's concurrent invocations without adding a lock file.
        fcntl.flock(directory, fcntl.LOCK_EX)
        if _existing_file(directory, expected_uid=uid) and not overwrite:
            raise SecretSetupError("OVERWRITE_CONFIRMATION_REQUIRED")
        descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        temporary_created = True
        try:
            os.fchmod(descriptor, 0o600)
            payload = ("CONTROL_PLANE_API_KEY=" + key + "\n").encode("ascii")
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise SecretSetupError("SECRET_WRITE_FAILED")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Recheck the target after preparing the complete, fsynced replacement.
        exists = _existing_file(directory, expected_uid=uid)
        if exists and not overwrite:
            raise SecretSetupError("OVERWRITE_CONFIRMATION_REQUIRED")
        if overwrite:
            os.replace(temporary_name, "control-plane.env", src_dir_fd=directory, dst_dir_fd=directory)
            temporary_created = False
        else:
            # Atomic no-clobber publication: a concurrent creator must not be
            # overwritten even if it does not participate in the directory lock.
            os.link(temporary_name, "control-plane.env", src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.unlink(temporary_name, dir_fd=directory)
            temporary_created = False
        os.fsync(directory)
    finally:
        if temporary_created:
            os.unlink(temporary_name, dir_fd=directory)
        os.close(directory)


def configure_secret(
    destination: Path = DEFAULT_DESTINATION, *, input_stream: TextIO | None = None,
    secret_reader: Callable[[str], str] | None = None,
    confirm_reader: Callable[[str], str] | None = None,
    expected_uid: int | None = None,
) -> bool:
    stream = sys.stdin if input_stream is None else input_stream
    if not stream.isatty():
        raise SecretSetupError("INTERACTIVE_TTY_REQUIRED")
    read_secret = getpass.getpass if secret_reader is None else secret_reader
    read_confirmation = input if confirm_reader is None else confirm_reader
    exists = destination_exists(destination, expected_uid=expected_uid)
    if exists and read_confirmation("已有密钥文件。输入 OVERWRITE 确认覆盖，或直接回车取消：") != "OVERWRITE":
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            first = read_secret("OpenAI Tunnel control-plane API key（隐藏输入）：")
            validate_key(first)
            second = read_secret("再次输入相同 key（隐藏输入）：")
    except getpass.GetPassWarning:
        raise SecretSetupError("HIDDEN_INPUT_UNAVAILABLE") from None
    if not isinstance(second, str) or len(second) > MAX_KEY_CHARACTERS or not hmac.compare_digest(first.encode(), second.encode()):
        raise SecretSetupError("KEY_CONFIRMATION_MISMATCH")
    store_control_plane_key(first, destination, overwrite=exists, expected_uid=expected_uid)
    return True


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise SecretSetupError("COMMAND_LINE_ARGUMENTS_NOT_ALLOWED")
        if os.geteuid() != 0:
            raise SecretSetupError("ROOT_REQUIRED")
        written = configure_secret(expected_uid=0)
    except SecretSetupError as error:
        print("密钥未完成保存：" + error.code, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已取消，未完成保存。", file=sys.stderr)
        return 1
    except Exception:
        # OSError paths, exception messages and traceback details may contain
        # private input or deployment information. Never echo them here.
        print("密钥未完成保存：SECURE_SETUP_FAILED", file=sys.stderr)
        return 1
    if written:
        print("密钥已安全保存。未连接 OpenAI，未启动或重启任何服务。")
        return 0
    print("已取消，原文件保持不变。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
