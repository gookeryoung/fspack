"""loader 缓存的互斥访问、校验与原子发布."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from fspack.core.exceptions import LoaderError
from fspack.core.fsutil import sha256_file

__all__ = ["cache_lock", "cached_copy", "publish_cache"]

_logger = logging.getLogger("fspack.packaging.loader")
_LOCK_TIMEOUT = 120.0
_LOCK_POLL_INTERVAL = 0.05
_registry_guard = threading.Lock()


class _LockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users = 0
        self.file: BinaryIO | None = None


_lock_entries: dict[tuple[int, str], _LockEntry] = {}


def _acquire_file_lock(file: BinaryIO, target: Path, deadline: float) -> None:
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LoaderError(f"等待 loader 缓存锁超时: {target}") from exc
            time.sleep(min(_LOCK_POLL_INTERVAL, remaining))


def _release_file_lock(file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _lock_name(target: Path) -> str:
    name = os.path.normcase(str(target))
    # 路径并发创建时 realpath 可能保留长路径前缀，两种写法必须使用同一锁键。
    if os.name == "nt" and name.startswith("\\\\?\\"):
        return "\\\\" + name[8:] if name.startswith("\\\\?\\unc\\") else name[4:]
    return name


@contextmanager
def cache_lock(target: Path) -> Iterator[None]:
    """同键可重入锁；共享锁文件保留，内存条目在最后使用者退出后释放."""
    target = Path(os.path.realpath(os.fspath(target)))
    key = (os.getpid(), _lock_name(target))
    with _registry_guard:
        entry = _lock_entries.setdefault(key, _LockEntry())
        entry.users += 1
    acquired = False
    file = None
    file_locked = False
    deadline = time.monotonic() + _LOCK_TIMEOUT
    try:
        acquired = entry.lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise LoaderError(f"等待 loader 缓存锁超时: {target}")
        if entry.file is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            lock_path = target.with_name(target.name + ".lock")
            descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                file = os.fdopen(descriptor, "r+b", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
            _acquire_file_lock(file, target, deadline)
            file_locked = True
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"\0")
            entry.file = file
        yield
    finally:
        if file is not None:
            if file_locked:
                entry.file = None
                try:
                    _release_file_lock(file)
                except OSError as exc:
                    _logger.warning("释放 loader 缓存锁失败: %s: %s", target, exc)
            try:
                file.close()
            except OSError as exc:
                _logger.warning("关闭 loader 缓存锁失败: %s: %s", target, exc)
        if acquired:
            entry.lock.release()
        with _registry_guard:
            entry.users -= 1
            if entry.users == 0:
                del _lock_entries[key]


def cached_copy(target: Path, out: Path) -> bool:
    """只有完整且校验通过的缓存才复制；输出错误交给调用者."""
    verified = False
    try:
        if not target.is_file():
            return False
        with cache_lock(target):
            metadata = target.with_name(target.name + ".sha256")
            try:
                expected = metadata.read_text(encoding="ascii").strip()
                if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                    return False
                if sha256_file(target) != expected:
                    return False
            except (OSError, UnicodeError, ValueError):
                return False
            verified = True
            shutil.copy2(target, out)
            return True
    except OSError:
        if verified:
            raise
        return False


def publish_cache(source: Path, target: Path) -> None:
    """调用者持锁；缓存回写失败不影响构建产物."""
    temporary_paths: list[Path] = []
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
        temporary = Path(name)
        temporary_paths.append(temporary)
        os.close(descriptor)
        shutil.copy2(source, temporary)
        with temporary.open("r+b") as file:
            os.fsync(file.fileno())
        digest = sha256_file(temporary)
        metadata = target.with_name(target.name + ".sha256")
        descriptor, name = tempfile.mkstemp(prefix="." + metadata.name + ".", suffix=".tmp", dir=str(target.parent))
        temporary_metadata = Path(name)
        temporary_paths.append(temporary_metadata)
        try:
            file = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with file:
            file.write((digest + "\n").encode("ascii"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)  # noqa: PTH105
        os.replace(temporary_metadata, metadata)  # noqa: PTH105
    except (OSError, ValueError, shutil.Error) as exc:
        _logger.warning("loader 缓存回写失败（不影响构建）: %s: %s", target, exc)
    finally:
        for temporary in temporary_paths:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _logger.warning("清理 loader 缓存临时文件失败: %s: %s", temporary, exc)
